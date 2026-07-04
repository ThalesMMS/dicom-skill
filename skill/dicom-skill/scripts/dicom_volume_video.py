#!/usr/bin/env python3
"""Export local DICOM image series to MP4 videos.

This script only reads local DICOM files and writes local MP4 files. It never
connects to a DICOM node and does not remove or modify source instances.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import imageio.v2 as imageio
    import numpy as np
    from PIL import Image
    from pydicom import dcmread
    from pydicom.dataset import Dataset
    from pydicom.multival import MultiValue
    from pydicom.pixels import apply_modality_lut
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": f"DICOM series video dependencies are required: {exc}"}), file=sys.stderr)
    raise

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _common import (
    InputFile,
    discover_dicom_files as _discover_input_files,
    print_or_write as _print_or_write,
    value_to_plain,
)


DEFAULT_LARGE_SERIES_MIN_IMAGES = 100
DEFAULT_MR_MIN_IMAGES = 15
CT_DEFAULT_FRAME_RATE = 10.0
MR_SMALL_SERIES_FRAME_RATE = 3.0
MR_LARGE_SERIES_FRAME_RATE = 10.0
DEFAULT_PLANES = ("axial",)
VALID_PLANES = {"axial", "sagittal", "coronal"}
PLANE_ALIASES = {
    "axial": "axial",
    "sagittal": "sagittal",
    "sagital": "sagittal",
    "coronal": "coronal",
}


@dataclass(frozen=True)
class VideoInstance:
    source: Path
    modality: str
    study_uid: str
    series_uid: str
    sop_instance_uid: str | None
    series_number: str | None
    series_description: str | None
    instance_number: int | None
    position_key: float | None
    rows: int
    columns: int
    frame_count: int
    pixel_spacing: tuple[float, float] | None
    slice_thickness: float | None
    spacing_between_slices: float | None
    photometric_interpretation: str | None


@dataclass
class VideoSeries:
    modality: str
    study_uid: str
    series_uid: str
    series_number: str | None = None
    series_description: str | None = None
    instances: list[VideoInstance] = field(default_factory=list)

    @property
    def instance_count(self) -> int:
        return len(self.instances)

    @property
    def image_count(self) -> int:
        return sum(max(1, instance.frame_count) for instance in self.instances)

    @property
    def rows(self) -> int:
        return self.instances[0].rows if self.instances else 0

    @property
    def columns(self) -> int:
        return self.instances[0].columns if self.instances else 0

    @property
    def pixel_spacing(self) -> tuple[float, float]:
        for instance in self.instances:
            if instance.pixel_spacing:
                return instance.pixel_spacing
        return (1.0, 1.0)

    @property
    def slice_spacing(self) -> float:
        positions = [instance.position_key for instance in self.sorted_instances() if instance.position_key is not None]
        if len(positions) >= 2:
            deltas = [abs(b - a) for a, b in zip(positions, positions[1:]) if abs(b - a) > 1e-6]
            if deltas:
                return float(np.median(np.asarray(deltas, dtype=np.float64)))
        for instance in self.instances:
            if instance.spacing_between_slices and instance.spacing_between_slices > 0:
                return float(instance.spacing_between_slices)
        for instance in self.instances:
            if instance.slice_thickness and instance.slice_thickness > 0:
                return float(instance.slice_thickness)
        return 1.0

    def sorted_instances(self) -> list[VideoInstance]:
        return sorted(
            self.instances,
            key=lambda item: (
                item.position_key is None,
                item.position_key if item.position_key is not None else 0.0,
                item.instance_number is None,
                item.instance_number if item.instance_number is not None else 0,
                str(item.source),
            ),
        )


def first_scalar(value: Any, index: int = 0) -> Any:
    if isinstance(value, MultiValue) or isinstance(value, (list, tuple)):
        if not value:
            return None
        try:
            return value[index]
        except IndexError:
            return value[0]
    return value


def optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except Exception:
        return None


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def parse_pixel_spacing(value: Any) -> tuple[float, float] | None:
    if value in (None, ""):
        return None
    try:
        values = [float(part) for part in value]
        if len(values) >= 2 and values[0] > 0 and values[1] > 0:
            return values[0], values[1]
    except Exception:
        return None
    return None


def position_key(ds: Dataset) -> float | None:
    try:
        orientation = [float(value) for value in getattr(ds, "ImageOrientationPatient")]
        position = [float(value) for value in getattr(ds, "ImagePositionPatient")]
    except Exception:
        return None
    if len(orientation) != 6 or len(position) != 3:
        return None
    row = np.asarray(orientation[:3], dtype=np.float64)
    col = np.asarray(orientation[3:], dtype=np.float64)
    normal = np.cross(row, col)
    norm = np.linalg.norm(normal)
    if not np.isfinite(norm) or norm <= 0:
        return None
    normal = normal / norm
    return float(np.dot(normal, np.asarray(position, dtype=np.float64)))


def safe_frame_count(ds: Dataset) -> int:
    frames = getattr(ds, "NumberOfFrames", None)
    try:
        count = int(frames)
        if count > 0:
            return count
    except Exception:
        pass
    return 1


def discover_dicom_files(paths: list[str], *, force: bool = False, max_files: int | None = None) -> list[InputFile]:
    return _discover_input_files(paths, force=force, max_files=max_files, required_attributes=("SOPClassUID", "Modality"))


def instance_from_file(item: InputFile, *, force: bool) -> VideoInstance | None:
    ds = dcmread(str(item.source), stop_before_pixels=True, force=force)
    modality = str(getattr(ds, "Modality", "")).upper()
    if not modality:
        return None
    study_uid = optional_str(getattr(ds, "StudyInstanceUID", None))
    series_uid = optional_str(getattr(ds, "SeriesInstanceUID", None))
    if not study_uid or not series_uid:
        return None
    rows = optional_int(getattr(ds, "Rows", None)) or 0
    columns = optional_int(getattr(ds, "Columns", None)) or 0
    if rows <= 0 or columns <= 0:
        return None
    return VideoInstance(
        source=item.source,
        modality=modality,
        study_uid=study_uid,
        series_uid=series_uid,
        sop_instance_uid=optional_str(getattr(ds, "SOPInstanceUID", None)),
        series_number=optional_str(getattr(ds, "SeriesNumber", None)),
        series_description=optional_str(getattr(ds, "SeriesDescription", None)),
        instance_number=optional_int(getattr(ds, "InstanceNumber", None)),
        position_key=position_key(ds),
        rows=rows,
        columns=columns,
        frame_count=safe_frame_count(ds),
        pixel_spacing=parse_pixel_spacing(getattr(ds, "PixelSpacing", None)),
        slice_thickness=optional_float(getattr(ds, "SliceThickness", None)),
        spacing_between_slices=optional_float(getattr(ds, "SpacingBetweenSlices", None)),
        photometric_interpretation=optional_str(getattr(ds, "PhotometricInterpretation", None)),
    )


def discover_video_series(args: argparse.Namespace) -> list[VideoSeries]:
    files = discover_dicom_files(args.path, force=args.force, max_files=args.max_files)
    requested_modalities = {modality.upper() for modality in args.modality or []}
    series_by_uid: dict[tuple[str, str], VideoSeries] = {}
    for item in files:
        try:
            instance = instance_from_file(item, force=args.force)
        except Exception:
            continue
        if instance is None:
            continue
        if args.study_uid and instance.study_uid not in args.study_uid:
            continue
        if requested_modalities and instance.modality not in requested_modalities:
            continue
        key = (instance.study_uid, instance.series_uid)
        series = series_by_uid.setdefault(
            key,
            VideoSeries(
                modality=instance.modality,
                study_uid=instance.study_uid,
                series_uid=instance.series_uid,
                series_number=instance.series_number,
                series_description=instance.series_description,
            ),
        )
        series.instances.append(instance)

    ordered = list(series_by_uid.values())
    for series in ordered:
        series.instances = series.sorted_instances()
    return sorted(
        ordered,
        key=lambda item: (
            item.study_uid,
            item.modality,
            optional_int(item.series_number) is None,
            optional_int(item.series_number) if optional_int(item.series_number) is not None else 0,
            item.series_uid,
        ),
    )


def selection_requested(args: argparse.Namespace) -> bool:
    return bool(args.series_uid or args.series_number or args.series_description_contains)


def selected_by_explicit_selector(series: VideoSeries, args: argparse.Namespace) -> tuple[bool, str | None]:
    requested = selection_requested(args)
    if args.series_uid and series.series_uid in set(args.series_uid):
        return True, "series_uid"
    if args.series_number and series.series_number in {str(value) for value in args.series_number}:
        return True, "series_number"
    if args.series_description_contains:
        description = (series.series_description or "").lower()
        if any(fragment.lower() in description for fragment in args.series_description_contains):
            return True, "series_description_contains"
    if requested:
        return False, "not_selected"
    return False, None


def automatic_policy(series: VideoSeries, args: argparse.Namespace) -> tuple[bool, str | None]:
    if series.modality == "CT":
        if series.image_count > args.large_series_min_images:
            return True, f"ct_image_count>{args.large_series_min_images}"
        return False, f"ct_image_count<={args.large_series_min_images}"
    if series.modality == "MR":
        if args.mr_min_images <= series.image_count <= args.large_series_min_images:
            return True, f"mr_{args.mr_min_images}<=image_count<={args.large_series_min_images}"
        if series.image_count > args.large_series_min_images:
            return True, f"mr_image_count>{args.large_series_min_images}"
        return False, f"mr_image_count<{args.mr_min_images}"
    return False, "modality_requires_explicit_series_and_frame_rate"


def series_matches_selection(series: VideoSeries, args: argparse.Namespace) -> tuple[bool, str | None]:
    explicit, reason = selected_by_explicit_selector(series, args)
    if selection_requested(args):
        return explicit, reason or "not_selected"
    return automatic_policy(series, args)


def default_frame_rate(series: VideoSeries, args: argparse.Namespace) -> tuple[float | None, str]:
    if args.frame_rate is not None:
        return args.frame_rate, "explicit_frame_rate"
    if series.modality == "CT":
        return CT_DEFAULT_FRAME_RATE, "ct_default"
    if series.modality == "MR":
        if args.mr_min_images <= series.image_count <= args.large_series_min_images:
            return MR_SMALL_SERIES_FRAME_RATE, "mr_15_to_100_default"
        if series.image_count > args.large_series_min_images:
            return MR_LARGE_SERIES_FRAME_RATE, "mr_over_100_default"
        return None, "mr_below_default_range_requires_frame_rate"
    return None, "modality_requires_frame_rate"


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


def parse_planes(values: list[str] | None) -> list[str]:
    if not values:
        return list(DEFAULT_PLANES)
    planes: list[str] = []
    for value in values:
        for part in value.split(","):
            requested = part.strip().lower()
            if not requested:
                continue
            plane = PLANE_ALIASES.get(requested)
            if plane not in VALID_PLANES:
                raise ValueError(f"Unsupported plane '{requested}'. Use axial, sagittal, or coronal.")
            if plane not in planes:
                planes.append(plane)
    if not planes:
        return list(DEFAULT_PLANES)
    return planes


def slug(value: str, *, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return (cleaned or "unknown")[:max_len]


def uid_suffix(value: str) -> str:
    parts = [part for part in value.split(".") if part]
    suffix = "_".join(parts[-4:]) if parts else value
    return slug(suffix, max_len=48)


def series_output_base(out_dir: Path, series: VideoSeries) -> Path:
    study_part = f"study_{uid_suffix(series.study_uid)}"
    modality_part = slug(series.modality or "unknown", max_len=12)
    number_part = f"{modality_part}_series_{slug(series.series_number or 'unknown', max_len=24)}"
    uid_part = uid_suffix(series.series_uid)
    return out_dir / study_part / f"{number_part}_{uid_part}"


def series_json(
    series: VideoSeries,
    *,
    include_files: bool = False,
    include_descriptions: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "modality": series.modality,
        "study_uid": series.study_uid,
        "series_uid": series.series_uid,
        "series_number": value_to_plain(series.series_number),
        "instance_count": series.instance_count,
        "image_count": series.image_count,
        "rows": series.rows,
        "columns": series.columns,
        "pixel_spacing": list(series.pixel_spacing),
        "slice_spacing": series.slice_spacing,
    }
    if include_descriptions:
        item["series_description"] = value_to_plain(series.series_description)
    if include_files:
        item["files"] = [str(instance.source) for instance in series.instances]
    return item


def load_pixel_array(ds: Dataset, args: argparse.Namespace) -> tuple[np.ndarray, bool]:
    options: dict[str, Any] = {"raw": False}
    if args.decoding_plugin:
        options["decoding_plugin"] = args.decoding_plugin
    ds.pixel_array_options(**options)
    arr = np.asarray(ds.pixel_array)
    samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
    if samples > 1:
        if arr.ndim not in (3, 4) or arr.shape[-1] not in (3, 4):
            raise ValueError(f"Unsupported color pixel array shape: {arr.shape}")
        return arr, True
    if arr.ndim not in (2, 3):
        raise ValueError(f"Unsupported grayscale pixel array shape: {arr.shape}")
    try:
        arr = apply_modality_lut(arr, ds)
    except Exception:
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        arr = np.asarray(arr, dtype=np.float32) * slope + intercept
    return np.asarray(arr, dtype=np.float32), False


def load_volume(series: VideoSeries, args: argparse.Namespace) -> tuple[np.ndarray, list[str], Dataset | None, bool]:
    frames: list[np.ndarray] = []
    warnings: list[str] = []
    reference_ds: Dataset | None = None
    expected_shape: tuple[int, ...] | None = None
    series_is_color = False

    for instance in series.sorted_instances():
        ds = dcmread(str(instance.source), force=args.force, defer_size="1 KB")
        if reference_ds is None:
            reference_ds = ds
        if "PixelData" not in ds:
            raise ValueError(f"Dataset has no PixelData element: {instance.source}")
        photometric = str(getattr(ds, "PhotometricInterpretation", "")).upper()
        samples = int(getattr(ds, "SamplesPerPixel", 1) or 1)
        if samples == 1 and photometric == "MONOCHROME1":
            warnings.append("MONOCHROME1 data was inverted after windowing.")

        arr, is_color = load_pixel_array(ds, args)
        if frames and is_color != series_is_color:
            raise ValueError("Cannot mix grayscale and color instances in one video series.")
        series_is_color = is_color
        slice_arrays: Iterable[np.ndarray]
        if is_color and arr.ndim == 3:
            slice_arrays = [arr]
        elif is_color and arr.ndim == 4:
            slice_arrays = [arr[index] for index in range(arr.shape[0])]
        elif arr.ndim == 2:
            slice_arrays = [arr]
        else:
            slice_arrays = [arr[index] for index in range(arr.shape[0])]

        for slice_array in slice_arrays:
            if expected_shape is None:
                expected_shape = tuple(slice_array.shape)
            elif tuple(slice_array.shape) != expected_shape:
                raise ValueError(f"Inconsistent slice shape {slice_array.shape}; expected {expected_shape}.")
            frames.append(np.asarray(slice_array, dtype=np.float32))

    if not frames:
        raise ValueError("No pixel frames were loaded for this series.")
    return np.stack(frames, axis=0), warnings, reference_ds, series_is_color


def window_from_dataset(ds: Dataset | None, index: int = 0) -> tuple[float, float] | None:
    if ds is None:
        return None
    center = first_scalar(getattr(ds, "WindowCenter", None), index)
    width = first_scalar(getattr(ds, "WindowWidth", None), index)
    try:
        center_f = float(center)
        width_f = float(width)
    except Exception:
        return None
    if width_f <= 0:
        return None
    return center_f, width_f


def normalize_to_uint8(values: np.ndarray, percentiles: tuple[float, float] | None = None) -> np.ndarray:
    work = np.asarray(values, dtype=np.float32)
    finite = np.isfinite(work)
    if not finite.any():
        return np.zeros(work.shape, dtype=np.uint8)
    if percentiles:
        low, high = np.percentile(work[finite], percentiles)
    else:
        low = float(np.min(work[finite]))
        high = float(np.max(work[finite]))
    if high <= low:
        return np.zeros(work.shape, dtype=np.uint8)
    work = np.clip(work, low, high)
    work = (work - low) / (high - low)
    return np.rint(work * 255.0).astype(np.uint8)


def window_to_uint8(values: np.ndarray, low: float, high: float) -> np.ndarray:
    if high <= low:
        raise ValueError("Window high bound must be greater than low bound.")
    work = np.asarray(values, dtype=np.float32)
    work = np.clip(work, low, high)
    work = (work - low) / (high - low)
    return np.rint(work * 255.0).astype(np.uint8)


def window_volume(volume: np.ndarray, reference_ds: Dataset | None, args: argparse.Namespace, *, is_color: bool) -> tuple[np.ndarray, str]:
    if is_color:
        if volume.dtype == np.uint8:
            return volume, "color_passthrough"
        return normalize_to_uint8(volume, args.percentile_window), "color_normalized"

    if args.window_center is not None or args.window_width is not None:
        if args.window_center is None or args.window_width is None:
            raise ValueError("Use --window-center and --window-width together.")
        center = float(args.window_center)
        width = float(args.window_width)
        if width <= 0:
            raise ValueError("--window-width must be greater than zero.")
        low = center - width / 2.0
        high = center + width / 2.0
        return window_to_uint8(volume, low, high), "manual_window"

    if not args.no_window:
        dataset_window = window_from_dataset(reference_ds, args.voi_index)
        if dataset_window is not None:
            center, width = dataset_window
            low = center - width / 2.0
            high = center + width / 2.0
            return window_to_uint8(volume, low, high), "dicom_window"

    if args.percentile_window is not None:
        return normalize_to_uint8(volume, args.percentile_window), "percentile_window"
    return normalize_to_uint8(volume), "minmax"


def plane_spacing(series: VideoSeries, plane: str) -> tuple[float, float]:
    row_spacing, column_spacing = series.pixel_spacing
    slice_spacing = series.slice_spacing
    if plane == "axial":
        return row_spacing, column_spacing
    if plane == "coronal":
        return slice_spacing, column_spacing
    if plane == "sagittal":
        return slice_spacing, row_spacing
    raise ValueError(f"Unsupported plane: {plane}")


def plane_frame_count(volume: np.ndarray, plane: str) -> int:
    if plane == "axial":
        return int(volume.shape[0])
    if plane == "coronal":
        return int(volume.shape[1])
    if plane == "sagittal":
        return int(volume.shape[2])
    raise ValueError(f"Unsupported plane: {plane}")


def plane_frame_count_from_shape(volume_shape: tuple[int, int, int], plane: str) -> int:
    if plane == "axial":
        return int(volume_shape[0])
    if plane == "coronal":
        return int(volume_shape[1])
    if plane == "sagittal":
        return int(volume_shape[2])
    raise ValueError(f"Unsupported plane: {plane}")


def iter_plane_frames(volume: np.ndarray, plane: str, *, reverse: bool = False) -> Iterable[np.ndarray]:
    if plane == "axial":
        indexes = range(volume.shape[0] - 1, -1, -1) if reverse else range(volume.shape[0])
        for index in indexes:
            yield volume[index, :, :]
    elif plane == "coronal":
        indexes = range(volume.shape[1] - 1, -1, -1) if reverse else range(volume.shape[1])
        for index in indexes:
            yield np.flipud(volume[:, index, :])
    elif plane == "sagittal":
        indexes = range(volume.shape[2] - 1, -1, -1) if reverse else range(volume.shape[2])
        for index in indexes:
            yield np.flipud(volume[:, :, index])
    else:
        raise ValueError(f"Unsupported plane: {plane}")


def scaled_size(
    shape: tuple[int, int],
    spacing: tuple[float, float],
    *,
    preserve_spacing: bool,
    max_size: int | None,
    pad_to_multiple: int,
) -> tuple[int, int]:
    rows, columns = shape
    if preserve_spacing:
        physical_height = max(float(rows) * spacing[0], 1.0)
        physical_width = max(float(columns) * spacing[1], 1.0)
        scale = max(rows, columns) / max(physical_height, physical_width)
        height = max(1, int(round(physical_height * scale)))
        width = max(1, int(round(physical_width * scale)))
    else:
        height = rows
        width = columns

    if max_size and max_size > 0 and max(width, height) > max_size:
        scale = max_size / float(max(width, height))
        width = max(1, int(round(width * scale)))
        height = max(1, int(round(height * scale)))

    if pad_to_multiple > 1:
        width = int(math.ceil(width / pad_to_multiple) * pad_to_multiple)
        height = int(math.ceil(height / pad_to_multiple) * pad_to_multiple)
    return width, height


def resize_frame(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    image = Image.fromarray(frame)
    if image.mode == "RGBA":
        image = image.convert("RGB")
    if image.size != size:
        image = image.resize(size, Image.Resampling.BILINEAR)
    return np.asarray(image, dtype=np.uint8)


def video_frame_array(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return np.repeat(frame[:, :, None], 3, axis=2)
    if frame.ndim == 3 and frame.shape[-1] == 3:
        return frame
    if frame.ndim == 3 and frame.shape[-1] == 4:
        return frame[:, :, :3]
    raise ValueError(f"Unsupported video frame shape: {frame.shape}")


def writer_kwargs(args: argparse.Namespace, frame_rate: float) -> dict[str, Any]:
    params: dict[str, Any] = {
        "fps": frame_rate,
        "codec": args.codec,
        "macro_block_size": None,
    }
    if args.quality is not None:
        params["quality"] = args.quality
    ffmpeg_params = ["-movflags", "+faststart"]
    if args.ffmpeg_param:
        ffmpeg_params.extend(args.ffmpeg_param)
    params["ffmpeg_params"] = ffmpeg_params
    return params


@contextlib.contextmanager
def atomic_video_writer(dest: Path, args: argparse.Namespace, frame_rate: float) -> Iterator[Any]:
    """Write the MP4 to a temp name and rename into place on success.

    An interrupted or failed export never leaves a partial file at the
    destination path.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part.mp4")
    try:
        with imageio.get_writer(str(tmp), **writer_kwargs(args, frame_rate)) as writer:
            yield writer
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def write_plane_video(
    volume: np.ndarray,
    series: VideoSeries,
    plane: str,
    dest: Path,
    args: argparse.Namespace,
    frame_rate: float,
) -> dict[str, Any]:
    first_frame = next(iter_plane_frames(volume, plane, reverse=args.reverse), None)
    if first_frame is None:
        raise ValueError(f"No frames for plane {plane}.")
    spacing = plane_spacing(series, plane)
    size = scaled_size(
        tuple(first_frame.shape),
        spacing,
        preserve_spacing=not args.no_preserve_spacing,
        max_size=args.max_size,
        pad_to_multiple=args.pad_to_multiple,
    )
    if dest.exists() and not args.overwrite:
        raise FileExistsError(f"Destination exists: {dest}. Use --overwrite to replace it.")

    frame_count = 0
    with atomic_video_writer(dest, args, frame_rate) as writer:
        for frame in iter_plane_frames(volume, plane, reverse=args.reverse):
            resized = resize_frame(frame, size)
            writer.append_data(video_frame_array(resized))
            frame_count += 1

    return {
        "plane": plane,
        "path": str(dest),
        "frame_count": frame_count,
        "frame_rate": frame_rate,
        "width": size[0],
        "height": size[1],
        "bytes": dest.stat().st_size,
    }


