#!/usr/bin/env python3
"""Temporary Orthanc helper for dicom-skill.

Creates an ad-hoc Orthanc DICOM receiver intended for C-MOVE destinations.
Default AE title: AGENT. Default host DICOM port: 4242. Default REST port: 8042
bound to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover - handled at runtime
    requests = None  # type: ignore

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _common import safe_path_component, write_bytes_atomic, write_private_text


DEFAULT_IMAGE = "orthancteam/orthanc:latest"
DEFAULT_NAME = "dicom-skill-agent-orthanc"
DEFAULT_AET = "AGENT"
DEFAULT_DICOM_PORT = 4242
DEFAULT_HTTP_PORT = 8042
DEFAULT_HTTP_USER = "agent"
PASSWORD_ENV = "DICOM_SKILL_ORTHANC_PASSWORD"


@dataclass
class OrthancState:
    name: str
    aet: str
    dicom_port: int
    http_port: int
    http_url: str
    data_dir: str
    config_path: str
    image: str
    container_id: str | None = None
    http_user: str | None = None
    http_password: str | None = None

    @property
    def http_auth(self) -> tuple[str, str] | None:
        if self.http_user and self.http_password:
            return (self.http_user, self.http_password)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aet": self.aet,
            "dicom_port": self.dicom_port,
            "http_port": self.http_port,
            "http_url": self.http_url,
            "data_dir": self.data_dir,
            "config_path": self.config_path,
            "image": self.image,
            "container_id": self.container_id,
            "http_user": self.http_user,
            # The password is needed by later status/export commands against this
            # ephemeral, localhost-bound REST API. Treat saved state as sensitive.
            "http_password": self.http_password,
        }


def require_requests() -> None:
    if requests is None:
        raise RuntimeError("The requests package is required. Install skill requirements first.")


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def docker_exists() -> bool:
    return shutil.which("docker") is not None


def ensure_docker() -> None:
    if not docker_exists():
        raise RuntimeError("Docker executable not found. Use C-GET or install/start Docker before C-MOVE with temporary Orthanc.")
    try:
        run(["docker", "version", "--format", "{{.Server.Version}}"], check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Docker is installed but not usable: {exc.stderr.strip() if exc.stderr else exc}") from exc


def is_port_busy(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def container_id_by_name(name: str) -> str | None:
    try:
        cp = run(["docker", "ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"], check=True)
    except Exception:
        return None
    cid = cp.stdout.strip()
    return cid or None


def container_running(name: str) -> bool:
    try:
        cp = run(["docker", "ps", "--filter", f"name=^{name}$", "--filter", "status=running", "--format", "{{.ID}}"], check=True)
    except Exception:
        return False
    return bool(cp.stdout.strip())


def write_config(config_path: Path, aet: str, http_user: str | None = None, http_password: str | None = None) -> None:
    config = {
        "Name": "AgentTemporaryOrthanc",
        "StorageDirectory": "/var/lib/orthanc/db",
        "IndexDirectory": "/var/lib/orthanc/db",
        "DicomAet": aet,
        "DicomPort": 4242,
        "HttpPort": 8042,
        "RemoteAccessAllowed": True,
        "AuthenticationEnabled": bool(http_user and http_password),
        "HttpDescribeErrors": True,
        "DicomCheckCalledAet": False,
        "DicomCheckModalityHost": False,
        "DicomAlwaysAllowEcho": True,
        "DicomAlwaysAllowStore": True,
        "DicomAlwaysAllowFind": True,
        "DicomAlwaysAllowGet": True,
        "DicomAlwaysAllowMove": True,
        "StoreDicom": True,
    }
    if http_user and http_password:
        config["RegisteredUsers"] = {http_user: http_password}
    # The config can contain REST credentials; keep it owner-readable only.
    write_private_text(config_path, json.dumps(config, indent=2))


def wait_for_orthanc(http_url: str, timeout: float = 30.0, auth: tuple[str, str] | None = None) -> dict[str, Any]:
    require_requests()
    deadline = time.time() + timeout
    last_error: str | None = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{http_url.rstrip('/')}/system", timeout=2.0, auth=auth)  # type: ignore[union-attr]
            if response.ok:
                return response.json()
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Orthanc REST did not become ready at {http_url}: {last_error}")


def default_data_dir(name: str = DEFAULT_NAME) -> Path:
    """Deterministic per-container-name data dir.

    Keyed on the container name so separate start/status/export/stop
    invocations land on the same state file (and saved REST credentials)
    without the caller having to thread --data-dir through every command.
    """
    return Path(tempfile.gettempdir()) / f"dicom-skill-orthanc-{safe_path_component(name, 'orthanc')}"


def load_saved_state(data_dir: str | None, name: str = DEFAULT_NAME) -> OrthancState | None:
    """Load the state file written by a previous start in the same data dir."""
    if not data_dir:
        data_dir = str(default_data_dir(name))
    state_path = Path(data_dir).resolve() / "orthanc_state.json"
    if not state_path.exists():
        return None
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        return OrthancState(**{key: raw.get(key) for key in OrthancState.__dataclass_fields__})
    except Exception:  # noqa: BLE001 - a corrupt state file should not block startup
        return None


def start_temp_orthanc(
    *,
    aet: str = DEFAULT_AET,
    dicom_port: int = DEFAULT_DICOM_PORT,
    http_port: int = DEFAULT_HTTP_PORT,
    name: str = DEFAULT_NAME,
    data_dir: str | None = None,
    image: str = DEFAULT_IMAGE,
    pull: bool = False,
    allow_port_conflict: bool = False,
    timeout: float = 30.0,
    http_auth: bool = True,
) -> OrthancState:
    ensure_docker()
    if not allow_port_conflict:
        if is_port_busy("127.0.0.1", dicom_port) and not container_running(name):
            raise RuntimeError(f"Host DICOM port {dicom_port} is already in use. Default temporary Orthanc requires AGENT:{dicom_port}.")
        if is_port_busy("127.0.0.1", http_port) and not container_running(name):
            raise RuntimeError(f"Host HTTP port {http_port} is already in use. Use --http-port to select a free local REST port.")

    if container_running(name):
        saved = load_saved_state(data_dir, name)
        state = OrthancState(
            name=name,
            aet=aet,
            dicom_port=dicom_port,
            http_port=http_port,
            http_url=f"http://127.0.0.1:{http_port}",
            data_dir=data_dir or "",
            config_path=saved.config_path if saved else "",
            image=image,
            container_id=container_id_by_name(name),
            http_user=saved.http_user if saved else None,
            http_password=saved.http_password if saved else None,
        )
        wait_for_orthanc(state.http_url, timeout=timeout, auth=state.http_auth)
        return state

    old = container_id_by_name(name)
    if old:
        run(["docker", "rm", "-f", name], check=True)

    if data_dir is None:
        data_dir = str(default_data_dir(name))
        # Owner-only: the dir will hold the REST password and received DICOM.
        Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    data_path = Path(data_dir).resolve()
    storage_path = data_path / "db"
    config_path = data_path / "orthanc.json"
    storage_path.mkdir(parents=True, exist_ok=True)
    http_user = DEFAULT_HTTP_USER if http_auth else None
    http_password = secrets.token_urlsafe(18) if http_auth else None
    write_config(config_path, aet, http_user, http_password)

    if pull:
        run(["docker", "pull", image], check=True, capture=False)

    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-p",
        f"{dicom_port}:4242",
        "-p",
        f"127.0.0.1:{http_port}:8042",
        "-v",
        f"{config_path}:/etc/orthanc/orthanc.json:ro",
        "-v",
        f"{storage_path}:/var/lib/orthanc/db",
        image,
    ]
    cp = run(cmd, check=True)
    cid = cp.stdout.strip()
    state = OrthancState(
        name=name,
        aet=aet,
        dicom_port=dicom_port,
        http_port=http_port,
        http_url=f"http://127.0.0.1:{http_port}",
        data_dir=str(data_path),
        config_path=str(config_path),
        image=image,
        container_id=cid,
        http_user=http_user,
        http_password=http_password,
    )
    # The state file contains the REST password; keep it owner-readable only.
    write_private_text(data_path / "orthanc_state.json", json.dumps(state.to_dict(), indent=2))
    wait_for_orthanc(state.http_url, timeout=timeout, auth=state.http_auth)
    return state


def stop_temp_orthanc(name: str = DEFAULT_NAME, purge: bool = False, data_dir: str | None = None) -> dict[str, Any]:
    ensure_docker()
    if purge and not data_dir:
        # Only fall back to the derived default dir when it is clearly ours.
        candidate = default_data_dir(name)
        if (candidate / "orthanc_state.json").exists():
            data_dir = str(candidate)
    existed = container_id_by_name(name) is not None
    running = container_running(name)
    if existed:
        run(["docker", "rm", "-f", name], check=True)
    purged = False
    if purge and data_dir:
        path = Path(data_dir).resolve()
        if path.exists() and path.is_dir():
            shutil.rmtree(path)
            purged = True
    return {"container": name, "existed": existed, "was_running": running, "purged": purged, "data_dir": data_dir}


def status(http_url: str, auth: tuple[str, str] | None = None) -> dict[str, Any]:
    require_requests()
    response = requests.get(f"{http_url.rstrip('/')}/system", timeout=5.0, auth=auth)  # type: ignore[union-attr]
    response.raise_for_status()
    return response.json()


def export_instances(http_url: str, out_dir: str, clear_after: bool = False, auth: tuple[str, str] | None = None) -> dict[str, Any]:
    require_requests()
    base = http_url.rstrip("/")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    session = requests.Session()  # type: ignore[union-attr]
    if auth:
        session.auth = auth
    response = session.get(f"{base}/instances", timeout=10.0)
    response.raise_for_status()
    instance_ids = response.json()
    exported: list[str] = []
    errors: list[dict[str, Any]] = []

    for instance_id in instance_ids:
        try:
            tag_resp = session.get(f"{base}/instances/{instance_id}/simplified-tags", timeout=10.0)
            tags = tag_resp.json() if tag_resp.ok else {}
            study_uid = safe_path_component(tags.get("StudyInstanceUID"), "unknown_study")
            series_uid = safe_path_component(tags.get("SeriesInstanceUID"), "unknown_series")
            sop_uid = safe_path_component(tags.get("SOPInstanceUID"), str(instance_id))
            dest = out / study_uid / series_uid / f"{sop_uid}.dcm"
            file_resp = session.get(f"{base}/instances/{instance_id}/file", timeout=60.0)
            file_resp.raise_for_status()
            write_bytes_atomic(dest, file_resp.content)
            exported.append(str(dest))
            if clear_after:
                session.delete(f"{base}/instances/{instance_id}", timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            errors.append({"instance_id": instance_id, "error": str(exc)})

    return {"http_url": http_url, "out_dir": str(out), "instance_count": len(instance_ids), "exported_count": len(exported), "exported": exported, "errors": errors}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage a temporary Orthanc receiver for dicom-skill.")
    sub = parser.add_subparsers(dest="command", required=True)

    start_p = sub.add_parser("start", help="Start temporary Orthanc AGENT receiver")
    start_p.add_argument("--aet", default=DEFAULT_AET)
    start_p.add_argument("--dicom-port", type=int, default=DEFAULT_DICOM_PORT)
    start_p.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT)
    start_p.add_argument("--name", default=DEFAULT_NAME)
    start_p.add_argument("--data-dir")
    start_p.add_argument("--image", default=DEFAULT_IMAGE)
    start_p.add_argument("--pull", action="store_true")
    start_p.add_argument("--allow-port-conflict", action="store_true")
    start_p.add_argument("--timeout", type=float, default=30.0)
    start_p.add_argument("--no-http-auth", action="store_true", help="Start the REST API without authentication (not recommended)")

    stop_p = sub.add_parser("stop", help="Stop temporary Orthanc")
    stop_p.add_argument("--name", default=DEFAULT_NAME)
    stop_p.add_argument("--purge", action="store_true")
    stop_p.add_argument("--data-dir")

    def add_auth_args(sub_parser: argparse.ArgumentParser) -> None:
        sub_parser.add_argument("--http-url", default=f"http://127.0.0.1:{DEFAULT_HTTP_PORT}")
        sub_parser.add_argument("--name", default=DEFAULT_NAME, help="Container name used by the start command; locates saved credentials")
        sub_parser.add_argument("--http-user", default=DEFAULT_HTTP_USER, help=f"REST user; default {DEFAULT_HTTP_USER}")
        sub_parser.add_argument(
            "--http-password",
            default=os.environ.get(PASSWORD_ENV),
            help=f"REST password printed by the start command; defaults to ${PASSWORD_ENV}",
        )
        sub_parser.add_argument("--data-dir", help="Data dir from the start command; default is derived from --name")

    status_p = sub.add_parser("status", help="Query Orthanc REST /system")
    add_auth_args(status_p)

    export_p = sub.add_parser("export", help="Export all instances currently stored in Orthanc")
    add_auth_args(export_p)
    export_p.add_argument("--out", required=True)
    export_p.add_argument("--clear-after", action="store_true")

    return parser


def resolve_cli_auth(args: argparse.Namespace) -> tuple[str, str] | None:
    if args.http_password:
        return (args.http_user, args.http_password)
    saved = load_saved_state(getattr(args, "data_dir", None), getattr(args, "name", DEFAULT_NAME))
    if saved and saved.http_auth:
        return saved.http_auth
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            result = start_temp_orthanc(
                aet=args.aet,
                dicom_port=args.dicom_port,
                http_port=args.http_port,
                name=args.name,
                data_dir=args.data_dir,
                image=args.image,
                pull=args.pull,
                allow_port_conflict=args.allow_port_conflict,
                timeout=args.timeout,
                http_auth=not args.no_http_auth,
            ).to_dict()
        elif args.command == "stop":
            result = stop_temp_orthanc(name=args.name, purge=args.purge, data_dir=args.data_dir)
        elif args.command == "status":
            result = status(args.http_url, auth=resolve_cli_auth(args))
        elif args.command == "export":
            result = export_instances(args.http_url, args.out, clear_after=args.clear_after, auth=resolve_cli_auth(args))
        else:  # pragma: no cover
            parser.error("Unknown command")
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
