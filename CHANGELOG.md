# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- Temporary Orthanc is now receive-only: C-ECHO and C-STORE to its own AE title
  are accepted, C-FIND/C-GET/C-MOVE are refused, and the called AE title is
  checked. New `--dicom-bind` and `--allow-aet AET[@HOST]` options (and
  `--orthanc-dicom-bind` / `--orthanc-allow-aet` on `retrieve`) narrow exposure.
- Senders listed with `--allow-aet` are registered store-only. Orthanc gives a
  registered modality every permission by default, so each entry now disables
  C-FIND, C-GET, C-MOVE, and storage commitment explicitly. Mixing `AET` and
  `AET@HOST` entries is rejected, because host checking applies to all senders.
- The Orthanc REST password is no longer printed by `orthanc_temp.py start` or
  written to C-MOVE audit JSON; output shows `http_password_saved` instead.
- `--out-json` audit files are written owner-only (mode 0600).
- Path components taken from DICOM metadata can no longer be `.`/`..`, and
  every received, exported, or anonymized file is confined to its output
  directory.
- The anonymizer no longer falls back to a salt derived from the site ID and
  project name. Without `--salt`/`--salt-env` it uses a random per-project
  secret file (`--salt-file`, default under `~/.config/dicom-skill/salts/`).
  **Behavior change:** runs that relied on the old implicit salt produce
  different pseudonyms; pass an explicit salt to keep earlier mappings.
- The default Orthanc image is pinned to `orthancteam/orthanc:26.11.1` instead
  of `latest`.

### Added

- `scripts/dicom_cloud_backup.py`: resumable per-study backup to any rclone
  remote, with bounded queries, instance-set verification, lossless
  transcoding of uncompressed studies, atomic uploads, a disk-space watchdog,
  and SQLite state. Example systemd units under `examples/systemd/`.
- `dicom_dimse.py query --summary-demographics` for exact patient
  verification.
- References: clinical request workflow, study delivery, cloud backup runbook,
  temporary Orthanc, troubleshooting, and a private site-workflows template.
- `compatibility` field in the skill frontmatter; explicit rules that
  retrieval authorization does not cover external transfer and that recurring
  backups need their own instruction.
- Anonymizer output reports `salt_source`, `deidentification_complete: false`,
  and the method's limitations.
- `SECURITY.md`, `requirements-tested.txt`, and
  `tools/build_skill_zip.py` for the installable skill archive.

### Changed

- C-GET offers JPEG 2000 and other compressed transfer syntaxes first.
- `retrieve` rejects repeated `--study-uid`/`--series-uid`/`--sop-uid` instead
  of silently using the last value.
- `SKILL.md` is shorter; MP4, Orthanc, and troubleshooting detail moved to
  `references/`.
- README screenshots were sanitized.

## Initial release

- DIMSE C-ECHO/C-FIND/C-GET/C-MOVE/C-STORE, temporary Orthanc receiver, local
  anonymization, JPEG 2000 transcoding, PNG preview, MP4 export, and PDF
  encapsulation.