def streaming_axial_window(series: VideoSeries, args: argparse.Namespace) -> tuple[float, float, str] | None:
    """Return (low, high, label) display bounds when they are known up front.

    Streaming export writes frames as they are loaded, so it needs the window
    bounds before seeing the whole volume. That is only possible for grayscale
    series with a manual window or a DICOM window in the reference instance;
    percentile/min-max normalization needs full-volume statistics.
    """
    if not series.instances:
        return None
    for instance in series.instances:
        photometric = (instance.photometric_interpretation or "").upper()
        if photometric not in ("MONOCHROME1", "MONOCHROME2"):
            return None
    if args.window_center is not None or args.window_width is not None:
        if args.window_center is None or args.window_width is None:
            raise ValueError("Use --window-center and --window-width together.")
        width = float(args.window_width)
        if width <= 0:
            raise ValueError("--window-width must be greater than zero.")
        center = float(args.window_center)
        return center - width / 2.0, center + width / 2.0, "manual_window"
    if not args.no_window and args.percentile_window is None:
        first = series.sorted_instances()[0]
        try:
            reference = dcmread(str(first.source), stop_before_pixels=True, force=args.force)
        except Exception:  # noqa: BLE001 - fall back to full-volume loading
            return None
        window = window_from_dataset(reference, args.voi_index)
        if window is not None:
            center, width = window
            return center - width / 2.0, center + width / 2.0, "dicom_window"
    return None


