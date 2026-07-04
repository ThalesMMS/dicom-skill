#!/usr/bin/env python3
"""Local DICOM anonymization CLI for dicom-skill.

This utility implements a lightweight, agent-friendly anonymization workflow
inspired by the RSNA DICOM Anonymizer. It uses the RSNA default XML script as a
retention/operation table for DICOM data elements, without starting the RSNA GUI
application and without requiring its OCR/neural-network stack.

It anonymizes local DICOM files or folders. It does not query, retrieve, send,
delete, or modify remote DICOM nodes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    import pydicom
    from pydicom import dcmread
    from pydicom.dataelem import DataElement
    from pydicom.dataset import Dataset, FileDataset
    from pydicom.errors import InvalidDicomError
    from pydicom.multival import MultiValue
    from pydicom.sequence import Sequence
    from pydicom.tag import BaseTag, Tag
    from pydicom.uid import ExplicitVRLittleEndian, UID
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": f"pydicom is required: {exc}"}), file=sys.stderr)
    raise

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_RSNA_SCRIPT = SKILL_DIR / "resources" / "rsna" / "default-anonymizer.script"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _common import (
    discover_dicom_files,
    print_or_write as _print_or_write,
    safe_path_component as sanitize_component,
    save_dataset_atomic,
    write_private_text,
)

DEFAULT_SITE_ID = "999999"
DEFAULT_PROJECT_NAME = "dicom-skill"
DEFAULT_UID_ROOT = "2.25"
DEFAULT_DEIDENTIFICATION_METHOD = "RSNA DICOM ANONYMIZER DERIVED WORKFLOW"
DEFAULT_DATE = "20000101"
DICOM_UID_MAX_LEN = 64

DEIDENTIFICATION_METHOD_CODES: list[tuple[str, str]] = [
    ("113100", "Basic Application Confidentiality Profile"),
    ("113107", "Retain Longitudinal Temporal Information Modified Dates Option"),
    ("113108", "Retain Patient Characteristics Option"),
]

UID_KEYWORDS = {
    "SOPInstanceUID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "FrameOfReferenceUID",
    "SynchronizationFrameOfReferenceUID",
    "DimensionOrganizationUID",
    "ConcatenationUID",
    "IrradiationEventUID",
    "TransactionUID",
    "CreatorVersionUID",
}

REQUIRED_ATTRIBUTES = ["SOPClassUID", "SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID"]


def tag_to_hex(tag: BaseTag | int | str) -> str:
    t = Tag(tag)
    return f"{int(t):08X}"


def readable_dicom_files(paths: list[str], *, force: bool = False, max_files: int | None = None) -> list[Path]:
    return [item.source for item in discover_dicom_files(paths, force=force, max_files=max_files)]


@dataclass
class ScriptRules:
    script_path: Path
    tag_keep: dict[str, str]
    remove_private_tags: bool = True
    remove_curves: bool = True
    remove_overlays: bool = True

    @classmethod
    def load(cls, script_path: Path) -> "ScriptRules":
        if not script_path.exists():
            raise FileNotFoundError(f"Anonymizer script not found: {script_path}")
        root = ET.parse(script_path).getroot()
        keep: dict[str, str] = {}
        for elem in root.findall("e"):
            tag = str(elem.attrib.get("t", "")).strip().upper()
            operation = (elem.text or "").strip()
            if not tag:
                continue
            if "@remove" not in operation:
                keep[tag] = operation
        remove_private = True
        remove_curves = True
        remove_overlays = True
        for rule in root.findall("r"):
            name = str(rule.attrib.get("t", "")).strip().lower()
            enabled = str(rule.attrib.get("en", "T")).upper() == "T"
            if name == "privategroups":
                remove_private = enabled
            elif name == "curves":
                remove_curves = enabled
            elif name == "overlays":
                remove_overlays = enabled
        return cls(
            script_path=script_path,
            tag_keep=keep,
            remove_private_tags=remove_private,
            remove_curves=remove_curves,
            remove_overlays=remove_overlays,
        )


@dataclass
class MappingState:
    site_id: str
    uid_root: str
    project_name: str
    patient_id_strategy: str
    patients: dict[str, str] = field(default_factory=dict)
    uids: dict[str, str] = field(default_factory=dict)
    accessions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, *, site_id: str, uid_root: str, project_name: str, patient_id_strategy: str) -> "MappingState":
        if not path.exists():
            return cls(site_id=site_id, uid_root=uid_root, project_name=project_name, patient_id_strategy=patient_id_strategy)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            site_id=str(raw.get("site_id") or site_id),
            uid_root=str(raw.get("uid_root") or uid_root),
            project_name=str(raw.get("project_name") or project_name),
            patient_id_strategy=str(raw.get("patient_id_strategy") or patient_id_strategy),
            patients={str(k): str(v) for k, v in (raw.get("patients") or {}).items()},
            uids={str(k): str(v) for k, v in (raw.get("uids") or {}).items()},
            accessions={str(k): str(v) for k, v in (raw.get("accessions") or {}).items()},
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "warning": "This file can contain PHI because original PatientID, UID and AccessionNumber values may be used as keys. Store securely.",
            "site_id": self.site_id,
            "uid_root": self.uid_root,
            "project_name": self.project_name,
            "patient_id_strategy": self.patient_id_strategy,
            "patients": self.patients,
            "uids": self.uids,
            "accessions": self.accessions,
        }

    def save(self, path: Path) -> None:
        # The mapping keys are original PHI values; restrict the file to the
        # current user (mode 0600).
        write_private_text(path, json.dumps(self.to_json(), indent=2, ensure_ascii=False))


def numeric_site_component(site_id: str) -> str:
    digits = "".join(ch for ch in str(site_id) if ch.isdigit())
    if digits:
        return digits.lstrip("0") or "0"
    value = int(hashlib.sha256(str(site_id).encode("utf-8")).hexdigest(), 16) % 10**12
    return str(value)


def deterministic_uid_2_25(value: str, salt: str) -> str:
    digest = hashlib.sha256((salt + "|uid|" + value).encode("utf-8")).digest()[:16]
    integer = int.from_bytes(digest, byteorder="big", signed=False)
    return f"2.25.{integer}"


def deterministic_uid_with_root(value: str, root: str, site_id: str, salt: str) -> str:
    root = root.strip().rstrip(".")
    if not root or root == "2.25":
        return deterministic_uid_2_25(value, salt)
    prefix = f"{root}.{numeric_site_component(site_id)}.2."
    if len(prefix) >= DICOM_UID_MAX_LEN:
        raise ValueError(f"UID root/site prefix is too long for DICOM UID max length: {prefix}")
    available_digits = DICOM_UID_MAX_LEN - len(prefix)
    digest_int = int(hashlib.md5((salt + "|uid|" + value).encode("utf-8")).hexdigest(), 16)
    suffix = str(digest_int % (10**available_digits))
    return prefix + suffix


def valid_date_yyyymmdd(value: str) -> bool:
    try:
        dt = datetime.strptime(value, "%Y%m%d")
        return dt >= datetime(1900, 1, 1)
    except ValueError:
        return False


def hash_date_like_rsna(value: Any, patient_id: str, salt: str = "") -> Any:
    """Shift dates by a per-patient offset derived from PatientID and the project salt.

    The salt keeps the offset non-computable for anyone who knows the original
    PatientID but not the salt. All dates of one patient shift by the same
    number of days, preserving intervals.
    """
    if value is None:
        return value
    if isinstance(value, (MultiValue, list, tuple)):
        return [hash_date_like_rsna(v, patient_id, salt) for v in value]
    text = str(value)
    date_part = text[:8]
    suffix = text[8:]
    if not valid_date_yyyymmdd(date_part) or not patient_id:
        shifted = DEFAULT_DATE
    else:
        md5_hash = hashlib.md5((salt + "|date|" + patient_id).encode("utf-8")).hexdigest()
        days = int(md5_hash, 16) % 3652
        shifted = (datetime.strptime(date_part, "%Y%m%d") + timedelta(days=days)).strftime("%Y%m%d")
    return shifted + suffix


def round_age(value: Any, width: int) -> Any:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        numeric = "".join(ch for ch in text if ch.isdigit())
        unit = "".join(ch for ch in text if ch.isalpha())
        if not numeric:
            return text
        age = round(float(numeric) / width) * width
        result = f"{int(age)}{unit}"
        if len(result) % 2 != 0:
            result = "0" + result
        return result
    except Exception:
        return text


def map_multivalue(value: Any, mapper: Any) -> Any:
    if value in (None, ""):
        return value
    if isinstance(value, (MultiValue, list, tuple)):
        return [mapper(str(v)) if v not in (None, "") else v for v in value]
    return mapper(str(value))


class Anonymizer:
    def __init__(
        self,
        *,
        rules: ScriptRules,
        site_id: str,
        project_name: str,
        uid_root: str,
        salt: str,
        patient_id_strategy: str,
        mapping: MappingState | None = None,
        keep_private_tags: bool = False,
    ) -> None:
        self.rules = rules
        self.site_id = site_id
        self.project_name = project_name
        self.uid_root = uid_root
        self.salt = salt
        self.patient_id_strategy = patient_id_strategy
        self.mapping = mapping or MappingState(
            site_id=site_id,
            uid_root=uid_root,
            project_name=project_name,
            patient_id_strategy=patient_id_strategy,
        )
        if "" not in self.mapping.patients:
            self.mapping.patients[""] = self.default_patient_id
        self.keep_private_tags = keep_private_tags

    @property
    def default_patient_id(self) -> str:
        return f"{self.site_id}-000000"

    def next_sequential_patient_id(self) -> str:
        max_index = 0
        prefix = f"{self.site_id}-"
        for anon_id in self.mapping.patients.values():
            if anon_id.startswith(prefix):
                tail = anon_id[len(prefix) :]
                if tail.isdigit():
                    max_index = max(max_index, int(tail))
        return f"{self.site_id}-{max_index + 1:06d}"

    def get_anon_patient_id(self, patient_id: str | None) -> str:
        phi_id = (patient_id or "").strip()
        if phi_id in self.mapping.patients:
            return self.mapping.patients[phi_id]
        if self.patient_id_strategy == "hashed":
            digest = hashlib.sha256((self.salt + "|patient|" + phi_id).encode("utf-8")).hexdigest()[:12].upper()
            anon = f"{self.site_id}-{digest}"
        else:
            anon = self.next_sequential_patient_id()
        self.mapping.patients[phi_id] = anon
        return anon

    def get_anon_uid(self, uid: str | None) -> str | None:
        if uid is None:
            return None
        text = str(uid).strip()
        if not text:
            return text
        if text not in self.mapping.uids:
            self.mapping.uids[text] = deterministic_uid_with_root(text, self.uid_root, self.site_id, self.salt)
        return self.mapping.uids[text]

    def get_anon_accession(self, accession: str | None) -> str | None:
        if accession is None:
            return None
        text = str(accession).strip()
        if not text:
            return ""
        if text not in self.mapping.accessions:
            salt_string = f"anonymizer::{self.site_id}::{self.uid_root}.{numeric_site_component(self.site_id)}"
            hasher = hashlib.sha256()
            hasher.update((self.salt + "|" + salt_string).encode("utf-8"))
            hasher.update(text.encode("utf-8"))
            # AccessionNumber is VR SH, max 16 characters. Use a compact
            # 16-character hexadecimal digest rather than a hyphenated form.
            self.mapping.accessions[text] = hasher.hexdigest()[:16]
        return self.mapping.accessions[text]

    def anonymize_element(
        self,
        dataset: Dataset,
        element: DataElement,
        *,
        phi_patient_id: str,
        anon_patient_id: str,
        anon_accession: str | None,
    ) -> None:
        tag_hex = tag_to_hex(element.tag)
        if self.keep_private_tags and element.tag.is_private:
            return
        if tag_hex not in self.rules.tag_keep:
            if element.tag in dataset:
                del dataset[element.tag]
            return

        operation = self.rules.tag_keep[tag_hex]
        if operation in ("", "@keep"):
            return
        if "@empty" in operation:
            dataset[element.tag].value = ""
            return
        if "@uid" in operation:
            dataset[element.tag].value = map_multivalue(element.value, self.get_anon_uid)
            return
        if "@ptid" in operation:
            dataset[element.tag].value = anon_patient_id
            return
        if "@acc" in operation:
            dataset[element.tag].value = "" if str(element.value or "") == "" else (anon_accession or "")
            return
        if "@hashdate" in operation:
            dataset[element.tag].value = hash_date_like_rsna(element.value, phi_patient_id, self.salt)
            return
        if "@round" in operation:
            match = re.search(r"\d+", operation)
            width = int(match.group(0)) if match else 5
            dataset[element.tag].value = round_age(element.value, width)
            return

    def remove_curve_and_overlay_groups(self, ds: Dataset) -> None:
        # DICOM retired curve groups are 0x5000-0x50FF. Overlay groups are 0x6000-0x60FF.
        to_delete: list[BaseTag] = []
        for elem in ds:
            group = elem.tag.group
            if self.rules.remove_curves and 0x5000 <= group <= 0x50FF:
                to_delete.append(elem.tag)
            elif self.rules.remove_overlays and 0x6000 <= group <= 0x60FF:
                to_delete.append(elem.tag)
        for tag in to_delete:
            if tag in ds:
                del ds[tag]

    def add_deidentification_tags(self, ds: Dataset) -> list[str]:
        warnings: list[str] = []
        ds.PatientIdentityRemoved = "YES"
        ds.DeidentificationMethod = DEFAULT_DEIDENTIFICATION_METHOD
        sequence = Sequence()
        for code, meaning in DEIDENTIFICATION_METHOD_CODES:
            item = Dataset()
            item.CodeValue = code
            item.CodingSchemeDesignator = "DCM"
            item.CodeMeaning = meaning
            sequence.append(item)
        ds.DeidentificationMethodCodeSequence = sequence
        try:
            block = ds.private_block(0x0013, "RSNA", create=True)
            block.add_new(0x01, "SH", str(self.site_id)[:16])
            block.add_new(0x03, "SH", str(self.project_name)[:16])
        except Exception as exc:  # noqa: BLE001
            # Private provenance tags are useful but not essential; surface the
            # failure in the audit output instead of swallowing it.
            warnings.append(f"Could not add private provenance tags: {exc}")
        return warnings

    def sync_file_meta(self, ds: Dataset) -> None:
        if not getattr(ds, "file_meta", None):
            ds.file_meta = Dataset()
        if getattr(ds, "SOPClassUID", None):
            ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
        if getattr(ds, "SOPInstanceUID", None):
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        if not getattr(ds.file_meta, "TransferSyntaxUID", None):
            ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    def anonymize_dataset(self, ds: Dataset) -> tuple[Dataset, dict[str, Any]]:
        missing = [name for name in REQUIRED_ATTRIBUTES if not getattr(ds, name, None)]
        if missing:
            raise ValueError(f"Dataset missing required attributes: {missing}")

        phi_patient_id = str(getattr(ds, "PatientID", "") or "").strip()
        anon_patient_id = self.get_anon_patient_id(phi_patient_id)
        anon_accession = self.get_anon_accession(str(getattr(ds, "AccessionNumber", "") or ""))

        original = {
            "PatientID_present": bool(phi_patient_id),
            "StudyInstanceUID": str(getattr(ds, "StudyInstanceUID", "")),
            "SeriesInstanceUID": str(getattr(ds, "SeriesInstanceUID", "")),
            "SOPInstanceUID": str(getattr(ds, "SOPInstanceUID", "")),
            "StudyDate": str(getattr(ds, "StudyDate", "") or ""),
        }

        if self.rules.remove_private_tags and not self.keep_private_tags:
            ds.remove_private_tags()
        self.remove_curve_and_overlay_groups(ds)
        if not hasattr(ds, "PatientID"):
            ds.PatientID = ""

        def callback(current_dataset: Dataset, data_element: DataElement) -> None:
            self.anonymize_element(
                current_dataset,
                data_element,
                phi_patient_id=phi_patient_id,
                anon_patient_id=anon_patient_id,
                anon_accession=anon_accession,
            )

        ds.walk(callback)
        warnings = self.add_deidentification_tags(ds)
        self.sync_file_meta(ds)

        after = {
            "PatientID": str(getattr(ds, "PatientID", "") or ""),
            "StudyInstanceUID": str(getattr(ds, "StudyInstanceUID", "") or ""),
            "SeriesInstanceUID": str(getattr(ds, "SeriesInstanceUID", "") or ""),
            "SOPInstanceUID": str(getattr(ds, "SOPInstanceUID", "") or ""),
            "StudyDate": str(getattr(ds, "StudyDate", "") or ""),
        }
        info: dict[str, Any] = {"original": original, "anonymized": after}
        if warnings:
            info["warnings"] = warnings
        return ds, info


def output_path_for_dataset(out_dir: Path, ds: Dataset, fallback_index: int) -> Path:
    patient = sanitize_component(getattr(ds, "PatientID", None), "unknown_patient")
    study = sanitize_component(getattr(ds, "StudyInstanceUID", None), "unknown_study")
    series = sanitize_component(getattr(ds, "SeriesInstanceUID", None), "unknown_series")
    sop = sanitize_component(getattr(ds, "SOPInstanceUID", None), f"instance_{fallback_index:06d}")
    dest_dir = out_dir / patient / study / series
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / f"{sop}.dcm"


def write_dataset(ds: Dataset, path: Path) -> None:
    save_dataset_atomic(ds, path)


def command_anonymize(args: argparse.Namespace) -> dict[str, Any]:
    script_path = Path(args.script).expanduser().resolve() if args.script else DEFAULT_RSNA_SCRIPT
    rules = ScriptRules.load(script_path)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    salt = args.salt
    if args.salt_env:
        salt = os.environ.get(args.salt_env, salt)
    if not salt:
        salt = f"{args.site_id}|{args.project_name}|dicom-skill-rsna-derived"

    mapping_path = Path(args.map_json).expanduser().resolve() if args.map_json else None
    mapping = (
        MappingState.load(
            mapping_path,
            site_id=args.site_id,
            uid_root=args.uid_root,
            project_name=args.project_name,
            patient_id_strategy=args.patient_id_strategy,
        )
        if mapping_path
        else None
    )

    anonymizer = Anonymizer(
        rules=rules,
        site_id=args.site_id,
        project_name=args.project_name,
        uid_root=args.uid_root,
        salt=salt,
        patient_id_strategy=args.patient_id_strategy,
        mapping=mapping,
        keep_private_tags=args.keep_private_tags,
    )

    files = readable_dicom_files(args.path, force=args.force, max_files=args.max_files)
    result: dict[str, Any] = {
        "operation": "ANONYMIZE",
        "input_paths": args.path,
        "out_dir": str(out_dir),
        "script": str(script_path),
        "site_id": args.site_id,
        "project_name": args.project_name,
        "uid_root": args.uid_root,
        "patient_id_strategy": args.patient_id_strategy,
        "file_count": len(files),
        "processed_count": 0,
        "failure_count": 0,
        "dry_run": bool(args.dry_run),
        "remove_private_tags": bool(rules.remove_private_tags and not args.keep_private_tags),
        "pixel_phi_removal": False,
        "deidentification_method": DEFAULT_DEIDENTIFICATION_METHOD,
        "items": [] if args.include_files else None,
        "failures": [],
    }

    if args.dry_run:
        if args.include_files:
            result["items"] = [{"source": str(p)} for p in files]
        return result

    seen_patients: set[str] = set()
    seen_studies: set[str] = set()
    seen_series: set[str] = set()
    for index, src in enumerate(files, start=1):
        try:
            ds = dcmread(str(src), force=args.force)
            ds, info = anonymizer.anonymize_dataset(ds)
            dest = output_path_for_dataset(out_dir, ds, index)
            if dest.exists() and not args.overwrite:
                raise FileExistsError(f"Destination exists; pass --overwrite to replace: {dest}")
            write_dataset(ds, dest)
            result["processed_count"] += 1
            seen_patients.add(str(getattr(ds, "PatientID", "")))
            seen_studies.add(str(getattr(ds, "StudyInstanceUID", "")))
            seen_series.add(str(getattr(ds, "SeriesInstanceUID", "")))
            if args.include_files:
                assert result["items"] is not None
                result["items"].append({"source": str(src), "dest": str(dest), **info})
        except InvalidDicomError as exc:
            item = {"source": str(src), "error": f"Invalid DICOM: {exc}"}
            result["failures"].append(item)
            if args.fail_on_error:
                raise
        except Exception as exc:  # noqa: BLE001
            item = {"source": str(src), "error": str(exc)}
            result["failures"].append(item)
            if args.fail_on_error:
                raise

    result["failure_count"] = len(result["failures"])
    result["patient_count"] = len([p for p in seen_patients if p])
    result["study_count"] = len([s for s in seen_studies if s])
    result["series_count"] = len([s for s in seen_series if s])
    result["mapping_counts"] = {
        "patients": len(anonymizer.mapping.patients),
        "uids": len(anonymizer.mapping.uids),
        "accessions": len(anonymizer.mapping.accessions),
    }
    if mapping_path:
        anonymizer.mapping.save(mapping_path)
        result["map_json"] = str(mapping_path)
        result["map_json_warning"] = "Mapping file may contain PHI; store securely."
    return result


def command_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": result.get("operation"),
        "file_count": result.get("file_count"),
        "processed_count": result.get("processed_count"),
        "failure_count": result.get("failure_count"),
        "patient_count": result.get("patient_count"),
        "study_count": result.get("study_count"),
        "series_count": result.get("series_count"),
        "out_dir": result.get("out_dir"),
        "dry_run": result.get("dry_run"),
        "pixel_phi_removal": result.get("pixel_phi_removal"),
        "deidentification_method": result.get("deidentification_method"),
        "map_json": result.get("map_json"),
        "map_json_warning": result.get("map_json_warning"),
        "failures": result.get("failures")[:5] if result.get("failures") else [],
    }


def print_or_write(result: dict[str, Any], out_json: str | None = None, summary: bool = False) -> None:
    _print_or_write(result, out_json, command_summary if summary else None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Anonymize local DICOM files/folders using an RSNA-anonymizer-derived tag script."
    )
    parser.add_argument("--path", action="append", required=True, help="DICOM file or directory; can be repeated")
    parser.add_argument("--out", required=True, help="Output directory for anonymized DICOM files")
    parser.add_argument("--script", default=str(DEFAULT_RSNA_SCRIPT), help="Anonymizer XML script; default is bundled RSNA default script")
    parser.add_argument("--site-id", default=DEFAULT_SITE_ID, help="Site/project ID used in anonymized PatientID and provenance tags")
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--uid-root", default=DEFAULT_UID_ROOT, help="UID root. Default 2.25 uses deterministic UUID-style OIDs. Provide your registered root for production.")
    parser.add_argument("--salt", help="Project salt for deterministic UID/accession/patient hashing")
    parser.add_argument("--salt-env", help="Read project salt from this environment variable")
    parser.add_argument("--patient-id-strategy", choices=["sequential", "hashed"], default="sequential")
    parser.add_argument("--map-json", help="Optional PHI-containing mapping JSON to load/update across runs")
    parser.add_argument("--force", action="store_true", help="Force pydicom reads for non-standard files")
    parser.add_argument("--dry-run", action="store_true", help="Discover DICOM files without writing anonymized output")
    parser.add_argument("--include-files", action="store_true", help="Include per-file source/destination details in JSON output")
    parser.add_argument("--max-files", type=int, help="Maximum number of readable DICOM files to process")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite anonymized destination files if they already exist")
    parser.add_argument("--keep-private-tags", action="store_true", help="Do not remove private tags even if the RSNA script requests private group removal")
    parser.add_argument("--fail-on-error", action="store_true", help="Stop on first file error instead of recording failures")
    parser.add_argument("--out-json", help="Write full JSON result to this file as well as stdout")
    parser.add_argument("--summary", action="store_true", help="Print concise PHI-light summary to stdout; --out-json still stores full JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = command_anonymize(args)
        print_or_write(result, args.out_json, args.summary)
        if result.get("failure_count") and args.fail_on_error:
            return 1
        return 0 if not result.get("failure_count") else 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
