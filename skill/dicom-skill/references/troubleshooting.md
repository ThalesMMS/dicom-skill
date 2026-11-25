# Troubleshooting

Symptoms and fixes for the dicom-skill scripts. Prefer C-ECHO as the first
diagnostic step, and keep `--out-json` files from failed commands.

**Association rejected**

Verify called AE title, host, port, TLS/firewall, and whether the remote node allows the calling AE `AGENT`.

**C-FIND returns no matches**

Confirm query model (`study` vs `patient`), level, date format, wildcard policy, and whether the server supports the requested return tags.

**C-FIND `--summary` shows no patient name**

By design. Add `--summary-demographics` or read `matches[]` in the `--out-json` file.

**A wildcard query returns the wrong or several patients**

The match is a candidate list. Compare the full registered `PatientName` and ask the user when candidates differ materially.

**`retrieve` exits with "was given more than once"**

Run one retrieve command per UID.

**C-GET fails during C-STORE suboperations or retrieves only a few images from a large study**

The remote may not support C-GET well or may reject storage presentation contexts. Try C-MOVE directly to a known destination AE, then verify the destination with C-FIND.

**C-MOVE status `0xA801`**

Unknown move destination. Register `AGENT` with host/port on the remote PACS, or use C-GET.

**Retrieved files are JPEG 2000/JPEG-LS and a downstream tool cannot read them**

C-GET prefers compressed syntaxes. Decompress locally with `scripts/dicom_jpeg2000.py decompress` (JPEG 2000) or install the matching pydicom pixel decoder.

**Cloud backup stops with `DiskPressure`, `Remote conflict`, or a size/SHA-1 mismatch**

These are safeguards. Do not delete state, ZIPs, or remote objects to get past them; follow `references/cloud-backup-runbook.md`.

**Cloud backup reports that the remote did not return a usable SHA-1**

The rclone backend does not support SHA-1 hashes. Use a backend that does.

**Temporary Orthanc cannot start**

Docker may be unavailable, the image may be unavailable, or port `4242`/`8042` may already be in use.

**JPEG 2000 compression fails with missing encoder dependencies**

Install `requirements.txt` and rerun `python scripts/validate_install.py`; encoding requires `numpy`, `pylibjpeg`, and `pylibjpeg-openjpeg`.

**PNG preview fails on compressed input**

Install `requirements.txt` and rerun `python scripts/validate_install.py`; JPEG 2000 and other compressed transfer syntaxes require an available pydicom pixel decoder.

**PDF dicomizer output still contains visible patient data**

The PDF is embedded as-is in `EncapsulatedDocument`. Run a separate PDF redaction/de-identification workflow before dicomizing if the document contains PHI that should not be shared.

**Anonymized files still contain visible burned-in annotations**

This skill's anonymizer does not run OCR or pixel masking. Do not share externally until pixel PHI has been reviewed or removed with a separate workflow.

**Anonymization mapping differs across separate runs**

Use `--map-json` to load/update a secure PHI-containing mapping file, or use `--patient-id-strategy hashed` with a stable project salt.

**The temporary Orthanc rejects the incoming C-STORE**

The receiver checks the called AE title and, when `--allow-aet` is used, the calling AE title (and host for `AET@HOST`). Make sure the PACS sends to the same AE title passed as `--aet`/`--destination-aet`, and that the PACS's own storage AE title is in the allow list. Behind Docker NAT the source address can appear as the bridge gateway, so prefer plain `AET` entries unless host pinning was tested.

**A status/export call against the temporary Orthanc returns 401**

The REST password is stored only in the owner-only `orthanc_state.json` inside the helper's data dir. Use the same `--name` (or `--data-dir`) as the `start` command, or set `$DICOM_SKILL_ORTHANC_PASSWORD`.

**Anonymized UIDs differ between two machines**

Each machine generated its own random salt file. Share one project salt through `--salt-env` or copy the salt file over a secure channel.
