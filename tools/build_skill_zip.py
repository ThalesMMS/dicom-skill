#!/usr/bin/env python3
"""Build the installable Agent Skill archive: dist/dicom-skill.zip.

The archive contains a single top-level ``dicom-skill/`` folder with the files
listed in ``skill/dicom-skill/manifest.txt`` plus the project license. Tests,
caches, and private ``*.local.*`` files are never included.

Run from anywhere:  python tools/build_skill_zip.py [--out dist/dicom-skill.zip]
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skill" / "dicom-skill"
TOP_LEVEL = "dicom-skill"
FIXED_TIME = (2020, 1, 1, 0, 0, 0)  # reproducible archives
PRIVATE_MARKERS = (".local.yaml", ".local.yml", ".local.md")


def manifest_entries() -> list[str]:
    lines = (SKILL_DIR / "manifest.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def check_manifest(entries: list[str]) -> list[str]:
    problems: list[str] = []
    if len(entries) != len(set(entries)):
        problems.append("manifest.txt lists a file more than once")
    for entry in entries:
        path = SKILL_DIR / entry
        if entry.endswith(PRIVATE_MARKERS):
            problems.append(f"private file listed in manifest: {entry}")
        elif ".." in Path(entry).parts or Path(entry).is_absolute():
            problems.append(f"unsafe manifest path: {entry}")
        elif not path.is_file():
            problems.append(f"listed in manifest but missing: {entry}")
    listed = set(entries)
    for folder in ("scripts", "references", "examples", "resources"):
        for path in sorted((SKILL_DIR / folder).rglob("*")):
            relative = path.relative_to(SKILL_DIR).as_posix()
            if not path.is_file() or "__pycache__" in path.parts or path.name == ".DS_Store":
                continue
            if relative.endswith(PRIVATE_MARKERS):
                continue
            if relative not in listed:
                problems.append(f"present but not in manifest: {relative}")
    if "SKILL.md" not in listed:
        problems.append("SKILL.md is not in the manifest")
    return problems


def build(out_path: Path) -> int:
    entries = manifest_entries()
    problems = check_manifest(entries)
    if problems:
        print("Manifest check failed:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    sources = [(SKILL_DIR / entry, f"{TOP_LEVEL}/{entry}") for entry in sorted(entries)]
    sources.append((REPO_ROOT / "LICENSE.txt", f"{TOP_LEVEL}/LICENSE.txt"))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for source, arcname in sources:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, source.read_bytes())
    print(f"Wrote {out_path} ({len(sources)} files, {out_path.stat().st_size} bytes)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO_ROOT / "dist" / "dicom-skill.zip"))
    args = parser.parse_args()
    return build(Path(args.out).expanduser().resolve())


if __name__ == "__main__":
    raise SystemExit(main())
