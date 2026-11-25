"""Security-hardening regression tests (path safety, private audit files, salts, Orthanc)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import _common
import dicom_anonymize
import dicom_dimse
import orthanc_temp
from conftest import make_ct_dataset


# --- path components built from DICOM metadata ------------------------------


@pytest.mark.parametrize("hostile", [".", "..", "...", " .. ", "../..", "..\\..", "/", ""])
def test_safe_path_component_never_returns_a_traversal_name(hostile):
    cleaned = _common.safe_path_component(hostile, "fallback")
    assert cleaned not in {"", ".", ".."}
    assert "/" not in cleaned and "\\" not in cleaned
    assert cleaned.strip(".") != ""


def test_safe_path_component_keeps_normal_uids():
    assert _common.safe_path_component("1.2.840.113619.2.55", "x") == "1.2.840.113619.2.55"
    assert _common.safe_path_component("1.2.3.", "x") == "1.2.3"


def test_ensure_within_rejects_escape(tmp_path: Path):
    base = tmp_path / "out"
    base.mkdir()
    assert _common.ensure_within(base, base / "a" / "b.dcm") == (base / "a" / "b.dcm").resolve()
    with pytest.raises(ValueError, match="outside the output directory"):
        _common.ensure_within(base, base / ".." / "evil.dcm")


def test_received_instance_with_hostile_uids_stays_in_out_dir(tmp_path: Path):
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()
    ds = make_ct_dataset()
    ds.StudyInstanceUID = ".."
    ds.SeriesInstanceUID = "."
    ds.SOPInstanceUID = "../../../escape"
    dest = dicom_dimse.path_for_store(out_dir, ds, 1)
    assert out_dir.resolve() in dest.resolve().parents
    assert dest.parent.parent.name == "unknown_study"
    assert dest.parent.name == "unknown_series"


# --- audit JSON ---------------------------------------------------------------


def test_out_json_is_written_and_private(tmp_path: Path, capsys):
    target = tmp_path / "audit" / "result.json"
    _common.print_or_write({"PatientName": "DOE^JANE"}, str(target))
    capsys.readouterr()
    assert json.loads(target.read_text(encoding="utf-8")) == {"PatientName": "DOE^JANE"}
    if os.name != "nt":  # POSIX permission bits are not enforced on Windows
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --- anonymizer salt ------------------------------------------------------------


def anonymize(tmp_path: Path, name: str, *extra: str) -> tuple[int, Path]:
    src = tmp_path / f"in-{name}"
    src.mkdir(exist_ok=True)
    make_ct_dataset(
        study_uid="1.2.826.0.1.3680043.9999.5", series_uid="1.2.826.0.1.3680043.9999.5.1", sop_uid="1.2.826.0.1.3680043.9999.5.1.1"
    ).save_as(str(src / "a.dcm"), enforce_file_format=True)
    out = tmp_path / f"out-{name}"
    return dicom_anonymize.main(["--path", str(src), "--out", str(out), *extra]), out


def remapped_study_uid(out: Path) -> str:
    from pydicom import dcmread

    files = sorted(out.rglob("*.dcm"))
    assert len(files) == 1
    return str(dcmread(str(files[0]), stop_before_pixels=True).StudyInstanceUID)


def test_default_salt_is_random_secret_file_not_derived_from_parameters(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv(dicom_anonymize.CONFIG_DIR_ENV, str(tmp_path / "cfg-a"))
    rc, out_a = anonymize(tmp_path, "a")
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["salt_source"].endswith("(created)")
    assert result["deidentification_complete"] is False

    salt_files = list((tmp_path / "cfg-a" / "salts").glob("*.salt"))
    assert len(salt_files) == 1
    secret = salt_files[0].read_text(encoding="utf-8").strip()
    assert len(secret) == 64
    if os.name != "nt":
        assert stat.S_IMODE(salt_files[0].stat().st_mode) == 0o600

    # Same machine/project: the stored secret is reused, so output is stable.
    rc, out_a2 = anonymize(tmp_path, "a2")
    assert rc == 0
    assert "(created)" not in json.loads(capsys.readouterr().out)["salt_source"]
    assert remapped_study_uid(out_a) == remapped_study_uid(out_a2)

    # Another install with the same site/project parameters gets different pseudonyms.
    monkeypatch.setenv(dicom_anonymize.CONFIG_DIR_ENV, str(tmp_path / "cfg-b"))
    rc, out_b = anonymize(tmp_path, "b")
    capsys.readouterr()
    assert rc == 0
    assert remapped_study_uid(out_a) != remapped_study_uid(out_b)


def test_explicit_salt_file_and_missing_env(tmp_path: Path, monkeypatch, capsys):
    salt_file = tmp_path / "project.salt"
    rc, _ = anonymize(tmp_path, "f", "--salt-file", str(salt_file))
    assert rc == 0
    assert salt_file.exists()
    assert json.loads(capsys.readouterr().out)["salt_source"].startswith("file:")

    monkeypatch.delenv("DICOM_SKILL_TEST_UNSET_SALT", raising=False)
    rc, _ = anonymize(tmp_path, "e", "--salt-env", "DICOM_SKILL_TEST_UNSET_SALT")
    assert rc != 0


def test_dry_run_does_not_create_a_salt_file(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv(dicom_anonymize.CONFIG_DIR_ENV, str(tmp_path / "cfg"))
    rc, _ = anonymize(tmp_path, "d", "--dry-run")
    capsys.readouterr()
    assert rc == 0
    assert not (tmp_path / "cfg").exists()


# --- temporary Orthanc ----------------------------------------------------------


def test_orthanc_config_is_receive_only(tmp_path: Path):
    config_path = tmp_path / "orthanc.json"
    orthanc_temp.write_config(config_path, "AGENT", "agent", "pw")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["DicomAlwaysAllowEcho"] is True
    assert config["DicomAlwaysAllowStore"] is True
    assert config["DicomAlwaysAllowFind"] is False
    assert config["DicomAlwaysAllowGet"] is False
    assert config["DicomAlwaysAllowMove"] is False
    assert config["DicomCheckCalledAet"] is True
    assert "DicomModalities" not in config


def test_orthanc_config_can_restrict_calling_aets(tmp_path: Path):
    config_path = tmp_path / "orthanc.json"
    orthanc_temp.write_config(config_path, "AGENT", "agent", "pw", ["PACS@192.0.2.9", "ARCHIVE@192.0.2.10"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["DicomAlwaysAllowStore"] is False
    assert config["DicomAlwaysAllowEcho"] is False
    assert config["DicomCheckModalityHost"] is True
    assert [(entry["AET"], entry["Host"]) for entry in config["DicomModalities"].values()] == [
        ("PACS", "192.0.2.9"),
        ("ARCHIVE", "192.0.2.10"),
    ]
    with pytest.raises(ValueError, match="Invalid AE title"):
        orthanc_temp.parse_allowed_aets(["THIS AE TITLE IS FAR TOO LONG"])
    with pytest.raises(ValueError, match="Invalid host"):
        orthanc_temp.parse_allowed_aets(["PACS@"])


def test_orthanc_allowed_modalities_are_store_only(tmp_path: Path):
    # Orthanc gives a registered modality every permission by default, even
    # when the global DicomAlwaysAllow* options are false.
    config_path = tmp_path / "orthanc.json"
    orthanc_temp.write_config(config_path, "AGENT", "agent", "pw", ["PACS", "ARCHIVE"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["DicomCheckModalityHost"] is False
    entries = list(config["DicomModalities"].values())
    assert [entry["AET"] for entry in entries] == ["PACS", "ARCHIVE"]
    for entry in entries:
        assert isinstance(entry, dict)
        assert entry["AllowEcho"] is True
        assert entry["AllowStore"] is True
        for denied in ("AllowFind", "AllowGet", "AllowMove", "AllowStorageCommitment"):
            assert entry[denied] is False


def test_orthanc_rejects_mixed_aet_and_host_entries(tmp_path: Path):
    # Host checking is global, so a plain AET next to AET@HOST could never match.
    with pytest.raises(ValueError, match="mix AET and AET@HOST"):
        orthanc_temp.parse_allowed_aets(["PACS", "ARCHIVE@192.0.2.10"])
    config_path = tmp_path / "orthanc.json"
    with pytest.raises(ValueError, match="mix AET and AET@HOST"):
        orthanc_temp.write_config(config_path, "AGENT", "agent", "pw", ["PACS", "ARCHIVE@192.0.2.10"])
    assert not config_path.exists()


def test_orthanc_public_state_omits_password():
    state = orthanc_temp.OrthancState(
        name="n", aet="AGENT", dicom_port=4242, http_port=8042, http_url="http://127.0.0.1:8042",
        data_dir="/tmp/x", config_path="/tmp/x/orthanc.json", image=orthanc_temp.DEFAULT_IMAGE,
        http_user="agent", http_password="s3cret-value",
    )
    public = state.to_public_dict()
    assert "http_password" not in public
    assert public["http_password_saved"] is True
    assert "s3cret-value" not in json.dumps(public)
    # The private state file still needs it for later status/export calls.
    assert state.to_dict()["http_password"] == "s3cret-value"


def test_orthanc_image_is_pinned():
    assert not orthanc_temp.DEFAULT_IMAGE.endswith(":latest")
    assert dicom_dimse.DEFAULT_ORTHANC_IMAGE == orthanc_temp.DEFAULT_IMAGE