def export_axial_streaming(
    series: VideoSeries,
    args: argparse.Namespace,
    dest: Path,
    frame_rate: float,
    bounds: tuple[float, float, str],
) -> dict[str, Any]:
    """Export the axial plane one instance at a time without stacking the volume."""
    low, high, _ = bounds
    if dest.exists() and not args.overwrite:
        raise FileExistsError(f"Destination exists: {dest}. Use --overwrite to replace it.")

    instances = series.sorted_instances()
    if args.reverse:
        instances = list(reversed(instances))
    expected_shape: tuple[int, ...] | None = None
    size: tuple[int, int] | None = None
    frame_count = 0

    with atomic_video_writer(dest, args, frame_rate) as writer:
        for instance in instances:
            ds = dcmread(str(instance.source), force=args.force, defer_size="1 KB")
            if "PixelData" not in ds:
                raise ValueError(f"Dataset has no PixelData element: {instance.source}")
            arr, is_color = load_pixel_array(ds, args)
            if is_color:
                raise ValueError("Streaming export supports grayscale series only.")
            slices = [arr] if arr.ndim == 2 else [arr[index] for index in range(arr.shape[0])]
            if args.reverse:
                slices = list(reversed(slices))
            invert = str(getattr(ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1"
            for slice_array in slices:
                if expected_shape is None:
                    expected_shape = tuple(slice_array.shape)
                    size = scaled_size(
                        expected_shape,
                        plane_spacing(series, "axial"),
                        preserve_spacing=not args.no_preserve_spacing,
                        max_size=args.max_size,
                        pad_to_multiple=args.pad_to_multiple,
                    )
                elif tuple(slice_array.shape) != expected_shape:
                    raise ValueError(f"Inconsistent slice shape {slice_array.shape}; expected {expected_shape}.")
                frame = window_to_uint8(slice_array, low, high)
                if invert:
                    frame = np.uint8(255) - frame
                assert size is not None
                writer.append_data(video_frame_array(resize_frame(frame, size)))
                frame_count += 1

    if frame_count == 0:
        raise ValueError("No pixel frames were loaded for this series.")
    assert size is not None
    return {
        "plane": "axial",
        "path": str(dest),
        "frame_count": frame_count,
        "frame_rate": frame_rate,
        "width": size[0],
        "height": size[1],
        "bytes": dest.stat().st_size,
    }


def planned_plane_video(
    volume_shape: tuple[int, ...],
    series: VideoSeries,
    plane: str,
    dest: Path,
    args: argparse.Namespace,
    frame_rate: float,
) -> dict[str, Any]:
    if plane == "axial":
        frame_shape = (volume_shape[1], volume_shape[2])
    elif plane == "coronal":
        frame_shape = (volume_shape[0], volume_shape[2])
    elif plane == "sagittal":
        frame_shape = (volume_shape[0], volume_shape[1])
    else:
        raise ValueError(f"Unsupported plane: {plane}")
    size = scaled_size(
        frame_shape,
        plane_spacing(series, plane),
        preserve_spacing=not args.no_preserve_spacing,
        max_size=args.max_size,
        pad_to_multiple=args.pad_to_multiple,
    )
    return {
        "plane": plane,
        "path": str(dest),
        "frame_count": plane_frame_count_from_shape(volume_shape, plane),
        "frame_rate": frame_rate,
        "width": size[0],
        "height": size[1],
    }


def export_series(series: VideoSeries, args: argparse.Namespace, out_dir: Path, planes: list[str], frame_rate: float, frame_rate_reason: str) -> dict[str, Any]:
    result = series_json(
        series,
        include_files=args.include_files,
        include_descriptions=args.include_descriptions,
    )
    result.update({"status": "pending", "videos": [], "warnings": [], "frame_rate": frame_rate, "frame_rate_reason": frame_rate_reason})
    base = series_output_base(out_dir, series)

    try:
        if args.dry_run:
            estimated_shape = (series.image_count, series.rows, series.columns)
            result["status"] = "planned"
            result["videos"] = [
                planned_plane_video(estimated_shape, series, plane, base.with_name(f"{base.name}_{plane}.mp4"), args, frame_rate)
                for plane in planes
            ]
            return result

        if planes == ["axial"]:
            bounds = streaming_axial_window(series, args)
            if bounds is not None:
                dest = base.with_name(f"{base.name}_axial.mp4")
                result["export_mode"] = "streaming_axial"
                result["windowing"] = bounds[2]
                result["videos"].append(export_axial_streaming(series, args, dest, frame_rate, bounds))
                result["status"] = "exported"
                return result

        volume, warnings, reference_ds, is_color = load_volume(series, args)
        result["export_mode"] = "full_volume"
        result["loaded_shape"] = list(volume.shape)
        result["warnings"].extend(warnings)
        volume_uint8, windowing = window_volume(volume, reference_ds, args, is_color=is_color)
        if not is_color and str(getattr(reference_ds, "PhotometricInterpretation", "")).upper() == "MONOCHROME1":
            volume_uint8 = np.uint8(255) - volume_uint8
        result["windowing"] = windowing

        for plane in planes:
            dest = base.with_name(f"{base.name}_{plane}.mp4")
            result["videos"].append(write_plane_video(volume_uint8, series, plane, dest, args, frame_rate))

        result["status"] = "exported"
        return result

    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = str(exc)
        return result


def command_summary(result: dict[str, Any]) -> dict[str, Any]:
    series_items = result.get("series") or []
    videos = [video for item in series_items for video in item.get("videos", [])]
    return {
        "operation": result.get("operation"),
        "out_dir": result.get("out_dir"),
        "discovered_series_count": result.get("discovered_series_count"),
        "selected_series_count": result.get("selected_series_count"),
        "video_count": len(videos),
        "exported_count": sum(1 for item in series_items if item.get("status") == "exported"),
        "planned_count": sum(1 for item in series_items if item.get("status") == "planned"),
        "listed_count": sum(1 for item in series_items if item.get("status") == "listed"),
        "skipped_count": sum(1 for item in series_items if item.get("status") == "skipped"),
        "error_count": sum(1 for item in series_items if item.get("status") == "error"),
        "ok": result.get("ok"),
    }


def print_or_write(result: dict[str, Any], out_json: str | None = None, summary: bool = False) -> None:
    _print_or_write(result, out_json, command_summary if summary else None)


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    args.planes = parse_planes(args.plane)
    args.percentile_window = parse_percentiles(args.percentile_window)
    if args.frame_rate is not None and args.frame_rate <= 0:
        raise ValueError("--frame-rate must be greater than zero.")
    if args.large_series_min_images < 0:
        raise ValueError("--large-series-min-images cannot be negative.")
    if args.mr_min_images < 0:
        raise ValueError("--mr-min-images cannot be negative.")
    if args.pad_to_multiple < 1:
        raise ValueError("--pad-to-multiple must be at least 1.")

    out_dir = Path(args.out).expanduser().resolve()
    series_list = discover_video_series(args)
    result: dict[str, Any] = {
        "operation": "DICOM-SERIES-MP4",
        "out_dir": str(out_dir),
        "dry_run": bool(args.dry_run),
        "list_series": bool(args.list_series),
        "planes": args.planes,
        "frame_rate_override": args.frame_rate,
        "large_series_min_images": args.large_series_min_images,
        "mr_min_images": args.mr_min_images,
        "selection_requested": selection_requested(args),
        "discovered_series_count": len(series_list),
        "selected_series_count": 0,
        "series": [],
    }

    if not series_list:
        result["ok"] = False
        result["error"] = "No readable local DICOM image series found."
        return result

    for series in series_list:
        selected, reason = series_matches_selection(series, args)
        frame_rate, frame_rate_reason = default_frame_rate(series, args)
        item = series_json(
            series,
            include_files=args.include_files,
            include_descriptions=args.include_descriptions,
        )
        item["selected"] = selected
        item["selection_reason"] = reason
        item["frame_rate"] = frame_rate
        item["frame_rate_reason"] = frame_rate_reason
        if args.list_series:
            item["status"] = "listed"
            result["series"].append(item)
            continue
        if not selected:
            item["status"] = "skipped"
            result["series"].append(item)
            continue
        result["selected_series_count"] += 1
        if frame_rate is None:
            item["status"] = "error"
            item["error"] = (
                f"Modality {series.modality} has no automatic frame-rate policy for "
                f"{series.image_count} image(s). Ask the user for the desired frames/sec "
                "and rerun with --frame-rate."
            )
            result["series"].append(item)
            continue
        exported = export_series(series, args, out_dir, args.planes, frame_rate, frame_rate_reason)
        exported["selected"] = True
        exported["selection_reason"] = reason
        result["series"].append(exported)

    if args.list_series:
        result["selected_series_count"] = sum(1 for item in result["series"] if item.get("selected"))
        result["ok"] = True
        return result

    if result["selected_series_count"] == 0:
        result["ok"] = False
        result["error"] = (
            "No DICOM image series selected. By default this command exports CT series with "
            f"more than {args.large_series_min_images} images, MR series with "
            f"{args.mr_min_images}-{args.large_series_min_images} images at 3 frames/sec, "
            f"and MR series with more than {args.large_series_min_images} images at 10 frames/sec. "
            "For other modalities, list the series and ask the user which series and frame rate to export."
        )
        return result

    result["ok"] = not any(item.get("status") == "error" for item in result["series"] if item.get("selected"))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export local DICOM image series to MP4 videos.")
    parser.add_argument("--path", action="append", required=True, help="DICOM file or directory; can be repeated")
    parser.add_argument("--out", required=True, help="Output directory for MP4 files")
    parser.add_argument("--study-uid", action="append", help="Restrict input to one StudyInstanceUID; can be repeated")
    parser.add_argument("--modality", action="append", help="Restrict input to a DICOM modality such as CT, MR, US, XA, or NM; can be repeated")
    parser.add_argument("--series-uid", action="append", help="Export a specific SeriesInstanceUID; can be repeated")
    parser.add_argument("--series-number", action="append", help="Export a specific SeriesNumber; can be repeated")
    parser.add_argument(
        "--series-description-contains",
        action="append",
        help="Export series whose SeriesDescription contains this text; can be repeated",
    )
    parser.add_argument(
        "--large-series-min-images",
        "--min-images",
        dest="large_series_min_images",
        type=int,
        default=DEFAULT_LARGE_SERIES_MIN_IMAGES,
        help="Large-series threshold for CT and MR automatic policy; default 100",
    )
    parser.add_argument(
        "--mr-min-images",
        type=int,
        default=DEFAULT_MR_MIN_IMAGES,
        help="Minimum MR image count for automatic 3 frames/sec export; default 15",
    )
    parser.add_argument("--plane", action="append", help="Plane to export: axial, sagittal, coronal; comma-separated or repeated")
    parser.add_argument("--frame-rate", type=float, help="Override MP4 frame rate in frames/sec")
    parser.add_argument("--force", action="store_true", help="Force pydicom reads for non-standard files")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing MP4 files")
    parser.add_argument("--dry-run", action="store_true", help="Inspect matching series and planned videos without writing output")
    parser.add_argument("--list-series", action="store_true", help="List discovered DICOM image series without exporting videos")
    parser.add_argument("--include-files", action="store_true", help="Include source file paths in JSON output")
    parser.add_argument("--include-descriptions", action="store_true", help="Include SeriesDescription in JSON output")
    parser.add_argument("--max-files", type=int, help="Maximum number of DICOM files to discover")
    parser.add_argument("--decoding-plugin", help="pydicom decoding plugin name, for example pylibjpeg")
    parser.add_argument("--window-center", type=float, help="Manual grayscale window center")
    parser.add_argument("--window-width", type=float, help="Manual grayscale window width")
    parser.add_argument("--no-window", action="store_true", help="Skip DICOM Window Center/Width and use percentile/minmax normalization")
    parser.add_argument("--voi-index", type=int, default=0, help="0-based DICOM Window Center/Width alternative index")
    parser.add_argument("--percentile-window", help="Normalize displayed values to LOW,HIGH percentiles when no DICOM/manual window is used")
    parser.add_argument("--max-size", type=int, default=1024, help="Maximum output frame side after spacing correction; use 0 to disable")
    parser.add_argument("--no-preserve-spacing", action="store_true", help="Do not resize reformats to approximate physical pixel spacing")
    parser.add_argument("--pad-to-multiple", type=int, default=2, help="Pad output dimensions to this multiple for H.264 compatibility")
    parser.add_argument("--reverse", action="store_true", help="Reverse frame order within each exported plane")
    parser.add_argument("--codec", default="libx264", help="FFmpeg video codec; default libx264")
    parser.add_argument("--quality", type=int, default=8, help="imageio/FFmpeg quality value; default 8")
    parser.add_argument("--ffmpeg-param", action="append", help="Additional raw FFmpeg parameter; can be repeated")
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
