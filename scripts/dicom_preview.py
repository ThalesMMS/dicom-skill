#!/usr/bin/env python3
"""Render local DICOM instances to PNG previews for dicom-skill.

This script only reads local DICOM files and writes PNG files. It never connects
to a DICOM node and does not remove or modify the source instances.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import numpy as np
    from PIL import Image
    from pydicom import dcmread
    from pydicom.dataset import Dataset
    from pydicom.pixels import apply_color_lut, apply_modality_lut, apply_voi_lut
    from pydicom.uid import UID
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": f"PNG preview dependencies are required: {exc}"}), file=sys.stderr)
    raise


@dataclass(frozen=True)
class InputFile:
    source: Path
    relative_output: Path


def value_to_plain(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def uid_to_plain(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    uid = UID(str(value))
    return {"uid": str(uid), "name": uid.name, "is_compressed": bool(uid.is_compressed)}


def parse_percentiles(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError("--percentile-window must be LOW,HIGH, for example 0.5,99.5")
    low, high = (float(parts[0]), float(parts[1]))
    if not 0 <= low < high <= 100:
        raise ValueError("--percentile-window values must satisfy 0 <= LOW < HIGH <= 100.")
    return low, high


def discover_dicom_files(paths: list[str], *, force: bool = False, max_files: int | None = None) -> list[InputFile]:
    discovered: list[InputFile] = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        if path.is_file():
            candidates = [(path, Path(path.name))]
        elif path.is_dir():
            root_name = path.name or "input"
            candidates = [
                (candidate, Path(root_name) / candidate.relative_to(path))
                for candidate in path.rglob("*")
                if candidate.is_file()
            ]
        else:
            raise FileNotFoundError(f"Path not found: {item}")

        for candidate, relative_output in candidates:
            try:
                ds = dcmread(str(candidate), stop_before_pixels=True, force=force)
                if getattr(ds, "SOPClassUID", None):
                    discovered.append(InputFile(candidate, relative_output))
            except Exception:
                continue
            if max_files is not None and len(discovered) >= max_files:
                return discovered
    return discovered


def relative_png_path(relative_output: Path, frame_number: int | None = None) -> Path:
    if relative_output.suffix:
        base = relative_output.with_suffix("")
    else:
        base = relative_output
    if frame_number is not None:
        return base.with_name(f"{base.name}_frame{frame_number:04d}.png")
    return base.with_suffix(".png")


def safe_frame_count(ds: Dataset, arr: np.ndarray | None = None) -> int:
    frames = getattr(ds, "NumberOfFrames", None)
    try:
        count = int(frames)
        if count > 0:
            return count
    except Exception:
        pass
    if arr is None:
        return 1
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if samples == 1 and arr.ndim == 3:
        return int(arr.shape[0])
    if samples > 1 and arr.ndim == 4:
        return int(arr.shape[0])
    return 1


def frame_from_array(arr: np.ndarray, ds: Dataset, frame_index: int) -> np.ndarray:
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if arr.ndim == 2:
        if frame_index != 0:
            raise IndexError("Frame index out of range for single-frame image.")
        return arr
    if arr.ndim == 3:
        if samples == 1:
            return arr[frame_index]
        if frame_index != 0:
            raise IndexError("Frame index out of range for single-frame color image.")
        return arr
    if arr.ndim == 4:
        return arr[frame_index]
    raise ValueError(f"Unsupported pixel array shape: {arr.shape}")


def normalize_to_uint8(arr: np.ndarray, percentiles: tuple[float, float] | None = None) -> np.ndarray:
    values = np.asarray(arr, dtype=np.float64)
    finite = np.isfinite(values)
    if not finite.any():
        return np.zeros(values.shape, dtype=np.uint8)

    if percentiles:
        low, high = np.percentile(values[finite], percentiles)
    else:
        low = float(np.min(values[finite]))
        high = float(np.max(values[finite]))

    if high <= low:
        return np.zeros(values.shape, dtype=np.uint8)

    values = np.clip(values, low, high)
    values = (values - low) / (high - low)
    return np.rint(values * 255.0).astype(np.uint8)


def apply_manual_window(arr: np.ndarray, center: float, width: float) -> np.ndarray:
    if width <= 0:
        raise ValueError("--window-width must be greater than zero.")
    low = center - width / 2.0
    high = center + width / 2.0
    return np.clip(np.asarray(arr, dtype=np.float64), low, high)


def grayscale_to_png_array(frame: np.ndarray, ds: Dataset, args: argparse.Namespace) -> tuple[np.ndarray, list[str]]:
    warnings: list[str] = []
    work = np.asarray(frame)

    if args.window_center is not None or args.window_width is not None:
        if args.window_center is None or args.window_width is None:
            raise ValueError("Use --window-center and --window-width together.")
        try:
            work = apply_modality_lut(work, ds)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not apply Modality LUT: {exc}")
        work = apply_manual_window(work, args.window_center, args.window_width)
    elif not args.no_window:
        try:
            work = apply_modality_lut(work, ds)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not apply Modality LUT: {exc}")
        try:
            work = apply_voi_lut(work, ds, index=args.voi_index, prefer_lut=not args.prefer_window)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not apply VOI LUT/window: {exc}")

    out = normalize_to_uint8(np.asarray(work), args.percentile_window)
    if str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        out = np.uint8(255) - out
    return out, warnings


def color_to_png_array(frame: np.ndarray, ds: Dataset, args: argparse.Namespace) -> tuple[np.ndarray, list[str]]:
    warnings: list[str] = []
    work = np.asarray(frame)
    photometric = str(getattr(ds, "PhotometricInterpretation", "")).upper()

    if photometric == "PALETTE COLOR":
        try:
            work = apply_color_lut(work, ds)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not apply Palette Color LUT: {exc}")

    if work.dtype == np.uint8:
        out = work
    else:
        out = normalize_to_uint8(work, args.percentile_window)

    if out.ndim != 3 or out.shape[-1] not in (3, 4):
        raise ValueError(f"Unsupported color pixel array shape after conversion: {out.shape}")
    return out, warnings


def render_png_array(frame: np.ndarray, ds: Dataset, args: argparse.Namespace) -> tuple[np.ndarray, list[str]]:
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    photometric = str(getattr(ds, "PhotometricInterpretation", "")).upper()
    if samples > 1 or photometric == "PALETTE COLOR":
        return color_to_png_array(frame, ds, args)
    return grayscale_to_png_array(frame, ds, args)


def load_pixels(ds: Dataset, args: argparse.Namespace, frame_index: int | None = None) -> np.ndarray:
    options: dict[str, Any] = {"raw": False}
    if frame_index is not None:
        options["index"] = frame_index
    if args.decoding_plugin:
        options["decoding_plugin"] = args.decoding_plugin
    ds.pixel_array_options(**options)
    return np.asarray(ds.pixel_array)


def maybe_resize(image: Image.Image, max_size: int | None) -> Image.Image:
    if not max_size or max(image.size) <= max_size:
        return image
    resized = image.copy()
    resized.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return resized


def save_png(png_array: np.ndarray, dest: Path, args: argparse.Namespace) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = Image.fromarray(png_array)
    image = maybe_resize(image, args.max_size)
    image.save(dest, format="PNG")
    return {"path": str(dest), "width": image.size[0], "height": image.size[1], "mode": image.mode, "bytes": dest.stat().st_size}


def transform_one(item: InputFile, args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": str(item.source),
        "status": "pending",
        "source_bytes": item.source.stat().st_size,
        "pngs": [],
        "warnings": [],
    }

    try:
        ds = dcmread(str(item.source), force=args.force, defer_size="1 KB")
        if "PixelData" not in ds:
            raise ValueError("Dataset has no PixelData element.")

        transfer_syntax = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
        result.update(
            {
                "sop_class_uid": value_to_plain(getattr(ds, "SOPClassUID", None)),
                "sop_instance_uid": value_to_plain(getattr(ds, "SOPInstanceUID", None)),
                "transfer_syntax": uid_to_plain(transfer_syntax),
                "photometric_interpretation": value_to_plain(getattr(ds, "PhotometricInterpretation", None)),
                "burned_in_annotation": value_to_plain(getattr(ds, "BurnedInAnnotation", None)),
            }
        )

        if args.all_frames:
            arr = load_pixels(ds, args)
            frame_count = safe_frame_count(ds, arr)
            frame_indexes = list(range(frame_count))
        else:
            requested_frame = args.frame - 1
            if requested_frame < 0:
                raise ValueError("--frame is 1-based and must be at least 1.")
            frame_count = safe_frame_count(ds)
            if requested_frame >= frame_count:
                raise ValueError(f"Requested frame {args.frame}, but instance has {frame_count} frame(s).")
            arr = load_pixels(ds, args, frame_index=requested_frame)
            frame_indexes = [requested_frame]

        result["frame_count"] = frame_count
        result["rows"] = int(getattr(ds, "Rows", 0) or 0)
        result["columns"] = int(getattr(ds, "Columns", 0) or 0)
        result["samples_per_pixel"] = int(getattr(ds, "SamplesPerPixel", 1) or 1)

        for frame_index in frame_indexes:
            frame = frame_from_array(arr, ds, frame_index) if args.all_frames else arr
            png_array, warnings = render_png_array(frame, ds, args)
            frame_number = frame_index + 1 if args.all_frames or frame_count > 1 else None
            dest = out_dir / relative_png_path(item.relative_output, frame_number=frame_number)
            if dest.exists() and not args.overwrite and not args.dry_run:
                raise FileExistsError(f"Destination exists: {dest}. Use --overwrite to replace it.")
            planned = {"path": str(dest), "frame": frame_index + 1, "width": int(png_array.shape[1]), "height": int(png_array.shape[0])}
            if warnings:
                result["warnings"].extend(warnings)
            if args.dry_run:
                result["pngs"].append(planned)
            else:
                written = save_png(png_array, dest, args)
                written["frame"] = frame_index + 1
                result["pngs"].append(written)

        result["status"] = "planned" if args.dry_run else "converted"
        result["png_count"] = len(result["pngs"])
        return result

    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = str(exc)
        return result


def command_summary(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items") or []
    return {
        "operation": result.get("operation"),
        "out_dir": result.get("out_dir"),
        "file_count": result.get("file_count"),
        "png_count": sum(int(item.get("png_count") or 0) for item in items),
        "converted_count": sum(1 for item in items if item.get("status") == "converted"),
        "planned_count": sum(1 for item in items if item.get("status") == "planned"),
        "error_count": sum(1 for item in items if item.get("status") == "error"),
        "ok": result.get("ok"),
    }


def print_or_write(result: dict[str, Any], out_json: str | None = None, summary: bool = False) -> None:
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(text + "\n", encoding="utf-8")
    printable = command_summary(result) if summary else result
    print(json.dumps(printable, indent=2, ensure_ascii=False))


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    args.percentile_window = parse_percentiles(args.percentile_window)
    out_dir = Path(args.out).expanduser().resolve()
    files = discover_dicom_files(args.path, force=args.force, max_files=args.max_files)
    result: dict[str, Any] = {
        "operation": "DICOM-PREVIEW-PNG",
        "out_dir": str(out_dir),
        "file_count": len(files),
        "dry_run": bool(args.dry_run),
        "all_frames": bool(args.all_frames),
        "frame": args.frame,
        "files": [str(item.source) for item in files] if args.include_files else None,
        "items": [],
    }
    if not files:
        result["ok"] = False
        result["error"] = "No readable DICOM files with SOPClassUID found."
        return result
    for item in files:
        result["items"].append(transform_one(item, args, out_dir))
    result["ok"] = not any(item.get("status") == "error" for item in result["items"])
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render local DICOM instance previews to PNG.")
    parser.add_argument("--path", action="append", required=True, help="DICOM file or directory; can be repeated")
    parser.add_argument("--out", required=True, help="Output directory for PNG preview files")
    parser.add_argument("--force", action="store_true", help="Force pydicom reads for non-standard files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing PNG files")
    parser.add_argument("--dry-run", action="store_true", help="Inspect files and planned PNGs without writing output")
    parser.add_argument("--include-files", action="store_true", help="Include discovered file list in JSON output")
    parser.add_argument("--max-files", type=int, help="Maximum number of DICOM files to discover/convert")
    parser.add_argument("--frame", type=int, default=1, help="1-based frame number to render; default first frame")
    parser.add_argument("--all-frames", action="store_true", help="Render every frame in each instance")
    parser.add_argument("--decoding-plugin", help="pydicom decoding plugin name, for example pylibjpeg")
    parser.add_argument("--no-window", action="store_true", help="Skip DICOM Modality/VOI LUT and windowing for grayscale images")
    parser.add_argument("--prefer-window", action="store_true", help="Prefer Window Center/Width over VOI LUT Sequence when both are present")
    parser.add_argument("--voi-index", type=int, default=0, help="0-based VOI LUT/window alternative index")
    parser.add_argument("--window-center", type=float, help="Manual window center for grayscale previews")
    parser.add_argument("--window-width", type=float, help="Manual window width for grayscale previews")
    parser.add_argument("--percentile-window", help="Normalize displayed values to LOW,HIGH percentiles, for example 0.5,99.5")
    parser.add_argument("--max-size", type=int, help="Resize PNG so the longest side is at most this many pixels")
    parser.add_argument("--out-json", help="Write JSON result to this file as well as stdout")
    parser.add_argument("--summary", action="store_true", help="Print concise PHI-light summary to stdout; --out-json still stores full JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_command(args)
        print_or_write(result, getattr(args, "out_json", None), getattr(args, "summary", False))
        return 0 if result.get("ok") else 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
