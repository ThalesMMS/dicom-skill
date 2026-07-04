from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from pydicom import dcmread
from pydicom.uid import UID

import _common
from conftest import make_ct_dataset


def test_safe_path_component_sanitizes():
    assert _common.safe_path_component("1.2.3.4", "fb") == "1.2.3.4"
    assert _common.safe_path_component("a b/c\\d", "fb") == "a_b_c_d"
    assert _common.safe_path_component(None, "fb") == "fb"
    assert _common.safe_path_component("  ", "fb") == "fb"


def test_value_to_plain_handles_dicom_types():
    assert _common.value_to_plain(b"\x00\x01") == "<2 bytes>"
    assert _common.value_to_plain(UID("1.2.840.10008.1.2")) == "1.2.840.10008.1.2"
    assert _common.value_to_plain(["a", b"xy"]) == ["a", "<2 bytes>"]
    json.dumps(_common.value_to_plain(make_ct_dataset()))  # must be serializable


def test_discover_dicom_files(tmp_path: Path):
    ds = make_ct_dataset()
    ds.save_as(str(tmp_path / "b.dcm"), enforce_file_format=True)
    make_ct_dataset().save_as(str(tmp_path / "a.dcm"), enforce_file_format=True)
    (tmp_path / "junk.txt").write_text("not dicom")

    found = _common.discover_dicom_files([str(tmp_path)])
    names = [item.source.name for item in found]
    assert names == ["a.dcm", "b.dcm"]  # sorted, junk excluded
    assert found[0].relative_output == Path(tmp_path.name) / "a.dcm"

    limited = _common.discover_dicom_files([str(tmp_path)], max_files=1)
    assert len(limited) == 1


def test_discover_requires_attributes(tmp_path: Path):
    ds = make_ct_dataset()
    del ds.Modality
    ds.save_as(str(tmp_path / "nomod.dcm"), enforce_file_format=True)
    assert _common.discover_dicom_files([str(tmp_path)], required_attributes=("SOPClassUID", "Modality")) == []
    assert len(_common.discover_dicom_files([str(tmp_path)])) == 1


def test_save_dataset_atomic_writes_readable_file(tmp_path: Path):
    dest = tmp_path / "nested" / "out.dcm"
    _common.save_dataset_atomic(make_ct_dataset(), dest)
    assert dest.exists()
    assert not list(dest.parent.glob("*.part"))
    assert dcmread(str(dest)).Modality == "CT"


def test_write_private_text_mode(tmp_path: Path):
    dest = tmp_path / "secret.json"
    _common.write_private_text(dest, "{}")
    mode = stat.S_IMODE(os.stat(dest).st_mode)
    assert mode == 0o600
    assert dest.read_text(encoding="utf-8") == "{}"
