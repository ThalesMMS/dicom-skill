#!/usr/bin/env python3
"""Create DICOM Encapsulated PDF instances from local PDF files.

This utility only reads local PDF files and writes DICOM files. It does not
connect to DICOM nodes and does not anonymize PDF contents.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from pydicom import dcmread
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import EncapsulatedPDFStorage, ExplicitVRLittleEndian, UID, generate_uid
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": f"pydicom is required: {exc}"}), file=sys.stderr)
    raise

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _common import (
    print_or_write as _print_or_write,
    safe_path_component as safe_component,
    save_dataset_atomic,
    value_to_plain,
)


DEFAULT_MODALITY = "DOC"
DEFAULT_MANUFACTURER = "dicom-skill"

ARG_TO_DICOM_KEY = {
    "accession_number": "AccessionNumber",
    "institution_name": "InstitutionName",
    "issuer_of_patient_id": "IssuerOfPatientID",
    "patient_birth_date": "PatientBirthDate",
    "patient_id": "PatientID",
    "patient_name": "PatientName",
    "patient_sex": "PatientSex",
    "referring_physician_name": "ReferringPhysicianName",
    "series_instance_uid": "SeriesInstanceUID",
    "study_date": "StudyDate",
    "study_id": "StudyID",
    "study_instance_uid": "StudyInstanceUID",
    "study_time": "StudyTime",
}


@dataclass(frozen=True)
class InputPDF:
    source: Path
    relative_output: Path


def pdf_like(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def discover_pdfs(paths: list[str], *, max_files: int | None = None) -> list[InputPDF]:
    discovered: list[InputPDF] = []
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
            if candidate.suffix.lower() == ".pdf" or pdf_like(candidate):
                discovered.append(InputPDF(candidate, relative_output.with_suffix(".dcm")))
            if max_files is not None and len(discovered) >= max_files:
                return discovered
    return discovered


def read_metadata_source(path: str | None, *, force: bool = False) -> dict[str, Any]:
    if not path:
        return {}
    ds = dcmread(str(Path(path).expanduser().resolve()), stop_before_pixels=True, force=force)
    keys = [
        "PatientName",
        "PatientID",
        "PatientBirthDate",
        "PatientSex",
        "StudyInstanceUID",
        "StudyDate",
        "StudyTime",
        "StudyID",
        "AccessionNumber",
        "ReferringPhysicianName",
        "IssuerOfPatientID",
        "InstitutionName",
    ]
    return {key: getattr(ds, key) for key in keys if getattr(ds, key, None) not in (None, "")}


def get_text(args: argparse.Namespace, metadata: dict[str, Any], attr: str, default: str = "") -> str:
    cli_value = getattr(args, attr, None)
    if cli_value is not None:
        return str(cli_value)
    dicom_key = ARG_TO_DICOM_KEY[attr]
    return str(metadata.get(dicom_key, default) or default)


def get_uid(args: argparse.Namespace, metadata: dict[str, Any], attr: str, default: UID | None = None) -> UID:
    cli_value = getattr(args, attr, None)
    if cli_value:
        return UID(str(cli_value))
    dicom_key = ARG_TO_DICOM_KEY[attr]
    value = metadata.get(dicom_key)
    if value:
        return UID(str(value))
    if default:
        return default
    return generate_uid(prefix=args.uid_prefix)


def output_path(out_dir: Path, item: InputPDF, sop_instance_uid: UID, args: argparse.Namespace) -> Path:
    if args.flat:
        stem = safe_component(item.source.stem, "document")
        return out_dir / f"{stem}_{sop_instance_uid}.dcm"
    return out_dir / item.relative_output


def set_optional(ds: FileDataset, key: str, value: Any) -> None:
    if value not in (None, ""):
        setattr(ds, key, value)


def build_dataset(
    *,
    pdf_bytes: bytes,
    item: InputPDF,
    args: argparse.Namespace,
    metadata: dict[str, Any],
    series_instance_uid: UID,
    instance_number: int,
    now: datetime,
) -> FileDataset:
    sop_instance_uid = generate_uid(prefix=args.uid_prefix)
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = EncapsulatedPDFStorage
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid(prefix=args.uid_prefix)
    file_meta.ImplementationVersionName = "DICOMSKILL_PDF"

    ds = FileDataset("", {}, file_meta=file_meta, preamble=b"\0" * 128)

    content_date = args.content_date or now.strftime("%Y%m%d")
    content_time = args.content_time or now.strftime("%H%M%S")
    document_title = args.document_title or item.source.stem

    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.SOPClassUID = EncapsulatedPDFStorage
    ds.SOPInstanceUID = sop_instance_uid
    ds.Modality = args.modality
    ds.InstanceNumber = instance_number
    ds.ContentDate = content_date
    ds.ContentTime = content_time
    ds.AcquisitionDateTime = f"{content_date}{content_time}"

    ds.PatientName = get_text(args, metadata, "patient_name")
    ds.PatientID = get_text(args, metadata, "patient_id")
    set_optional(ds, "IssuerOfPatientID", get_text(args, metadata, "issuer_of_patient_id"))
    set_optional(ds, "PatientBirthDate", get_text(args, metadata, "patient_birth_date"))
    set_optional(ds, "PatientSex", get_text(args, metadata, "patient_sex"))

    ds.StudyInstanceUID = get_uid(args, metadata, "study_instance_uid")
    ds.StudyDate = get_text(args, metadata, "study_date", content_date)
    ds.StudyTime = get_text(args, metadata, "study_time", content_time)
    ds.AccessionNumber = get_text(args, metadata, "accession_number")
    ds.StudyID = get_text(args, metadata, "study_id")
    ds.ReferringPhysicianName = get_text(args, metadata, "referring_physician_name")

    ds.SeriesInstanceUID = series_instance_uid
    ds.SeriesNumber = args.series_number
    ds.Manufacturer = args.manufacturer
    set_optional(ds, "InstitutionName", get_text(args, metadata, "institution_name"))

    ds.DocumentTitle = document_title
    ds.MIMETypeOfEncapsulatedDocument = "application/pdf"
    ds.EncapsulatedDocument = pdf_bytes if len(pdf_bytes) % 2 == 0 else pdf_bytes + b"\0"
    ds.BurnedInAnnotation = args.burned_in_annotation

    return ds


def write_dataset(ds: FileDataset, dest: Path) -> None:
    save_dataset_atomic(ds, dest)


def dicomize_one(
    item: InputPDF,
    args: argparse.Namespace,
    out_dir: Path,
    metadata: dict[str, Any],
    series_instance_uid: UID,
    instance_number: int,
    now: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": str(item.source),
        "status": "pending",
        "source_bytes": item.source.stat().st_size,
    }
    try:
        pdf_bytes = item.source.read_bytes()
        if not args.allow_non_pdf and not pdf_bytes.startswith(b"%PDF-"):
            raise ValueError("Input does not start with %PDF-. Use --allow-non-pdf to override.")
        ds = build_dataset(
            pdf_bytes=pdf_bytes,
            item=item,
            args=args,
            metadata=metadata,
            series_instance_uid=series_instance_uid,
            instance_number=instance_number,
            now=now,
        )
        dest = output_path(out_dir, item, ds.SOPInstanceUID, args)
        result.update(
            {
                "destination": str(dest),
                "sop_class_uid": str(ds.SOPClassUID),
                "sop_instance_uid": str(ds.SOPInstanceUID),
                "study_instance_uid": str(ds.StudyInstanceUID),
                "series_instance_uid": str(ds.SeriesInstanceUID),
                "document_title": value_to_plain(ds.DocumentTitle),
                "burned_in_annotation": value_to_plain(ds.BurnedInAnnotation),
            }
        )
        if dest.exists() and not args.overwrite and not args.dry_run:
            raise FileExistsError(f"Destination exists: {dest}. Use --overwrite to replace it.")
        if args.dry_run:
            result["status"] = "planned"
            return result
        write_dataset(ds, dest)
        result["status"] = "converted"
        result["destination_bytes"] = dest.stat().st_size
        return result
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["error"] = str(exc)
        return result


def command_summary(result: dict[str, Any]) -> dict[str, Any]:
    items = result.get("items") or []
    return {
        "operation": result.get("operation"),
        "out_dir": result.get("out_dir"),
        "file_count": result.get("file_count"),
        "converted_count": sum(1 for item in items if item.get("status") == "converted"),
        "planned_count": sum(1 for item in items if item.get("status") == "planned"),
        "error_count": sum(1 for item in items if item.get("status") == "error"),
        "ok": result.get("ok"),
    }


def print_or_write(result: dict[str, Any], out_json: str | None = None, summary: bool = False) -> None:
    _print_or_write(result, out_json, command_summary if summary else None)


def run_command(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out).expanduser().resolve()
    metadata = read_metadata_source(args.metadata_from, force=args.force)
    if not args.study_instance_uid and not metadata.get("StudyInstanceUID"):
        metadata["StudyInstanceUID"] = generate_uid(prefix=args.uid_prefix)
    series_instance_uid = get_uid(args, metadata, "series_instance_uid")
    files = discover_pdfs(args.pdf, max_files=args.max_files)
    now = datetime.now()
    result: dict[str, Any] = {
        "operation": "PDF-DICOMIZE",
        "out_dir": str(out_dir),
        "file_count": len(files),
        "dry_run": bool(args.dry_run),
        "metadata_from": args.metadata_from,
        "files": [str(item.source) for item in files] if args.include_files else None,
        "items": [],
    }
    if not files:
        result["ok"] = False
        result["error"] = "No PDF files found."
        return result
    for offset, item in enumerate(files, start=0):
        result["items"].append(
            dicomize_one(
                item,
                args,
                out_dir,
                metadata,
                series_instance_uid,
                args.instance_number + offset,
                now,
            )
        )
    result["ok"] = not any(item.get("status") == "error" for item in result["items"])
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create DICOM Encapsulated PDF instances from local PDF files.")
    parser.add_argument("--pdf", action="append", required=True, help="PDF file or directory; can be repeated")
    parser.add_argument("--out", required=True, help="Output directory for DICOM Encapsulated PDF files")
    parser.add_argument("--metadata-from", help="Optional DICOM file to copy patient/study metadata from")
    parser.add_argument("--patient-name")
    parser.add_argument("--patient-id")
    parser.add_argument("--issuer-of-patient-id")
    parser.add_argument("--patient-birth-date")
    parser.add_argument("--patient-sex")
    parser.add_argument("--study-instance-uid")
    parser.add_argument("--study-date")
    parser.add_argument("--study-time")
    parser.add_argument("--study-id")
    parser.add_argument("--accession-number")
    parser.add_argument("--referring-physician-name")
    parser.add_argument("--institution-name")
    parser.add_argument("--series-instance-uid")
    parser.add_argument("--series-number", type=int, default=999)
    parser.add_argument("--instance-number", type=int, default=1)
    parser.add_argument("--modality", default=DEFAULT_MODALITY)
    parser.add_argument("--manufacturer", default=DEFAULT_MANUFACTURER)
    parser.add_argument("--document-title", help="DocumentTitle. Defaults to each PDF filename stem")
    parser.add_argument("--content-date", help="ContentDate as YYYYMMDD. Defaults to current date")
    parser.add_argument("--content-time", help="ContentTime as HHMMSS. Defaults to current time")
    parser.add_argument("--burned-in-annotation", choices=["YES", "NO"], default="YES")
    parser.add_argument("--uid-prefix", default="1.2.826.0.1.3680043.8.498.")
    parser.add_argument("--flat", action="store_true", help="Do not preserve input directory structure under --out")
    parser.add_argument("--allow-non-pdf", action="store_true", help="Allow inputs that do not start with %%PDF-")
    parser.add_argument("--force", action="store_true", help="Force pydicom read of --metadata-from")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing destination files")
    parser.add_argument("--dry-run", action="store_true", help="Plan DICOM outputs without writing files")
    parser.add_argument("--include-files", action="store_true", help="Include discovered source file paths in JSON output")
    parser.add_argument("--max-files", type=int, help="Maximum number of PDF files to process")
    parser.add_argument("--out-json", help="Write full JSON result to this file as well as stdout")
    parser.add_argument("--summary", action="store_true", help="Print concise PHI-light summary to stdout; --out-json still stores full JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_command(args)
        print_or_write(result, args.out_json, args.summary)
        return 0 if result.get("ok") else 1
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
