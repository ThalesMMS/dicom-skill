"""Shared helpers for dicom-skill scripts.

Every script in this skill is still runnable directly (``python scripts/x.py``);
this module only holds logic that was previously duplicated across them:
JSON serialization of pydicom values, DICOM file discovery, filesystem-safe
path components, result printing, and atomic file writes.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.multival import MultiValue
from pydicom.tag import BaseTag
from pydicom.uid import UID


def value_to_plain(value: Any) -> Any:
    """Convert a pydicom element value into a JSON-serializable value."""
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, UID):
        return str(value)
    if isinstance(value, (MultiValue, list, tuple)):
        return [value_to_plain(v) for v in value]
    if isinstance(value, Dataset):
        return dataset_to_plain(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def dataset_to_plain(ds: Dataset | None) -> dict[str, Any] | None:
    if ds is None:
        return None
    out: dict[str, Any] = {}
    for elem in ds:
        key = elem.keyword or f"{int(elem.tag):08X}"
        if elem.VR == "SQ":
            out[key] = [dataset_to_plain(item) for item in elem.value]
        else:
            out[key] = value_to_plain(elem.value)
    return out


def uid_to_plain(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    uid = UID(str(value))
    return {"uid": str(uid), "name": uid.name, "is_compressed": bool(uid.is_compressed)}


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, UID):
        return str(value)
    if isinstance(value, BaseTag):
        return f"{int(value):08X}"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def safe_path_component(value: Any, fallback: str, *, max_len: int = 180) -> str:
    """Sanitize a DICOM value (usually a UID) into a filesystem-safe name."""
    text = "" if value is None else str(value).strip()
    if not text:
        return fallback
    keep = []
    for ch in text:
        if ch.isalnum() or ch in ".-_^":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:max_len] or fallback


@dataclass(frozen=True)
class InputFile:
    source: Path
    relative_output: Path


def discover_dicom_files(
    paths: list[str],
    *,
    force: bool = False,
    max_files: int | None = None,
    required_attributes: tuple[str, ...] = ("SOPClassUID",),
) -> list[InputFile]:
    """Find readable DICOM files under the given files/directories.

    Directory contents are sorted so discovery order is deterministic across
    filesystems. ``relative_output`` preserves the input directory structure
    for scripts that mirror it under an output directory.
    """
    discovered: list[InputFile] = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        if path.is_file():
            candidates = [(path, Path(path.name))]
        elif path.is_dir():
            root_name = path.name or "input"
            candidates = [
                (candidate, Path(root_name) / candidate.relative_to(path))
                for candidate in sorted(path.rglob("*"))
                if candidate.is_file()
            ]
        else:
            raise FileNotFoundError(f"Path not found: {item}")

        for candidate, relative_output in candidates:
            try:
                ds = dcmread(str(candidate), stop_before_pixels=True, force=force)
                if all(getattr(ds, attr, None) for attr in required_attributes):
                    discovered.append(InputFile(candidate, relative_output))
            except Exception:
                continue
            if max_files is not None and len(discovered) >= max_files:
                return discovered
    return discovered


def print_or_write(
    result: dict[str, Any],
    out_json: str | None = None,
    summary_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> None:
    """Print the result JSON (or its summary) and optionally persist the full JSON."""
    text = json.dumps(result, indent=2, ensure_ascii=False, default=json_default)
    if out_json:
        path = Path(out_json).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    printable = summary_fn(result) if summary_fn else result
    print(json.dumps(printable, indent=2, ensure_ascii=False, default=json_default))


def _atomic_replace(dest: Path, write_tmp: Callable[[Path], None]) -> None:
    """Write via a temp file in the destination directory, then rename into place.

    Guarantees the destination is either absent or complete, never partial.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        write_tmp(tmp)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def save_dataset_atomic(ds: Dataset, dest: Path) -> None:
    """Write a pydicom dataset atomically (temp file + rename)."""

    def _write(tmp: Path) -> None:
        try:
            ds.save_as(str(tmp), enforce_file_format=True)
        except TypeError:  # older pydicom compatibility
            ds.save_as(str(tmp), write_like_original=False)

    _atomic_replace(dest, _write)


def write_bytes_atomic(dest: Path, data: bytes) -> None:
    _atomic_replace(dest, lambda tmp: tmp.write_bytes(data))


def write_private_text(dest: Path, text: str) -> None:
    """Write a file readable only by the current user (mode 0600).

    Used for outputs that can contain PHI or credentials.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        f = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    with f:
        f.write(text)
    os.chmod(dest, 0o600)
