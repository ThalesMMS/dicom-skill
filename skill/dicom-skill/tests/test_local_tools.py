from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from pydicom import dcmread
from pydicom.uid import JPEG2000Lossless

import dicom_jpeg2000
import dicom_pdf
import dicom_preview
from conftest import make_ct_dataset


def run_cli(module, argv: list[str], capsys) -> dict:
    rc = module.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_jpeg2000_lossless_round_trip(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    original = make_ct_dataset()
    original.save_as(str(src / "ct.dcm"), enforce_file_format=True)

    j2k = tmp_path / "j2k"
    rc, result = run_cli(dicom_jpeg2000, ["compress", "--path", str(src), "--out", str(j2k)], capsys)
    assert rc == 0 and result["ok"]
    compressed_file = next(j2k.rglob("*.dcm"))
    compressed = dcmread(str(compressed_file))
    assert compressed.file_meta.TransferSyntaxUID == JPEG2000Lossless
    assert compressed.SOPInstanceUID != original.SOPInstanceUID  # new UID by default

    restored_dir = tmp_path / "restored"
    rc, result = run_cli(dicom_jpeg2000, ["decompress", "--path", str(j2k), "--out", str(restored_dir)], capsys)
    assert rc == 0 and result["ok"]
    restored = dcmread(str(next(restored_dir.rglob("*.dcm"))))
    np.testing.assert_array_equal(restored.pixel_array, original.pixel_array)


def test_preview_renders_png(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    make_ct_dataset().save_as(str(src / "ct.dcm"), enforce_file_format=True)

    out = tmp_path / "previews"
    rc, result = run_cli(dicom_preview, ["--path", str(src), "--out", str(out), "--max-size", "16"], capsys)
    assert rc == 0 and result["ok"]
    pngs = list(out.rglob("*.png"))
    assert len(pngs) == 1
    assert result["items"][0]["pngs"][0]["width"] <= 16


def test_pdf_dicomizer(tmp_path: Path, capsys):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4\n1 0 obj\nendobj\ntrailer\n%%EOF")  # odd length on purpose

    out = tmp_path / "dicomized"
    rc, result = run_cli(
        dicom_pdf,
        ["--pdf", str(pdf), "--out", str(out), "--patient-id", "TEST123", "--patient-name", "Test^Patient"],
        capsys,
    )
    assert rc == 0 and result["ok"]
    ds = dcmread(str(next(out.rglob("*.dcm"))))
    assert str(ds.SOPClassUID) == "1.2.840.10008.5.1.4.1.1.104.1"
    assert ds.MIMETypeOfEncapsulatedDocument == "application/pdf"
    assert ds.BurnedInAnnotation == "YES"
    assert len(ds.EncapsulatedDocument) % 2 == 0
    assert bytes(ds.EncapsulatedDocument).startswith(b"%PDF-")


def test_pdf_rejects_non_pdf(tmp_path: Path, capsys):
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a pdf")
    out = tmp_path / "out"
    rc, result = run_cli(dicom_pdf, ["--pdf", str(fake), "--out", str(out)], capsys)
    assert rc == 1
    assert result["items"][0]["status"] == "error"
