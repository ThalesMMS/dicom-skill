"""Credential/state handling for the temporary Orthanc helper (no Docker needed)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import orthanc_temp


def test_default_data_dir_is_deterministic():
    a = orthanc_temp.default_data_dir("dicom-skill-agent-orthanc")
    b = orthanc_temp.default_data_dir("dicom-skill-agent-orthanc")
    other = orthanc_temp.default_data_dir("another-name")
    assert a == b
    assert a != other
    assert "dicom-skill-orthanc-" in a.name


def write_state(data_dir: Path, name: str, password: str) -> None:
    state = orthanc_temp.OrthancState(
        name=name,
        aet="AGENT",
        dicom_port=4242,
        http_port=8042,
        http_url="http://127.0.0.1:8042",
        data_dir=str(data_dir),
        config_path=str(data_dir / "orthanc.json"),
        image="orthancteam/orthanc:latest",
        container_id="abc123",
        http_user="agent",
        http_password=password,
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "orthanc_state.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")


def test_load_saved_state_round_trip(tmp_path: Path):
    write_state(tmp_path, "myorthanc", "s3cret")
    saved = orthanc_temp.load_saved_state(str(tmp_path))
    assert saved is not None
    assert saved.http_auth == ("agent", "s3cret")


def test_resolve_cli_auth_priority(tmp_path: Path):
    write_state(tmp_path, "myorthanc", "from-state-file")

    explicit = argparse.Namespace(http_user="agent", http_password="explicit", data_dir=str(tmp_path), name="myorthanc")
    assert orthanc_temp.resolve_cli_auth(explicit) == ("agent", "explicit")

    from_state = argparse.Namespace(http_user="agent", http_password=None, data_dir=str(tmp_path), name="myorthanc")
    assert orthanc_temp.resolve_cli_auth(from_state) == ("agent", "from-state-file")

    nothing = argparse.Namespace(http_user="agent", http_password=None, data_dir=str(tmp_path / "missing"), name="myorthanc")
    assert orthanc_temp.resolve_cli_auth(nothing) is None


def test_resolve_cli_auth_uses_default_dir_for_name(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(orthanc_temp.tempfile, "gettempdir", lambda: str(tmp_path))
    default_dir = orthanc_temp.default_data_dir("myorthanc")
    write_state(default_dir, "myorthanc", "auto-found")

    args = argparse.Namespace(http_user="agent", http_password=None, data_dir=None, name="myorthanc")
    assert orthanc_temp.resolve_cli_auth(args) == ("agent", "auto-found")


def test_write_config_registers_user(tmp_path: Path):
    config_path = tmp_path / "orthanc.json"
    orthanc_temp.write_config(config_path, "AGENT", "agent", "pw")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["AuthenticationEnabled"] is True
    assert config["RegisteredUsers"] == {"agent": "pw"}

    orthanc_temp.write_config(config_path, "AGENT")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["AuthenticationEnabled"] is False
    assert "RegisteredUsers" not in config
