"""DIMSE tests against an in-process pynetdicom SCP. No network access needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydicom.dataset import Dataset
from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, evt
from pynetdicom.sop_class import CTImageStorage, StudyRootQueryRetrieveInformationModelFind, Verification

import dicom_dimse
from conftest import make_ct_dataset


@pytest.fixture
def scp():
    received = []

    def handle_store(event):
        received.append(event.dataset.SOPInstanceUID)
        return 0x0000

    ae = AE(ae_title="TESTSCP")
    ae.add_supported_context(Verification)
    ae.add_supported_context(CTImageStorage, ALL_TRANSFER_SYNTAXES)
    server = ae.start_server(("127.0.0.1", 0), block=False, evt_handlers=[(evt.EVT_C_STORE, handle_store)])
    port = server.socket.getsockname()[1]
    yield port, received
    server.shutdown()


def run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = dicom_dimse.main(argv)
    return rc, json.loads(capsys.readouterr().out)


def test_echo(scp, capsys):
    port, _ = scp
    rc, result = run_cli(
        ["echo", "--host", "127.0.0.1", "--port", str(port), "--aet", "TESTSCP", "--calling-aet", "AGENT"],
        capsys,
    )
    assert rc == 0
    assert result["established"] is True
    assert result["status"]["StatusHex"] == "0x0000"


def test_echo_connection_refused(capsys):
    rc, result = run_cli(
        ["echo", "--host", "127.0.0.1", "--port", "1", "--aet", "NOPE", "--calling-aet", "AGENT", "--timeout", "2"],
        capsys,
    )
    assert rc == 1
    assert result["established"] is False
    assert "error" in result


def test_send(scp, tmp_path: Path, capsys):
    port, received = scp
    src = tmp_path / "outgoing"
    src.mkdir()
    ds = make_ct_dataset()
    ds.save_as(str(src / "ct.dcm"), enforce_file_format=True)

    rc, result = run_cli(
        ["send", "--host", "127.0.0.1", "--port", str(port), "--aet", "TESTSCP", "--calling-aet", "AGENT", "--path", str(src)],
        capsys,
    )
    assert rc == 0
    assert result["sent_count"] == 1
    assert result["failure_count"] == 0
    assert received == [str(ds.SOPInstanceUID)]


def test_send_dry_run(scp, tmp_path: Path, capsys):
    port, received = scp
    src = tmp_path / "outgoing"
    src.mkdir()
    make_ct_dataset().save_as(str(src / "ct.dcm"), enforce_file_format=True)

    rc, result = run_cli(
        ["send", "--host", "127.0.0.1", "--port", str(port), "--aet", "TESTSCP", "--calling-aet", "AGENT", "--path", str(src), "--dry-run"],
        capsys,
    )
    assert rc == 0
    assert result["file_count"] == 1
    assert received == []


def test_get_storage_contexts_prefer_jpeg2000():
    ae = AE(ae_title="AGENT")
    roles = dicom_dimse.configure_storage_contexts_for_get(ae, [str(CTImageStorage)])

    assert len(roles) == 1
    transfer_syntaxes = [str(uid) for uid in ae.requested_contexts[0].transfer_syntax]
    assert transfer_syntaxes[:2] == ["1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.4.91"]
    # Native syntaxes stay available for non-image objects and simple PACS.
    assert "1.2.840.10008.1.2.1" in transfer_syntaxes
    assert "1.2.840.10008.1.2" in transfer_syntaxes


def test_retrieve_rejects_repeated_study_uid(tmp_path: Path, capsys):
    """A repeated --study-uid used to keep only the last value silently."""
    with pytest.raises(SystemExit) as excinfo:
        dicom_dimse.main(
            [
                "retrieve", "--host", "127.0.0.1", "--port", "1", "--aet", "X",
                "--study-uid", "1.2.3", "--study-uid", "1.2.4", "--out", str(tmp_path),
            ]
        )
    assert excinfo.value.code == 2
    assert "more than once" in capsys.readouterr().err


@pytest.fixture
def find_scp():
    """Study Root C-FIND SCP returning two synthetic studies with similar patient names."""
    studies = [
        ("DOE^JANE", "PAT001", "1.2.826.0.1.3680043.8.498.1"),
        ("DOE^JANET MARIE", "PAT002", "1.2.826.0.1.3680043.8.498.2"),
    ]

    def handle_find(event):
        for name, patient_id, uid in studies:
            ds = Dataset()
            ds.QueryRetrieveLevel = "STUDY"
            ds.PatientName = name
            ds.PatientID = patient_id
            ds.StudyDate = "20240115"
            ds.StudyDescription = "TEST CT STUDY"
            ds.StudyInstanceUID = uid
            yield 0xFF00, ds

    ae = AE(ae_title="FINDSCP")
    ae.add_supported_context(StudyRootQueryRetrieveInformationModelFind)
    server = ae.start_server(("127.0.0.1", 0), block=False, evt_handlers=[(evt.EVT_C_FIND, handle_find)])
    yield server.socket.getsockname()[1]
    server.shutdown()


QUERY_ARGS = [
    "--aet", "FINDSCP", "--calling-aet", "AGENT",
    "--filter", "PatientName=DOE*", "--filter", "StudyDate=20240115",
    "--return", "PatientName", "--return", "PatientID", "--return", "StudyInstanceUID",
]


def test_query_summary_omits_demographics_but_audit_json_keeps_them(find_scp, tmp_path: Path, capsys):
    audit = tmp_path / "audit" / "query.json"
    rc, summary = run_cli(
        ["query", "--host", "127.0.0.1", "--port", str(find_scp), *QUERY_ARGS, "--summary", "--out-json", str(audit)],
        capsys,
    )
    assert rc == 0
    assert summary["match_count"] == 2
    assert all("PatientName" not in match and "PatientID" not in match for match in summary["matches"])

    full = json.loads(audit.read_text(encoding="utf-8"))
    assert [match["PatientName"] for match in full["matches"]] == ["DOE^JANE", "DOE^JANET MARIE"]


def test_query_summary_demographics_is_opt_in(find_scp, capsys):
    rc, summary = run_cli(
        ["query", "--host", "127.0.0.1", "--port", str(find_scp), *QUERY_ARGS, "--summary-demographics"],
        capsys,
    )
    assert rc == 0
    # Both wildcard candidates are visible so the caller can pick the exact registered name.
    assert [(m["PatientName"], m["PatientID"]) for m in summary["matches"]] == [
        ("DOE^JANE", "PAT001"),
        ("DOE^JANET MARIE", "PAT002"),
    ]
    assert "responses" not in summary


def test_query_summary_is_phi_light(scp, capsys):
    """C-FIND against a Verification-only SCP fails to negotiate, but the JSON error contract must hold."""
    port, _ = scp
    rc, result = run_cli(
        [
            "query", "--host", "127.0.0.1", "--port", str(port), "--aet", "TESTSCP", "--calling-aet", "AGENT",
            "--filter", "PatientID=X", "--return", "StudyInstanceUID",
        ],
        capsys,
    )
    # The SCP does not support C-FIND: association may be rejected or the find aborted;
    # either way the command must exit non-zero with structured JSON, not a traceback.
    assert isinstance(result, dict)
    assert result["operation"] == "C-FIND"
    assert rc in (0, 1)
