"""Shared fixtures for dicom-skill tests.

All tests run against synthetic DICOM datasets generated with pydicom; no real
patient data is used or required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid


def make_ct_dataset(
    *,
    patient_id: str = "PAT001",
    patient_name: str = "Doe^Jane",
    study_uid: str | None = None,
    series_uid: str | None = None,
    sop_uid: str | None = None,
    rows: int = 32,
    columns: int = 32,
    instance_number: int = 1,
    study_date: str = "20240115",
    accession: str = "ACC123",
    window: tuple[float, float] | None = (40.0, 400.0),
    slice_position: float = 0.0,
) -> FileDataset:
    """Build a minimal but standards-plausible CT image instance."""
    sop_uid = sop_uid or generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "F"
    ds.AccessionNumber = accession
    ds.StudyInstanceUID = study_uid or generate_uid()
    ds.SeriesInstanceUID = series_uid or generate_uid()
    ds.StudyDate = study_date
    ds.SeriesDate = study_date
    ds.StudyTime = "101500"
    ds.StudyDescription = "TEST CT STUDY"
    ds.SeriesDescription = "TEST AXIAL"
    ds.SeriesNumber = 3
    ds.Modality = "CT"
    ds.InstanceNumber = instance_number
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [0.0, 0.0, float(slice_position)]
    ds.PixelSpacing = [0.5, 0.5]
    ds.SliceThickness = 1.0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = rows
    ds.Columns = columns
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.RescaleIntercept = "-1024"
    ds.RescaleSlope = "1"
    if window is not None:
        ds.WindowCenter = str(window[0])
        ds.WindowWidth = str(window[1])

    # Deterministic gradient with a bright square so windowing has structure.
    arr = np.linspace(900, 1300, rows * columns, dtype=np.float64).reshape(rows, columns)
    arr[rows // 4 : rows // 2, columns // 4 : columns // 2] = 2000
    ds.PixelData = arr.astype(np.uint16).tobytes()
    return ds


@pytest.fixture
def ct_dataset() -> FileDataset:
    return make_ct_dataset()


@pytest.fixture
def ct_series_dir(tmp_path: Path) -> Path:
    """A 3-instance CT series written to disk as a study folder."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    src = tmp_path / "study"
    src.mkdir()
    for index in range(1, 4):
        ds = make_ct_dataset(
            study_uid=study_uid,
            series_uid=series_uid,
            instance_number=index,
            slice_position=float(index),
        )
        ds.save_as(str(src / f"slice{index:03d}.dcm"), enforce_file_format=True)
    return src
