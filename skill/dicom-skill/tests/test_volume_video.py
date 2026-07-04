from __future__ import annotations

import json
from pathlib import Path

import pytest

import dicom_volume_video
from dicom_volume_video import VideoInstance, VideoSeries


def make_series(modality: str, image_count: int) -> VideoSeries:
    instances = [
        VideoInstance(
            source=Path(f"/tmp/fake{i}.dcm"),
            modality=modality,
            study_uid="1.2.3",
            series_uid="1.2.3.4",
            sop_instance_uid=f"1.2.3.4.{i}",
            series_number="1",
            series_description="fake",
            instance_number=i,
            position_key=float(i),
            rows=32,
            columns=32,
            frame_count=1,
            pixel_spacing=(0.5, 0.5),
            slice_thickness=1.0,
            spacing_between_slices=None,
            photometric_interpretation="MONOCHROME2",
        )
        for i in range(image_count)
    ]
    return VideoSeries(modality=modality, study_uid="1.2.3", series_uid="1.2.3.4", instances=instances)


class PolicyArgs:
    large_series_min_images = 100
    mr_min_images = 15


@pytest.mark.parametrize(
    "modality,count,expected",
    [
        ("CT", 150, True),
        ("CT", 50, False),
        ("MR", 20, True),
        ("MR", 150, True),
        ("MR", 10, False),
        ("US", 500, False),  # non-CT/MR requires explicit selection
    ],
)
def test_automatic_policy(modality, count, expected):
    selected, _ = dicom_volume_video.automatic_policy(make_series(modality, count), PolicyArgs())
    assert selected is expected


def run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = dicom_volume_video.main(argv)
    return rc, json.loads(capsys.readouterr().out)


def test_streaming_axial_export(ct_series_dir: Path, tmp_path: Path, capsys):
    out = tmp_path / "videos"
    rc, result = run_cli(
        ["--path", str(ct_series_dir), "--out", str(out), "--series-number", "3", "--frame-rate", "4"],
        capsys,
    )
    assert rc == 0, result
    series_result = next(item for item in result["series"] if item.get("selected"))
    assert series_result["status"] == "exported"
    assert series_result["export_mode"] == "streaming_axial"
    assert series_result["windowing"] == "dicom_window"
    video = series_result["videos"][0]
    assert video["frame_count"] == 3
    assert Path(video["path"]).exists()
    assert not list(out.rglob("*.part.mp4"))


def test_full_volume_export_multi_plane(ct_series_dir: Path, tmp_path: Path, capsys):
    out = tmp_path / "videos"
    rc, result = run_cli(
        [
            "--path", str(ct_series_dir), "--out", str(out),
            "--series-number", "3", "--frame-rate", "4",
            "--plane", "axial,sagittal", "--no-window",
        ],
        capsys,
    )
    assert rc == 0, result
    series_result = next(item for item in result["series"] if item.get("selected"))
    assert series_result["status"] == "exported"
    assert series_result["export_mode"] == "full_volume"
    assert {v["plane"] for v in series_result["videos"]} == {"axial", "sagittal"}
    for video in series_result["videos"]:
        assert Path(video["path"]).exists()


def test_list_series(ct_series_dir: Path, tmp_path: Path, capsys):
    rc, result = run_cli(
        ["--path", str(ct_series_dir), "--out", str(tmp_path / "v"), "--list-series", "--include-descriptions"],
        capsys,
    )
    assert rc == 0
    assert result["series"][0]["status"] == "listed"
    assert result["series"][0]["image_count"] == 3
