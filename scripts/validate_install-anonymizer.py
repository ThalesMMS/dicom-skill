#!/usr/bin/env python3
"""Validate dicom-skill runtime dependencies."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib import import_module
from pathlib import Path
from typing import Any


def package_version(module_name: str) -> str:
    module = import_module(module_name)
    return str(getattr(module, "__version__", "unknown"))


def docker_info() -> dict[str, Any]:
    docker_path = shutil.which("docker")
    if not docker_path:
        return {"available": False, "reason": "docker executable not found"}
    try:
        cp = subprocess.run(
            ["docker", "version", "--format", "{{json .}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=True,
        )
        return {"available": True, "path": docker_path, "version": json.loads(cp.stdout)}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "path": docker_path, "reason": str(exc)}


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def rsna_script_info() -> dict[str, Any]:
    script_path = Path(__file__).resolve().parent.parent / "resources" / "rsna" / "default-anonymizer.script"
    if not script_path.exists():
        return {"available": False, "path": str(script_path), "reason": "missing"}
    try:
        root = ET.parse(script_path).getroot()
        return {
            "available": True,
            "path": str(script_path),
            "element_rules": len(root.findall("e")),
            "rule_count": len(root.findall("r")),
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "path": str(script_path), "reason": str(exc)}


def main() -> int:
    result: dict[str, Any] = {"ok": True, "packages": {}, "docker": {}, "ports": {}, "rsna_anonymizer_script": {}}
    for name in ["pydicom", "pynetdicom", "requests", "yaml"]:
        try:
            result["packages"][name] = {"ok": True, "version": package_version(name)}
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["packages"][name] = {"ok": False, "error": str(exc)}
    result["docker"] = docker_info()
    result["ports"] = {"4242_busy": port_busy(4242), "8042_busy": port_busy(8042)}
    result["rsna_anonymizer_script"] = rsna_script_info()
    if not result["rsna_anonymizer_script"].get("available"):
        result["ok"] = False
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
