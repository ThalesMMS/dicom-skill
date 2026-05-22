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
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def package_version(module_name: str, distribution_name: str | None = None) -> str:
    module = import_module(module_name)
    if distribution_name:
        try:
            return version(distribution_name)
        except PackageNotFoundError:
            pass
    return str(getattr(module, "__version__", "unknown"))


def jpeg2000_codec_info() -> dict[str, Any]:
    try:
        from pydicom.pixels.decoders import JPEG2000Decoder, JPEG2000LosslessDecoder
        from pydicom.pixels.encoders import JPEG2000Encoder, JPEG2000LosslessEncoder
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    encoders_ok = bool(JPEG2000LosslessEncoder.is_available and JPEG2000Encoder.is_available)
    decoders_ok = bool(JPEG2000LosslessDecoder.is_available and JPEG2000Decoder.is_available)
    return {
        "ok": encoders_ok and decoders_ok,
        "encoders_ok": encoders_ok,
        "decoders_ok": decoders_ok,
        "encoder_plugins": {
            "lossless": list(JPEG2000LosslessEncoder.available_plugins),
            "lossy": list(JPEG2000Encoder.available_plugins),
        },
        "decoder_plugins": {
            "lossless": list(JPEG2000LosslessDecoder.available_plugins),
            "lossy": list(JPEG2000Decoder.available_plugins),
        },
        "missing_encoder_dependencies": {
            "lossless": list(JPEG2000LosslessEncoder.missing_dependencies),
            "lossy": list(JPEG2000Encoder.missing_dependencies),
        },
        "missing_decoder_dependencies": {
            "lossless": list(JPEG2000LosslessDecoder.missing_dependencies),
            "lossy": list(JPEG2000Decoder.missing_dependencies),
        },
    }


def preview_info() -> dict[str, Any]:
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {
        "ok": "PNG" in Image.registered_extensions().values(),
        "numpy_version": str(getattr(np, "__version__", "unknown")),
        "pillow_version": str(getattr(Image, "__version__", "unknown")),
        "png_writer": "PNG" in Image.registered_extensions().values(),
    }


def video_info() -> dict[str, Any]:
    try:
        import imageio
        import imageio_ffmpeg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    try:
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "imageio_version": str(getattr(imageio, "__version__", "unknown")),
            "imageio_ffmpeg_version": str(getattr(imageio_ffmpeg, "__version__", "unknown")),
            "error": str(exc),
        }
    return {
        "ok": True,
        "imageio_version": str(getattr(imageio, "__version__", "unknown")),
        "imageio_ffmpeg_version": str(getattr(imageio_ffmpeg, "__version__", "unknown")),
        "ffmpeg_path": ffmpeg_path,
    }


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


def pdf_dicomizer_info() -> dict[str, Any]:
    try:
        from pydicom.uid import EncapsulatedPDFStorage
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {
        "ok": str(EncapsulatedPDFStorage) == "1.2.840.10008.5.1.4.1.1.104.1",
        "sop_class_uid": str(EncapsulatedPDFStorage),
        "sop_class_name": EncapsulatedPDFStorage.name,
    }


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


def main() -> int:
    result: dict[str, Any] = {
        "ok": True,
        "packages": {},
        "codecs": {},
        "rsna_anonymizer_script": {},
        "docker": {},
        "ports": {},
    }
    packages = [
        ("pydicom", None, "pydicom"),
        ("pynetdicom", None, "pynetdicom"),
        ("requests", None, "requests"),
        ("yaml", "PyYAML", "PyYAML"),
        ("numpy", None, "numpy"),
        ("pylibjpeg", None, "pylibjpeg"),
        ("openjpeg", "pylibjpeg-openjpeg", "pylibjpeg-openjpeg"),
        ("PIL", "Pillow", "Pillow"),
        ("imageio", None, "imageio"),
        ("imageio_ffmpeg", "imageio-ffmpeg", "imageio-ffmpeg"),
    ]
    for module_name, distribution_name, display_name in packages:
        try:
            result["packages"][display_name] = {"ok": True, "version": package_version(module_name, distribution_name)}
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["packages"][display_name] = {"ok": False, "error": str(exc)}
    result["codecs"]["jpeg2000"] = jpeg2000_codec_info()
    if not result["codecs"]["jpeg2000"].get("ok"):
        result["ok"] = False
    result["codecs"]["preview_png"] = preview_info()
    if not result["codecs"]["preview_png"].get("ok"):
        result["ok"] = False
    result["codecs"]["video_mp4"] = video_info()
    if not result["codecs"]["video_mp4"].get("ok"):
        result["ok"] = False
    result["codecs"]["pdf_dicomizer"] = pdf_dicomizer_info()
    if not result["codecs"]["pdf_dicomizer"].get("ok"):
        result["ok"] = False
    result["rsna_anonymizer_script"] = rsna_script_info()
    if not result["rsna_anonymizer_script"].get("available"):
        result["ok"] = False
    result["docker"] = docker_info()
    result["ports"] = {"4242_busy": port_busy(4242), "8042_busy": port_busy(8042)}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
