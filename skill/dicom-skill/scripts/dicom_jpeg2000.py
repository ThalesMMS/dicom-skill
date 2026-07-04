#!/usr/bin/env python3
"""DICOM JPEG 2000 local transcoding for dicom-skill.

Compresses DICOM Pixel Data to JPEG 2000 or decompresses compressed DICOM files
to Explicit VR Little Endian. This script only transforms local files; it never
connects to a DICOM node.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from pydicom import dcmread
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import JPEG2000, JPEG2000Lossless, UID
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": f"pydicom is required: {exc}"}), file=sys.stderr)
    raise

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _common import (
    InputFile,
    discover_dicom_files,
    print_or_write as _print_or_write,
    save_dataset_atomic,
    uid_to_plain,
    value_to_plain,
)


DEFAULT_SYNTAX = "lossless"
JPEG2000_SYNTAXES = {
    "lossless": JPEG2000Lossless,
    "lossy": JPEG2000,
}


def get_transfer_syntax(ds: Dataset) -> UID:
    file_meta = getattr(ds, "file_meta", None)
    transfer_syntax = getattr(file_meta, "TransferSyntaxUID", None)
    if not transfer_syntax:
        raise ValueError("Missing file_meta.TransferSyntaxUID; cannot determine pixel data encoding.")
    return UID(str(transfer_syntax))


def sync_file_meta(ds: Dataset) -> None:
    file_meta = getattr(ds, "file_meta", None)
    if file_meta is None:
        file_meta = FileMetaDataset()
        ds.file_meta = file_meta
    if getattr(ds, "SOPClassUID", None):
        file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    if getattr(ds, "SOPInstanceUID", None):
        file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID


def save_dataset(ds: Dataset, dest: Path) -> None:
    save_dataset_atomic(ds, dest)


def parse_float_list(values: list[str] | None, option_name: str) -> list[float] | None:
    parsed: list[float] = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                parsed.append(float(part))
            except ValueError as exc:
                raise ValueError(f"{option_name} must contain numbers, got: {part}") from exc
    return parsed or None


def compression_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    j2k_cr = parse_float_list(args.j2k_cr, "--j2k-cr")
    j2k_psnr = parse_float_list(args.j2k_psnr, "--j2k-psnr")
    if j2k_cr and j2k_psnr:
        raise ValueError("Use either --j2k-cr or --j2k-psnr, not both.")
    if args.syntax == "lossless" and (j2k_cr or j2k_psnr):
        raise ValueError("--j2k-cr and --j2k-psnr are only valid with --syntax lossy.")
    if args.syntax == "lossy" and not (j2k_cr or j2k_psnr):
        raise ValueError("--syntax lossy requires --j2k-cr or --j2k-psnr.")

    kwargs: dict[str, Any] = {"generate_instance_uid": not args.keep_instance_uid}
    if args.encoding_plugin:
        kwargs["encoding_plugin"] = args.encoding_plugin
    if j2k_cr:
        kwargs["j2k_cr"] = j2k_cr
    if j2k_psnr:
        kwargs["j2k_psnr"] = j2k_psnr
    return kwargs


def decompression_kwargs(args: argparse.Namespace, *, intermediate: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "generate_instance_uid": False if intermediate else not args.keep_instance_uid,
    }
    if args.decoding_plugin:
        kwargs["decoding_plugin"] = args.decoding_plugin
    if getattr(args, "preserve_color_space", False):
        kwargs["as_rgb"] = False
    return kwargs


def transform_one(
    item: InputFile,
    args: argparse.Namespace,
    out_dir: Path,
    prepared_compression_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dest = out_dir / item.relative_output
    result: dict[str, Any] = {
        "source": str(item.source),
        "destination": str(dest),
        "status": "pending",
        "source_bytes": item.source.stat().st_size,
    }

    if dest.exists() and not args.overwrite and not args.dry_run:
        result["status"] = "error"
        result["error"] = "Destination exists. Use --overwrite to replace it."
        return result

    try:
        ds = dcmread(str(item.source), force=args.force, defer_size="1 KB")
        source_transfer_syntax = get_transfer_syntax(ds)
        result["source_transfer_syntax"] = uid_to_plain(source_transfer_syntax)

        if "PixelData" not in ds:
            result["status"] = "passthrough"
            result["reason"] = "Dataset has no PixelData element; copied unchanged."
            result["destination_transfer_syntax"] = uid_to_plain(source_transfer_syntax)
            result["SOPInstanceUID"] = value_to_plain(getattr(ds, "SOPInstanceUID", None))
            if args.dry_run:
                result["status"] = "planned_passthrough"
                return result
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.source, dest)
            result["destination_bytes"] = dest.stat().st_size
            return result

        if args.command == "compress":
            target_syntax = UID(str(JPEG2000_SYNTAXES[args.syntax]))
            result["target_transfer_syntax"] = uid_to_plain(target_syntax)
            if source_transfer_syntax == target_syntax:
                result["status"] = "skipped"
                result["reason"] = "Already encoded with the requested JPEG 2000 transfer syntax."
                return result
            if args.dry_run:
                result["status"] = "planned"
                return result
            if source_transfer_syntax.is_compressed:
                ds.decompress(**decompression_kwargs(args, intermediate=True))
                result["intermediate_decompression"] = True
            ds.compress(target_syntax, **(prepared_compression_kwargs or compression_kwargs(args)))

        elif args.command == "decompress":
            if not source_transfer_syntax.is_compressed:
                result["status"] = "skipped"
                result["reason"] = "Dataset is already uncompressed."
                return result
            if args.dry_run:
                result["status"] = "planned"
                return result
            ds.decompress(**decompression_kwargs(args))

        else:  # pragma: no cover
            raise ValueError(f"Unknown command: {args.command}")

        sync_file_meta(ds)
        save_dataset(ds, dest)
        result["status"] = "converted"
        result["destination_bytes"] = dest.stat().st_size
        result["destination_transfer_syntax"] = uid_to_plain(get_transfer_syntax(ds))
        if result["destination_bytes"]:
            result["size_ratio_source_to_destination"] = round(result["source_bytes"] / result["destination_bytes"], 4)
        result["SOPInstanceUID"] = value_to_plain(getattr(ds, "SOPInstanceUID", None))
        return result

    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = str(exc)
        return result


def command_summary(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items") or []
    return {
        "operation": result.get("operation"),
        "syntax": result.get("syntax"),
        "out_dir": result.get("out_dir"),
        "file_count": result.get("file_count"),
        "converted_count": sum(1 for item in items if item.get("status") == "converted"),
        "planned_count": sum(1 for item in items if item.get("status") == "planned"),
        "skipped_count": sum(1 for item in items if item.get("status") == "skipped"),
        "passthrough_count": sum(1 for item in items if item.get("status") in {"passthrough", "planned_passthrough"}),
        "error_count": sum(1 for item in items if item.get("status") == "error"),
        "ok": result.get("ok"),
    }


def print_or_write(result: dict[str, Any], out_json: str | None = None, summary: bool = False) -> None:
    _print_or_write(result, out_json, command_summary if summary else None)


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out).expanduser().resolve()
    prepared_compression_kwargs = compression_kwargs(args) if args.command == "compress" else None
    files = discover_dicom_files(args.path, force=args.force, max_files=args.max_files)
    result: dict[str, Any] = {
        "operation": f"JPEG2000-{args.command.upper()}",
        "syntax": getattr(args, "syntax", None),
        "out_dir": str(out_dir),
        "file_count": len(files),
        "dry_run": bool(args.dry_run),
        "files": [str(item.source) for item in files] if args.include_files else None,
        "items": [],
    }
    if not files:
        result["ok"] = False
        result["error"] = "No readable DICOM files with SOPClassUID found."
        return result
    for item in files:
        result["items"].append(transform_one(item, args, out_dir, prepared_compression_kwargs))
    result["ok"] = not any(item.get("status") == "error" for item in result["items"])
    return result


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", action="append", required=True, help="DICOM file or directory; can be repeated")
    parser.add_argument("--out", required=True, help="Output directory for converted files")
    parser.add_argument("--force", action="store_true", help="Force pydicom reads for non-standard files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing destination files")
    parser.add_argument("--dry-run", action="store_true", help="Inspect files and planned operation without writing output")
    parser.add_argument("--include-files", action="store_true", help="Include discovered file list in JSON output")
    parser.add_argument("--max-files", type=int, help="Maximum number of files to discover/convert")
    parser.add_argument("--keep-instance-uid", action="store_true", help="Do not generate a new SOP Instance UID during transcoding")
    parser.add_argument("--decoding-plugin", help="pydicom decoding plugin name, for example pylibjpeg")
    parser.add_argument("--out-json", help="Write JSON result to this file as well as stdout")
    parser.add_argument("--summary", action="store_true", help="Print concise PHI-light summary to stdout; --out-json still stores full JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compress/decompress local DICOM files with JPEG 2000.")
    sub = parser.add_subparsers(dest="command", required=True)

    compress_p = sub.add_parser("compress", help="Compress DICOM Pixel Data to JPEG 2000")
    add_common_args(compress_p)
    compress_p.add_argument("--syntax", choices=sorted(JPEG2000_SYNTAXES), default=DEFAULT_SYNTAX, help="JPEG 2000 transfer syntax")
    compress_p.add_argument("--encoding-plugin", help="pydicom encoding plugin name, for example pylibjpeg")
    compress_p.add_argument("--j2k-cr", action="append", help="Lossy JPEG 2000 compression ratio layer; comma-separated or repeatable")
    compress_p.add_argument("--j2k-psnr", action="append", help="Lossy JPEG 2000 PSNR layer; comma-separated or repeatable")

    decompress_p = sub.add_parser("decompress", help="Decompress compressed DICOM Pixel Data to Explicit VR Little Endian")
    add_common_args(decompress_p)
    decompress_p.add_argument("--preserve-color-space", action="store_true", help="Do not convert YBR color data to RGB during decompression")

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
