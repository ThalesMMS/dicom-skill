"""Tests for the rclone study backup. No network, rclone, or DICOM node needed."""

from __future__ import annotations

import json
import os
import stat
import zipfile
from collections import Counter
from pathlib import Path

import pytest
from pydicom import dcmread
from pydicom.uid import ExplicitVRLittleEndian, JPEG2000Lossless

import dicom_cloud_backup as backup
from conftest import make_ct_dataset

REMOTE_ROOT = "testremote:dicom-backup"
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")


def make_config(tmp_path: Path, remote_root: str = REMOTE_ROOT) -> backup.BackupConfig:
    return backup.BackupConfig(
        dicom_config=tmp_path / "nodes.yaml",
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        remote_root=remote_root,
        rclone="rclone",
        python="python",
        dimse_script=tmp_path / "dicom_dimse.py",
        backfill_pause_bytes=0,
        all_pause_bytes=0,
    )


def test_date_filters_must_be_bounded():
    assert backup.validate_date_filter("20240115") == "20240115"
    assert backup.validate_date_filter("20200101-20240114") == "20200101-20240114"
    with pytest.raises(backup.BackupError, match="Open-ended"):
        backup.validate_date_filter("20200101-")
    with pytest.raises(backup.BackupError, match="Open-ended"):
        backup.validate_date_filter("-20240114")
    with pytest.raises(backup.BackupError, match="starts after"):
        backup.validate_date_filter("20240115-20200101")


def test_remote_path_has_date_and_no_node_prefix(tmp_path: Path):
    runner = backup.BackupRunner(make_config(tmp_path))
    study = backup.Study("primary", "1.2.3.4", "20200108", "120000")
    assert runner.remote_path(study) == f"{REMOTE_ROOT}/2020/01/08/1.2.3.4.zip"


def test_state_database_is_resumable(tmp_path: Path):
    db = backup.StateDB(tmp_path / "state" / "state.sqlite3")
    study = backup.Study("primary", "1.2.3", "20240115", "010203", 2)
    db.discover(study, "daily")
    db.mark(study, "retrieving", expected_instances=2)
    db.mark(study, "retrieving", expected_instances=2)
    db.mark(
        study,
        "completed",
        received_instances=2,
        archive_size=100,
        archive_sha1="a" * 40,
        remote_path=f"{REMOTE_ROOT}/2024/01/15/1.2.3.zip",
        completed_at=backup.utc_now(),
    )
    row = db.get(study.node, study.study_uid)
    assert row is not None
    assert row["status"] == "completed"
    assert row["attempts"] == 1
    db.set_metadata("backfill_cursor_date:primary", "20200101")
    summary = db.summary()
    assert summary["coverage"] == [
        {"node": "primary", "study_date": "20240115", "status": "completed", "count": 1}
    ]
    assert summary["metadata"]["backfill_cursor_date:primary"]["value"] == "20200101"


@posix_only
def test_state_database_is_owner_only(tmp_path: Path):
    db = backup.StateDB(tmp_path / "state" / "state.sqlite3")
    assert stat.S_IMODE(db.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.path.parent.stat().st_mode) == 0o700


def test_archive_is_zip64_stored_and_contains_only_study_files(tmp_path: Path):
    runner = backup.BackupRunner(make_config(tmp_path))
    study = backup.Study("primary", "1.2.3", "20200108")
    source = tmp_path / "source.dcm"
    source.write_bytes(b"DICOM-BYTES")
    records = [
        {
            "path": str(source),
            "series_uid": "1.2.3.1",
            "sop_uid": "1.2.3.1.1",
            "sop_class_uid": "1.2.840.10008.5.1.4.1.1.2",
            "transfer_syntax_uid": "1.2.840.10008.1.2.4.91",
        }
    ]
    archive = tmp_path / "1.2.3.zip"
    size, digest = runner.create_archive(
        study,
        records,
        Counter({"1.2.840.10008.1.2.4.91": 1}),
        Counter({"1.2.840.10008.5.1.4.1.1.2": 1}),
        archive,
    )

    assert size == archive.stat().st_size
    assert digest == backup.sha1_file(archive)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        assert bundle.namelist() == ["DICOM/1.2.3.1/1.2.3.1.1.dcm", "manifest.json"]
        assert all(item.compress_type == zipfile.ZIP_STORED for item in bundle.infolist())
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["study_instance_uid"] == "1.2.3"
        assert manifest["instance_count"] == 1
        # The manifest is PHI-light: technical identifiers only.
        assert "PatientName" not in json.dumps(manifest)


def dicom_record(path: Path) -> dict[str, object]:
    ds = dcmread(path, defer_size="1 KB")
    return {
        "path": str(path),
        "series_uid": str(ds.SeriesInstanceUID),
        "sop_uid": str(ds.SOPInstanceUID),
        "sop_class_uid": str(ds.SOPClassUID),
        "transfer_syntax_uid": str(ds.file_meta.TransferSyntaxUID),
        "has_pixel_data": "PixelData" in ds,
    }


def test_uncompressed_image_majority_is_transcoded_losslessly(tmp_path: Path):
    runner = backup.BackupRunner(make_config(tmp_path))
    study = backup.Study("primary", "1.2.3.4", "20200108")
    records = []
    original_uids = []
    for index in range(2):
        ds = make_ct_dataset(study_uid=study.study_uid, instance_number=index + 1)
        path = tmp_path / f"image-{index}.dcm"
        ds.save_as(path, enforce_file_format=True)
        original_uids.append(str(ds.SOPInstanceUID))
        records.append(dicom_record(path))

    prepared, syntaxes, _, report = runner.apply_compression_policy(study, records)

    assert report["triggered"] is True
    assert report["transcoded_instances"] == 2
    assert report["prepared_uncompressed_pixel_instances"] == 0
    assert syntaxes == Counter({str(JPEG2000Lossless): 2})
    assert [str(dcmread(item["path"], stop_before_pixels=True).SOPInstanceUID) for item in prepared] == original_uids


