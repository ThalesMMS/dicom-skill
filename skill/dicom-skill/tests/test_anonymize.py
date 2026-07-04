from __future__ import annotations

import json
import os
import stat
from datetime import datetime
from pathlib import Path

from pydicom import dcmread

import dicom_anonymize
from conftest import make_ct_dataset


def write_input(tmp_path: Path, **kwargs) -> Path:
    src = tmp_path / "input"
    src.mkdir(parents=True, exist_ok=True)
    ds = make_ct_dataset(**kwargs)
    ds.save_as(str(src / f"{ds.SOPInstanceUID}.dcm"), enforce_file_format=True)
    return src


def run_anonymize(src: Path, out: Path, *extra: str) -> int:
    return dicom_anonymize.main(["--path", str(src), "--out", str(out), "--salt", "unit-test-salt", *extra])


def read_single_output(out: Path):
    files = sorted(out.rglob("*.dcm"))
    assert len(files) == 1
    return dcmread(str(files[0]))


def test_anonymize_removes_phi(tmp_path: Path, capsys):
    src = write_input(tmp_path)
    out = tmp_path / "anon"
    assert run_anonymize(src, out) == 0
    capsys.readouterr()

    ds = read_single_output(out)
    assert str(getattr(ds, "PatientName", "")) != "Doe^Jane"
    assert ds.PatientID != "PAT001"
    assert ds.PatientIdentityRemoved == "YES"
    assert str(ds.AccessionNumber) != "ACC123"


def test_uid_remap_deterministic_per_salt(tmp_path: Path, capsys):
    study_uid = "1.2.826.0.1.3680043.9999.1"
    outputs = []
    for run, salt in enumerate(["salt-a", "salt-a", "salt-b"]):
        src = write_input(tmp_path / f"in{run}", study_uid=study_uid)
        out = tmp_path / f"anon{run}"
        assert dicom_anonymize.main(["--path", str(src), "--out", str(out), "--salt", salt]) == 0
        capsys.readouterr()
        files = sorted(out.rglob("*.dcm"))
        outputs.append(str(dcmread(str(files[0])).StudyInstanceUID))

    assert outputs[0] == outputs[1], "same salt must remap the same UID identically"
    assert outputs[0] != outputs[2], "different salt must produce a different remapped UID"
    assert outputs[0] != study_uid


def test_date_shift_consistent_and_salted():
    shifted_a1 = dicom_anonymize.hash_date_like_rsna("20240110", "PAT001", "salt-a")
    shifted_a2 = dicom_anonymize.hash_date_like_rsna("20240120", "PAT001", "salt-a")
    delta_original = 10
    d1 = datetime.strptime(shifted_a1, "%Y%m%d")
    d2 = datetime.strptime(shifted_a2, "%Y%m%d")
    assert (d2 - d1).days == delta_original, "per-patient shift must preserve intervals"

    shifted_b1 = dicom_anonymize.hash_date_like_rsna("20240110", "PAT001", "salt-b")
    assert shifted_b1 != shifted_a1, "salt must change the date shift"

    assert dicom_anonymize.hash_date_like_rsna("notadate", "PAT001", "salt-a") == dicom_anonymize.DEFAULT_DATE
    assert dicom_anonymize.hash_date_like_rsna("20240110123000", "PAT001", "salt-a").endswith("123000")


def test_mapping_file_is_private(tmp_path: Path, capsys):
    src = write_input(tmp_path)
    out = tmp_path / "anon"
    map_json = tmp_path / "map.json"
    assert run_anonymize(src, out, "--map-json", str(map_json)) == 0
    capsys.readouterr()

    assert map_json.exists()
    mode = stat.S_IMODE(os.stat(map_json).st_mode)
    assert mode == 0o600
    raw = json.loads(map_json.read_text(encoding="utf-8"))
    assert "PAT001" in raw["patients"]


def test_no_partial_output_left_behind(tmp_path: Path, capsys):
    src = write_input(tmp_path)
    out = tmp_path / "anon"
    assert run_anonymize(src, out) == 0
    capsys.readouterr()
    assert not [p for p in out.rglob("*") if p.name.endswith(".part")]
