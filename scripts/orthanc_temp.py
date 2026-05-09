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
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover - handled at runtime
    requests = None  # type: ignore


DEFAULT_IMAGE = "orthancteam/orthanc:latest"
DEFAULT_NAME = "dicom-skill-agent-orthanc"
DEFAULT_AET = "AGENT"
DEFAULT_DICOM_PORT = 4242
DEFAULT_HTTP_PORT = 8042


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


def write_config(config_path: Path, aet: str) -> None:
    config = {
        "Name": "AgentTemporaryOrthanc",
        "StorageDirectory": "/var/lib/orthanc/db",
        "IndexDirectory": "/var/lib/orthanc/db",
        "DicomAet": aet,
        "DicomPort": 4242,
        "HttpPort": 8042,
        "RemoteAccessAllowed": True,
        "AuthenticationEnabled": False,
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
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def wait_for_orthanc(http_url: str, timeout: float = 30.0) -> dict[str, Any]:
    require_requests()
    deadline = time.time() + timeout
    last_error: str | None = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{http_url.rstrip('/')}/system", timeout=2.0)  # type: ignore[union-attr]
            if response.ok:
                return response.json()
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Orthanc REST did not become ready at {http_url}: {last_error}")


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
) -> OrthancState:
    ensure_docker()
    if not allow_port_conflict:
        if is_port_busy("127.0.0.1", dicom_port) and not container_running(name):
            raise RuntimeError(f"Host DICOM port {dicom_port} is already in use. Default temporary Orthanc requires AGENT:{dicom_port}.")
        if is_port_busy("127.0.0.1", http_port) and not container_running(name):
            raise RuntimeError(f"Host HTTP port {http_port} is already in use. Use --http-port to select a free local REST port.")

    if container_running(name):
        state = OrthancState(
            name=name,
            aet=aet,
            dicom_port=dicom_port,
            http_port=http_port,
            http_url=f"http://127.0.0.1:{http_port}",
            data_dir=data_dir or "",
            config_path="",
            image=image,
            container_id=container_id_by_name(name),
        )
        wait_for_orthanc(state.http_url, timeout=timeout)
        return state

    old = container_id_by_name(name)
    if old:
        run(["docker", "rm", "-f", name], check=True)

    if data_dir is None:
        data_dir = tempfile.mkdtemp(prefix="dicom-skill-orthanc-")
    data_path = Path(data_dir).resolve()
    storage_path = data_path / "db"
    config_path = data_path / "orthanc.json"
    storage_path.mkdir(parents=True, exist_ok=True)
    write_config(config_path, aet)

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
    )
    (data_path / "orthanc_state.json").write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    wait_for_orthanc(state.http_url, timeout=timeout)
    return state


def stop_temp_orthanc(name: str = DEFAULT_NAME, purge: bool = False, data_dir: str | None = None) -> dict[str, Any]:
    ensure_docker()
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


def status(http_url: str) -> dict[str, Any]:
    require_requests()
    response = requests.get(f"{http_url.rstrip('/')}/system", timeout=5.0)  # type: ignore[union-attr]
    response.raise_for_status()
    return response.json()


def safe_component(value: Any, fallback: str) -> str:
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


def export_instances(http_url: str, out_dir: str, clear_after: bool = False) -> dict[str, Any]:
    require_requests()
    base = http_url.rstrip("/")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    response = requests.get(f"{base}/instances", timeout=10.0)  # type: ignore[union-attr]
    response.raise_for_status()
    instance_ids = response.json()
    exported: list[str] = []
    errors: list[dict[str, Any]] = []

    for instance_id in instance_ids:
        try:
            tag_resp = requests.get(f"{base}/instances/{instance_id}/simplified-tags", timeout=10.0)  # type: ignore[union-attr]
            tags = tag_resp.json() if tag_resp.ok else {}
            study_uid = safe_component(tags.get("StudyInstanceUID"), "unknown_study")
            series_uid = safe_component(tags.get("SeriesInstanceUID"), "unknown_series")
            sop_uid = safe_component(tags.get("SOPInstanceUID"), str(instance_id))
            dest_dir = out / study_uid / series_uid
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{sop_uid}.dcm"
            file_resp = requests.get(f"{base}/instances/{instance_id}/file", timeout=60.0)  # type: ignore[union-attr]
            file_resp.raise_for_status()
            dest.write_bytes(file_resp.content)
            exported.append(str(dest))
            if clear_after:
                requests.delete(f"{base}/instances/{instance_id}", timeout=10.0)  # type: ignore[union-attr]
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

    stop_p = sub.add_parser("stop", help="Stop temporary Orthanc")
    stop_p.add_argument("--name", default=DEFAULT_NAME)
    stop_p.add_argument("--purge", action="store_true")
    stop_p.add_argument("--data-dir")

    status_p = sub.add_parser("status", help="Query Orthanc REST /system")
    status_p.add_argument("--http-url", default=f"http://127.0.0.1:{DEFAULT_HTTP_PORT}")

    export_p = sub.add_parser("export", help="Export all instances currently stored in Orthanc")
    export_p.add_argument("--http-url", default=f"http://127.0.0.1:{DEFAULT_HTTP_PORT}")
    export_p.add_argument("--out", required=True)
    export_p.add_argument("--clear-after", action="store_true")

    return parser


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
            ).to_dict()
        elif args.command == "stop":
            result = stop_temp_orthanc(name=args.name, purge=args.purge, data_dir=args.data_dir)
        elif args.command == "status":
            result = status(args.http_url)
        elif args.command == "export":
            result = export_instances(args.http_url, args.out, clear_after=args.clear_after)
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
