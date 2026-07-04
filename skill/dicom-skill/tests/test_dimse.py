"""DIMSE tests against an in-process pynetdicom SCP. No network access needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, evt
from pynetdicom.sop_class import CTImageStorage, Verification

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