def test_uncompressed_images_are_preserved_without_strict_majority(tmp_path: Path):
    runner = backup.BackupRunner(make_config(tmp_path))
    study = backup.Study("primary", "1.2.3.5", "20240115")
    uncompressed = make_ct_dataset(study_uid=study.study_uid, instance_number=1)
    uncompressed_path = tmp_path / "uncompressed.dcm"
    uncompressed.save_as(uncompressed_path, enforce_file_format=True)
    compressed = make_ct_dataset(study_uid=study.study_uid, instance_number=2)
    compressed.compress(JPEG2000Lossless, generate_instance_uid=False)
    compressed_path = tmp_path / "compressed.dcm"
    compressed.save_as(compressed_path, enforce_file_format=True)

    records = [dicom_record(uncompressed_path), dicom_record(compressed_path)]
    _, syntaxes, _, report = runner.apply_compression_policy(study, records)

    assert report["triggered"] is False
    assert report["transcoded_instances"] == 0
    assert syntaxes[ExplicitVRLittleEndian] == 1
    assert syntaxes[JPEG2000Lossless] == 1


def test_default_disk_floors():
    args = backup.build_parser().parse_args(["status"])
    assert args.min_free_gib == 30.0
    assert args.backfill_min_free_gib == 40.0


def test_no_site_specific_destination_is_built_in(monkeypatch):
    monkeypatch.delenv(backup.REMOTE_ROOT_ENV, raising=False)
    args = backup.build_parser().parse_args(["status"])
    assert args.remote_root == ""
    with pytest.raises(backup.BackupError, match="No destination configured"):
        backup.require_remote_root(args.remote_root)
    with pytest.raises(backup.BackupError, match="rclone remote path"):
        backup.require_remote_root("/local/path")
    assert backup.require_remote_root(REMOTE_ROOT) == REMOTE_ROOT


def test_lane_lock_rejects_second_worker(tmp_path: Path):
    runner = backup.BackupRunner(make_config(tmp_path))
    other = backup.BackupRunner(make_config(tmp_path))
    with runner.lane_lock("daily"):
        with pytest.raises(backup.BackupError, match="already running"):
            with other.lane_lock("daily"):
                pass
        # A different lane is independent.
        with other.lane_lock("backfill"):
            pass
    with other.lane_lock("daily"):
        pass


def test_backfill_walks_bounded_windows_and_resumes(tmp_path: Path, monkeypatch):
    runner = backup.BackupRunner(make_config(tmp_path))
    queries: list[str] = []
    studies = {
        "20200110": backup.Study("primary", "1.2.1", "20200110"),
        "20200220": backup.Study("primary", "1.2.2", "20200220"),
    }

    def fake_query(node: str, date_filter: str) -> list[backup.Study]:
        backup.validate_date_filter(date_filter)
        queries.append(date_filter)
        start, end = date_filter.split("-")
        return [s for d, s in sorted(studies.items()) if start <= d <= end]

    processed: list[str] = []

    def fake_process(study: backup.Study, lane: str) -> dict[str, object]:
        runner.db.discover(study, lane)
        runner.db.mark(study, "completed")
        processed.append(study.study_uid)
        return {"status": "completed"}

    monkeypatch.setattr(runner, "query_studies", fake_query)
    monkeypatch.setattr(runner, "process_study", fake_process)

    # A capped first run stops before the second study and keeps the cursor.
    assert len(runner.run_backfill("primary", "20200101", "20200229", max_studies=1)) == 1
    assert processed == ["1.2.1"]
    assert queries == ["20200101-20200131", "20200201-20200229"]
    assert runner.db.metadata("backfill_cursor_date:primary", "") == "20200201"

    # The next run resumes from the stored cursor and closes the range.
    queries.clear()
    assert len(runner.run_backfill("primary", "20200101", "20200229", max_studies=None)) == 1
    assert processed == ["1.2.1", "1.2.2"]
    assert queries == ["20200201-20200229"]
    assert runner.db.metadata("backfill_cursor_date:primary", "") == "20200301"

    # --rescan re-walks the range but completed studies are still skipped.
    queries.clear()
    assert runner.run_backfill("primary", "20200101", "20200229", max_studies=None, rescan=True) == []
    assert queries == ["20200101-20200131", "20200201-20200229"]


def test_cli_rejects_unbounded_backfill_and_bad_node(tmp_path: Path, capsys):
    base = ["--state-dir", str(tmp_path / "state"), "--work-dir", str(tmp_path / "work"), "--remote-root", REMOTE_ROOT]
    assert backup.main([*base, "run", "--lane", "backfill", "--node", "primary", "--start", "20200101"]) == 1
    assert "closed range" in capsys.readouterr().out
    assert backup.main([*base, "run", "--lane", "daily", "--node", "bad node", "--date", "20240115"]) == 1
    assert "Invalid node name" in capsys.readouterr().out
    assert backup.main([*base, "run", "--lane", "daily", "--node", "primary", "--start", "20200101"]) == 1
    assert "only to the backfill lane" in capsys.readouterr().out
