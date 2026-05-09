#!/usr/bin/env python3
"""DICOM DIMSE command-line operations for dicom-skill.

Supports C-ECHO, C-FIND, C-GET/C-MOVE retrieval, and C-STORE send.
This script is intentionally a CLI utility for agents; it does not expose MCP.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except Exception:  # pragma: no cover - handled at runtime
    yaml = None  # type: ignore

try:
    import pydicom
    from pydicom import dcmread
    from pydicom.datadict import dictionary_VR, keyword_for_tag, tag_for_keyword
    from pydicom.dataset import Dataset
    from pydicom.multival import MultiValue
    from pydicom.tag import Tag
    from pydicom.uid import UID
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": f"pydicom is required: {exc}"}), file=sys.stderr)
    raise

try:
    from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, StoragePresentationContexts, build_role, debug_logger, evt
    from pynetdicom.sop_class import (
        PatientRootQueryRetrieveInformationModelFind,
        PatientRootQueryRetrieveInformationModelGet,
        PatientRootQueryRetrieveInformationModelMove,
        StudyRootQueryRetrieveInformationModelFind,
        StudyRootQueryRetrieveInformationModelGet,
        StudyRootQueryRetrieveInformationModelMove,
        Verification,
    )
except Exception as exc:  # pragma: no cover
    print(json.dumps({"error": f"pynetdicom is required: {exc}"}), file=sys.stderr)
    raise

# Allow sibling import whether script is executed directly or from package context.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from orthanc_temp import export_instances, start_temp_orthanc, stop_temp_orthanc
except Exception:  # pragma: no cover - retrieve --method move will fail clearly
    export_instances = None  # type: ignore
    start_temp_orthanc = None  # type: ignore
    stop_temp_orthanc = None  # type: ignore


DEFAULT_CALLING_AET = "AGENT"
DEFAULT_TIMEOUT = 30.0

FIND_MODELS = {
    "patient": PatientRootQueryRetrieveInformationModelFind,
    "study": StudyRootQueryRetrieveInformationModelFind,
}
GET_MODELS = {
    "patient": PatientRootQueryRetrieveInformationModelGet,
    "study": StudyRootQueryRetrieveInformationModelGet,
}
MOVE_MODELS = {
    "patient": PatientRootQueryRetrieveInformationModelMove,
    "study": StudyRootQueryRetrieveInformationModelMove,
}

DEFAULT_RETURNS = {
    "PATIENT": ["PatientName", "PatientID", "PatientBirthDate", "PatientSex"],
    "STUDY": [
        "PatientName",
        "PatientID",
        "PatientBirthDate",
        "PatientSex",
        "AccessionNumber",
        "StudyDate",
        "StudyTime",
        "StudyDescription",
        "ModalitiesInStudy",
        "StudyInstanceUID",
    ],
    "SERIES": [
        "PatientID",
        "StudyInstanceUID",
        "SeriesInstanceUID",
        "Modality",
        "SeriesNumber",
        "SeriesDescription",
        "ProtocolName",
    ],
    "IMAGE": ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "SOPClassUID", "InstanceNumber"],
}


@dataclass
class RemoteNode:
    host: str
    port: int
    aet: str
    calling_aet: str = DEFAULT_CALLING_AET

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "aet": self.aet, "calling_aet": self.calling_aet}


def require_yaml() -> None:
    if yaml is None:
        raise RuntimeError("PyYAML is required for --config. Install requirements.txt.")


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    require_yaml()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}  # type: ignore[union-attr]
    if not isinstance(data, dict):
        raise ValueError("Config YAML must contain a mapping at top level.")
    return data


def resolve_remote(args: argparse.Namespace) -> RemoteNode:
    config = load_config(getattr(args, "config", None))
    node_name = getattr(args, "node", None) or config.get("current_node")
    node_data: dict[str, Any] = {}
    if node_name:
        nodes = config.get("nodes") or {}
        if node_name not in nodes:
            raise ValueError(f"Node '{node_name}' not found in config.")
        node_data = nodes[node_name] or {}
    host = getattr(args, "host", None) or node_data.get("host")
    port = getattr(args, "port", None) or node_data.get("port")
    aet = getattr(args, "aet", None) or node_data.get("ae_title") or node_data.get("aet")
    calling_aet = getattr(args, "calling_aet", None) or config.get("calling_aet") or DEFAULT_CALLING_AET
    missing = [name for name, value in [("host", host), ("port", port), ("aet/ae_title", aet)] if value in (None, "")]
    if missing:
        raise ValueError(f"Missing DICOM remote settings: {', '.join(missing)}. Provide CLI args or --config/--node.")
    return RemoteNode(host=str(host), port=int(port), aet=str(aet), calling_aet=str(calling_aet))


def normalize_level(level: str | None) -> str:
    text = (level or "STUDY").strip().upper()
    if text == "INSTANCE":
        text = "IMAGE"
    if text not in {"PATIENT", "STUDY", "SERIES", "IMAGE"}:
        raise ValueError("QueryRetrieveLevel must be PATIENT, STUDY, SERIES, IMAGE, or INSTANCE.")
    return text


def parse_tag_reference(key: str) -> Tag | None:
    cleaned = key.strip().replace("(", "").replace(")", "").replace(",", "").replace(" ", "")
    if len(cleaned) == 8 and all(ch in "0123456789abcdefABCDEF" for ch in cleaned):
        return Tag(int(cleaned, 16))
    return None


def set_dicom_value(ds: Dataset, key: str, value: Any) -> None:
    key = key.strip()
    if not key:
        raise ValueError("Empty DICOM key.")
    tag = parse_tag_reference(key)
    if tag is not None:
        vr = dictionary_VR(tag) or "LO"
        ds.add_new(tag, vr, value)
        return
    tag_int = tag_for_keyword(key)
    if tag_int is None:
        raise ValueError(f"Unknown DICOM keyword/tag: {key}")
    setattr(ds, key, value)


def ensure_return_tag(ds: Dataset, key: str) -> None:
    key = key.strip()
    if not key:
        return
    tag = parse_tag_reference(key)
    if tag is not None:
        if tag not in ds:
            vr = dictionary_VR(tag) or "LO"
            ds.add_new(tag, vr, "")
        return
    if tag_for_keyword(key) is None:
        raise ValueError(f"Unknown DICOM return keyword/tag: {key}")
    if key not in ds:
        setattr(ds, key, "")


def parse_filter(expr: str) -> tuple[str, str]:
    if "=" not in expr:
        raise ValueError(f"Filter must be KEY=VALUE, got: {expr}")
    key, value = expr.split("=", 1)
    return key.strip(), value


def build_query_dataset(level: str, filters: list[str], returns: list[str] | None) -> Dataset:
    level = normalize_level(level)
    ds = Dataset()
    ds.QueryRetrieveLevel = level
    for expr in filters:
        key, value = parse_filter(expr)
        set_dicom_value(ds, key, value)
    return_tags = list(returns or [])
    if not return_tags:
        return_tags = DEFAULT_RETURNS[level]
    for key in return_tags:
        if key == "QueryRetrieveLevel":
            continue
        # Do not overwrite a matching key that already has a filter value.
        tag = parse_tag_reference(key)
        if tag is not None and tag in ds:
            continue
        if tag is None and key in ds:
            continue
        ensure_return_tag(ds, key)
    return ds


def build_retrieve_dataset(args: argparse.Namespace) -> Dataset:
    level = getattr(args, "level", None)
    if not level:
        if getattr(args, "sop_uid", None):
            level = "IMAGE"
        elif getattr(args, "series_uid", None):
            level = "SERIES"
        elif getattr(args, "study_uid", None):
            level = "STUDY"
        elif getattr(args, "patient_id", None):
            level = "PATIENT"
        else:
            level = "STUDY"
    level = normalize_level(level)
    ds = Dataset()
    ds.QueryRetrieveLevel = level
    if getattr(args, "patient_id", None):
        ds.PatientID = args.patient_id
    if getattr(args, "study_uid", None):
        ds.StudyInstanceUID = args.study_uid
    if getattr(args, "series_uid", None):
        ds.SeriesInstanceUID = args.series_uid
    if getattr(args, "sop_uid", None):
        ds.SOPInstanceUID = args.sop_uid
    for expr in getattr(args, "filter", []) or []:
        key, value = parse_filter(expr)
        set_dicom_value(ds, key, value)
    return ds


def value_to_plain(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, UID):
        return str(value)
    if isinstance(value, MultiValue):
        return [value_to_plain(v) for v in value]
    if isinstance(value, (list, tuple)):
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


def status_to_plain(status: Dataset | None) -> dict[str, Any]:
    if status is None:
        return {"status": None, "message": "No response status; association may have timed out or aborted."}
    out = dataset_to_plain(status) or {}
    code = getattr(status, "Status", None)
    if isinstance(code, int):
        out["StatusHex"] = f"0x{code:04X}"
    return out


def final_response_status(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return the last non-empty DIMSE response status from a command result."""
    for item in reversed(result.get("responses") or []):
        status = item.get("status") if isinstance(item, dict) else None
        if status:
            return status
    status = result.get("status")
    return status if isinstance(status, dict) else None


def command_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Small, PHI-light summary intended for terminal/chat output.

    The full JSON result can still be persisted with --out-json for audit/debugging.
    """
    op = result.get("operation")
    summary: dict[str, Any] = {
        "operation": op,
        "established": result.get("established"),
    }
    if result.get("error"):
        summary["error"] = result.get("error")

    if op == "C-FIND":
        summary["match_count"] = result.get("match_count", len(result.get("matches") or []))
        summary["matches"] = [
            {
                "StudyDate": m.get("StudyDate"),
                "StudyTime": m.get("StudyTime"),
                "ModalitiesInStudy": m.get("ModalitiesInStudy"),
                "StudyDescription": m.get("StudyDescription"),
                "StudyInstanceUID": m.get("StudyInstanceUID"),
                "NumberOfStudyRelatedInstances": m.get("NumberOfStudyRelatedInstances"),
            }
            for m in (result.get("matches") or [])
        ]
    elif op == "C-GET":
        final = final_response_status(result) or {}
        summary.update(
            {
                "received_count": result.get("received_count", len(result.get("received") or [])),
                "store_error_count": len(result.get("store_errors") or []),
                "response_count": len(result.get("responses") or []),
                "final_status": final,
            }
        )
        failed = final.get("NumberOfFailedSuboperations")
        if isinstance(failed, int) and failed > 0:
            summary["recommendation"] = "C-GET had failed suboperations; try C-MOVE to a known destination AE and verify there."
    elif op == "C-MOVE":
        final = final_response_status(result) or {}
        summary.update(
            {
                "destination_aet": result.get("destination_aet"),
                "use_temp_orthanc": result.get("use_temp_orthanc"),
                "response_count": len(result.get("responses") or []),
                "final_status": final,
            }
        )
        if result.get("export"):
            summary["export"] = result.get("export")
    elif op == "C-STORE":
        summary.update(
            {
                "file_count": result.get("file_count"),
                "dry_run": result.get("dry_run"),
                "sent_count": result.get("sent_count", len(result.get("sent") or [])),
                "failure_count": result.get("failure_count", len(result.get("failures") or [])),
            }
        )
    elif op == "C-ECHO":
        summary["status"] = result.get("status")

    return summary


def print_or_write(result: dict[str, Any], out_json: str | None = None, summary: bool = False) -> None:
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if out_json:
        Path(out_json).parent.mkdir(parents=True, exist_ok=True)
        Path(out_json).write_text(text + "\n", encoding="utf-8")
    printable = command_summary(result) if summary else result
    print(json.dumps(printable, indent=2, ensure_ascii=False))


def make_ae(calling_aet: str, timeout: float | None = None) -> AE:
    ae = AE(ae_title=calling_aet)
    if timeout is not None:
        ae.acse_timeout = timeout
        ae.dimse_timeout = timeout
        ae.network_timeout = timeout
    return ae


def command_echo(args: argparse.Namespace) -> dict[str, Any]:
    remote = resolve_remote(args)
    ae = make_ae(remote.calling_aet, args.timeout)
    ae.add_requested_context(Verification)
    assoc = ae.associate(remote.host, remote.port, ae_title=remote.aet)
    result: dict[str, Any] = {"operation": "C-ECHO", "remote": remote.to_dict(), "established": bool(assoc.is_established)}
    if assoc.is_established:
        status = assoc.send_c_echo()
        result["status"] = status_to_plain(status)
        assoc.release()
    else:
        result["error"] = "Association rejected, aborted, or never connected."
    return result


def command_query(args: argparse.Namespace) -> dict[str, Any]:
    remote = resolve_remote(args)
    level = normalize_level(args.level)
    model_key = args.model.lower()
    model = FIND_MODELS[model_key]
    identifier = build_query_dataset(level, args.filter or [], args.return_tag or [])

    ae = make_ae(remote.calling_aet, args.timeout)
    ae.add_requested_context(model)
    assoc = ae.associate(remote.host, remote.port, ae_title=remote.aet)
    result: dict[str, Any] = {
        "operation": "C-FIND",
        "remote": remote.to_dict(),
        "model": model_key,
        "identifier": dataset_to_plain(identifier),
        "established": bool(assoc.is_established),
        "matches": [],
        "responses": [],
    }
    if assoc.is_established:
        for status, response_identifier in assoc.send_c_find(identifier, model):
            plain_status = status_to_plain(status)
            result["responses"].append({"status": plain_status, "identifier": dataset_to_plain(response_identifier)})
            status_code = getattr(status, "Status", None) if status else None
            if status_code in (0xFF00, 0xFF01) and response_identifier is not None:
                result["matches"].append(dataset_to_plain(response_identifier))
        assoc.release()
        result["match_count"] = len(result["matches"])
    else:
        result["error"] = "Association rejected, aborted, or never connected."
    return result


def path_for_store(out_dir: Path, ds: Dataset, fallback_index: int) -> Path:
    study_uid = safe_path_component(getattr(ds, "StudyInstanceUID", None), "unknown_study")
    series_uid = safe_path_component(getattr(ds, "SeriesInstanceUID", None), "unknown_series")
    sop_uid = safe_path_component(getattr(ds, "SOPInstanceUID", None), f"instance_{fallback_index:06d}")
    dest_dir = out_dir / study_uid / series_uid
    dest_dir.mkdir(parents=True, exist_ok=True)
    return dest_dir / f"{sop_uid}.dcm"


def safe_path_component(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    if not text:
        return fallback
    keep = []
    for ch in text:
        if ch.isalnum() or ch in ".-_^":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:180] or fallback


def configure_storage_contexts_for_get(ae: AE) -> list[Any]:
    roles = []
    for cx in StoragePresentationContexts:
        ae.add_requested_context(cx.abstract_syntax, ALL_TRANSFER_SYNTAXES)
        roles.append(build_role(cx.abstract_syntax, scp_role=True))
    return roles


def command_retrieve_get(args: argparse.Namespace, remote: RemoteNode, identifier: Dataset, model_key: str) -> dict[str, Any]:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    model = GET_MODELS[model_key]
    ae = make_ae(remote.calling_aet, args.timeout)
    ae.add_requested_context(model)
    roles = configure_storage_contexts_for_get(ae)
    received: list[str] = []
    store_errors: list[dict[str, Any]] = []
    counter = {"n": 0}

    def handle_store(event: Any) -> int:
        counter["n"] += 1
        try:
            ds = event.dataset
            ds.file_meta = event.file_meta
            dest = path_for_store(out_dir, ds, counter["n"])
            try:
                ds.save_as(str(dest), enforce_file_format=True)
            except TypeError:  # older pydicom compatibility
                ds.save_as(str(dest), write_like_original=False)
            received.append(str(dest))
            return 0x0000
        except Exception as exc:  # noqa: BLE001
            store_errors.append({"index": counter["n"], "error": str(exc)})
            return 0xC211

    handlers = [(evt.EVT_C_STORE, handle_store)]
    assoc = ae.associate(remote.host, remote.port, ae_title=remote.aet, ext_neg=roles, evt_handlers=handlers)
    result: dict[str, Any] = {
        "operation": "C-GET",
        "remote": remote.to_dict(),
        "model": model_key,
        "identifier": dataset_to_plain(identifier),
        "out_dir": str(out_dir),
        "established": bool(assoc.is_established),
        "responses": [],
        "received": received,
        "store_errors": store_errors,
    }
    if assoc.is_established:
        for status, response_identifier in assoc.send_c_get(identifier, model):
            result["responses"].append({"status": status_to_plain(status), "identifier": dataset_to_plain(response_identifier)})
        assoc.release()
        result["received_count"] = len(received)
    else:
        result["error"] = "Association rejected, aborted, or never connected."
    return result


def command_retrieve_move(args: argparse.Namespace, remote: RemoteNode, identifier: Dataset, model_key: str) -> dict[str, Any]:
    model = MOVE_MODELS[model_key]
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    use_temp = args.use_temp_orthanc
    if use_temp is None:
        use_temp = (args.destination_aet or "AGENT") == "AGENT"

    state: Any = None
    temp_data_dir: str | None = None
    if use_temp:
        if start_temp_orthanc is None:
            raise RuntimeError("orthanc_temp.py could not be imported. Cannot start temporary Orthanc.")
        temp_data_dir = args.orthanc_data_dir or tempfile.mkdtemp(prefix="dicom-skill-move-orthanc-")
        state = start_temp_orthanc(
            aet=args.destination_aet,
            dicom_port=args.orthanc_dicom_port,
            http_port=args.orthanc_http_port,
            name=args.orthanc_name,
            data_dir=temp_data_dir,
            image=args.orthanc_image,
            pull=args.orthanc_pull,
            timeout=args.orthanc_timeout,
        )

    ae = make_ae(remote.calling_aet, args.timeout)
    ae.add_requested_context(model)
    assoc = ae.associate(remote.host, remote.port, ae_title=remote.aet)
    result: dict[str, Any] = {
        "operation": "C-MOVE",
        "remote": remote.to_dict(),
        "model": model_key,
        "identifier": dataset_to_plain(identifier),
        "destination_aet": args.destination_aet,
        "use_temp_orthanc": bool(use_temp),
        "out_dir": str(out_dir),
        "established": bool(assoc.is_established),
        "responses": [],
    }
    if state is not None:
        result["temporary_orthanc"] = state.to_dict()

    try:
        if assoc.is_established:
            for status, response_identifier in assoc.send_c_move(identifier, args.destination_aet, model):
                result["responses"].append({"status": status_to_plain(status), "identifier": dataset_to_plain(response_identifier)})
            assoc.release()
        else:
            result["error"] = "Association rejected, aborted, or never connected."

        if use_temp and state is not None and export_instances is not None:
            # Orthanc generally receives during C-MOVE before final status, but leave a short grace period.
            if args.post_move_wait > 0:
                time.sleep(args.post_move_wait)
            result["export"] = export_instances(state.http_url, str(out_dir), clear_after=False)
    finally:
        if use_temp and state is not None and not args.keep_orthanc:
            if stop_temp_orthanc is not None:
                result["temporary_orthanc_stop"] = stop_temp_orthanc(args.orthanc_name, purge=True, data_dir=temp_data_dir)
    return result


def command_retrieve(args: argparse.Namespace) -> dict[str, Any]:
    remote = resolve_remote(args)
    identifier = build_retrieve_dataset(args)
    model_key = args.model.lower()
    if args.method == "get":
        return command_retrieve_get(args, remote, identifier, model_key)
    if args.method == "move":
        return command_retrieve_move(args, remote, identifier, model_key)
    raise ValueError(f"Unknown retrieve method: {args.method}")


def discover_dicom_files(paths: list[str], force: bool = False, max_files: int | None = None) -> list[str]:
    discovered: list[str] = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = [p for p in path.rglob("*") if p.is_file()]
        else:
            raise FileNotFoundError(f"Path not found: {item}")
        for candidate in candidates:
            try:
                ds = dcmread(str(candidate), stop_before_pixels=True, force=force)
                if getattr(ds, "SOPClassUID", None):
                    discovered.append(str(candidate))
            except Exception:
                continue
            if max_files is not None and len(discovered) >= max_files:
                return discovered
    return discovered


def command_send(args: argparse.Namespace) -> dict[str, Any]:
    remote = resolve_remote(args)
    files = discover_dicom_files(args.path, force=args.force, max_files=args.max_files)
    result: dict[str, Any] = {
        "operation": "C-STORE",
        "remote": remote.to_dict(),
        "file_count": len(files),
        "files": files if args.include_files else None,
        "dry_run": bool(args.dry_run),
        "sent": [],
        "failures": [],
    }
    if args.dry_run:
        return result
    if not files:
        result["error"] = "No readable DICOM files with SOPClassUID found."
        return result

    sop_transfer_syntaxes: dict[str, set[str]] = {}
    for f in files:
        try:
            ds = dcmread(f, stop_before_pixels=True, force=args.force)
            sop_uid = str(ds.SOPClassUID)
            transfer_syntax_uid = str(ds.file_meta.TransferSyntaxUID)
            sop_transfer_syntaxes.setdefault(sop_uid, set()).add(transfer_syntax_uid)
        except Exception as exc:  # noqa: BLE001
            result["failures"].append({"file": f, "error": f"Could not inspect SOPClassUID/TransferSyntaxUID: {exc}"})

    ae = make_ae(remote.calling_aet, args.timeout)
    for sop, transfer_syntaxes in sorted(sop_transfer_syntaxes.items()):
        # Request the actual transfer syntax(es) present in the file set. Some
        # PACS reject or negotiate poorly when offered every known syntax for a
        # storage SOP class, especially for JPEG2000 or Encapsulated PDF.
        ae.add_requested_context(sop, sorted(transfer_syntaxes))
    assoc = ae.associate(remote.host, remote.port, ae_title=remote.aet)
    result["established"] = bool(assoc.is_established)
    if not assoc.is_established:
        result["error"] = "Association rejected, aborted, or never connected."
        return result

    for f in files:
        try:
            ds = dcmread(f, force=args.force)
            status = assoc.send_c_store(ds)
            plain = status_to_plain(status)
            code = getattr(status, "Status", None) if status else None
            item = {"file": f, "status": plain}
            if code == 0x0000:
                result["sent"].append(item)
            else:
                result["failures"].append(item)
        except Exception as exc:  # noqa: BLE001
            result["failures"].append({"file": f, "error": str(exc)})
    assoc.release()
    result["sent_count"] = len(result["sent"])
    result["failure_count"] = len(result["failures"])
    return result


def add_remote_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="YAML config with nodes and calling_aet")
    parser.add_argument("--node", help="Node name in YAML config")
    parser.add_argument("--host", help="Remote DICOM host/IP")
    parser.add_argument("--port", type=int, help="Remote DICOM port")
    parser.add_argument("--aet", help="Remote called AE title")
    parser.add_argument("--calling-aet", help="Local/calling AE title; default AGENT or config calling_aet")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Association/DIMSE/network timeout seconds")
    parser.add_argument("--debug", action="store_true", help="Enable pynetdicom debug logging")
    parser.add_argument("--out-json", help="Write JSON result to this file as well as stdout")
    parser.add_argument("--summary", action="store_true", help="Print concise PHI-light summary to stdout; --out-json still stores full JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DICOM DIMSE operations for agents: echo, query, retrieve, send.")
    sub = parser.add_subparsers(dest="command", required=True)

    echo_p = sub.add_parser("echo", help="Run C-ECHO verification")
    add_remote_args(echo_p)

    query_p = sub.add_parser("query", help="Run C-FIND query")
    add_remote_args(query_p)
    query_p.add_argument("--model", choices=sorted(FIND_MODELS), default="study", help="Query/Retrieve information model")
    query_p.add_argument("--level", default="STUDY", help="PATIENT, STUDY, SERIES, IMAGE/INSTANCE")
    query_p.add_argument("--filter", action="append", default=[], help="DICOM matching key, KEY=VALUE; can be repeated")
    query_p.add_argument("--return", dest="return_tag", action="append", default=[], help="DICOM return key; can be repeated")

    retrieve_p = sub.add_parser("retrieve", help="Retrieve with C-GET or C-MOVE")
    add_remote_args(retrieve_p)
    retrieve_p.add_argument("--method", choices=["get", "move"], default="get")
    retrieve_p.add_argument("--model", choices=sorted(GET_MODELS), default="study")
    retrieve_p.add_argument("--level", help="PATIENT, STUDY, SERIES, IMAGE/INSTANCE; inferred from UID args if omitted")
    retrieve_p.add_argument("--patient-id")
    retrieve_p.add_argument("--study-uid")
    retrieve_p.add_argument("--series-uid")
    retrieve_p.add_argument("--sop-uid")
    retrieve_p.add_argument("--filter", action="append", default=[], help="Additional retrieve key, KEY=VALUE; can be repeated")
    retrieve_p.add_argument("--out", required=True, help="Directory for retrieved/exported DICOM files")
    retrieve_p.add_argument("--destination-aet", default="AGENT", help="C-MOVE destination AE title")
    retrieve_p.add_argument("--use-temp-orthanc", dest="use_temp_orthanc", action="store_true", default=None)
    retrieve_p.add_argument("--no-temp-orthanc", dest="use_temp_orthanc", action="store_false")
    retrieve_p.add_argument("--orthanc-dicom-port", type=int, default=4242)
    retrieve_p.add_argument("--orthanc-http-port", type=int, default=8042)
    retrieve_p.add_argument("--orthanc-name", default="dicom-skill-agent-orthanc")
    retrieve_p.add_argument("--orthanc-data-dir")
    retrieve_p.add_argument("--orthanc-image", default="orthancteam/orthanc:latest")
    retrieve_p.add_argument("--orthanc-pull", action="store_true")
    retrieve_p.add_argument("--orthanc-timeout", type=float, default=30.0)
    retrieve_p.add_argument("--post-move-wait", type=float, default=1.0)
    retrieve_p.add_argument("--keep-orthanc", action="store_true", help="Do not stop/purge temporary Orthanc after export")

    send_p = sub.add_parser("send", help="Send DICOM file/folder with C-STORE")
    add_remote_args(send_p)
    send_p.add_argument("--path", action="append", required=True, help="DICOM file or directory; can be repeated")
    send_p.add_argument("--force", action="store_true", help="Force pydicom reads for non-standard files")
    send_p.add_argument("--dry-run", action="store_true", help="Only discover files; do not associate/send")
    send_p.add_argument("--include-files", action="store_true", help="Include discovered file list in JSON output")
    send_p.add_argument("--max-files", type=int, help="Maximum number of files to discover/send")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "debug", False):
        debug_logger()
    try:
        if args.command == "echo":
            result = command_echo(args)
        elif args.command == "query":
            result = command_query(args)
        elif args.command == "retrieve":
            result = command_retrieve(args)
        elif args.command == "send":
            result = command_send(args)
        else:  # pragma: no cover
            parser.error("Unknown command")
            return 2
        print_or_write(result, getattr(args, "out_json", None), getattr(args, "summary", False))
        # Non-zero exit for clear failures, but keep DICOM warning statuses in JSON.
        if result.get("error"):
            return 1
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
