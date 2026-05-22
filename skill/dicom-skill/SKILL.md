---
name: dicom-skill
description: Perform DICOM DIMSE C-ECHO, C-FIND, C-GET/C-MOVE retrieval, C-STORE send operations, local DICOM anonymization, PDF-to-DICOM encapsulation, JPEG 2000 compression/decompression, DICOM-to-PNG preview rendering, and local DICOM image-series MP4 export from an agent shell. Includes helper scripts to create a temporary Orthanc receiver with AE title AGENT on DICOM port 4242 when a C-MOVE destination is needed.
---

# dicom-skill

Use this skill when the user asks an agent to interact with DICOM nodes/PACS/VNA/Orthanc using DIMSE operations: verify connectivity, query metadata, retrieve instances, send DICOM files, anonymize local DICOM files, wrap local PDFs as DICOM Encapsulated PDF instances, locally compress/decompress DICOM pixel data with JPEG 2000, render local DICOM instances to PNG previews, or export local DICOM image series to MP4 videos.

This is a shell/CLI skill, not an MCP server. Do not start an MCP server. Use the scripts in `scripts/` directly.

## Safety and data handling

DICOM metadata and files can contain patient-identifying information. Do not connect to clinical systems, retrieve studies, send images, anonymize clinical payloads, export videos, or encapsulate PDFs as DICOM unless the user has authorization and has provided the endpoint or file details. Keep outputs in local, user-controlled folders. Avoid printing full patient data unless the user explicitly asks for it. PNG previews, MP4 videos, and encapsulated PDFs can preserve visible PHI already present in pixel data or document contents. Anonymization must be an explicit local file operation; never assume retrieved, received, dicomized, previewed, or video-exported files are de-identified until `scripts/dicom_anonymize.py` has been run and checked.

For troubleshooting, prefer C-ECHO first. For data movement, prefer the least invasive operation that satisfies the request. When the user wants to copy studies from one DICOM node to another known DICOM destination, prefer C-MOVE directly to that destination AE over C-GET + C-STORE. Never delete, overwrite, or modify remote DICOM data from this skill. The anonymizer removes metadata PHI according to the bundled script, but it does not perform OCR or burned-in pixel PHI removal.

## Install dependencies

From the skill folder:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/validate_install.py
```

The DIMSE, anonymization, PDF dicomizer, JPEG 2000, PNG preview, and MP4 export scripts require Python packages only. JPEG 2000 encoding requires `pylibjpeg-openjpeg`; PNG preview rendering requires `Pillow`; MP4 export requires `imageio` and `imageio-ffmpeg`. The temporary Orthanc helper requires Docker and the `orthancteam/orthanc` image, pulled automatically by Docker if absent.

## Configuration

Use either explicit command-line parameters or a YAML config. Minimal config:

```yaml
calling_aet: AGENT
nodes:
  orthanc:
    host: localhost
    port: 4242
    ae_title: ORTHANC
```

Save as `dicom_nodes.yaml`, then use `--config dicom_nodes.yaml --node orthanc`.

## Core commands

### C-ECHO

```bash
python scripts/dicom_dimse.py echo \
  --host 127.0.0.1 --port 4242 --aet ORTHANC --calling-aet AGENT
```

### C-FIND query

Study-level query by date and modality:

```bash
python scripts/dicom_dimse.py query \
  --host pacs.local --port 104 --aet PACS --calling-aet AGENT \
  --model study --level STUDY \
  --filter StudyDate=20250101-20250131 \
  --filter ModalitiesInStudy=CT \
  --return PatientName --return PatientID --return AccessionNumber \
  --return StudyDate --return StudyDescription --return StudyInstanceUID
```

Common levels: `PATIENT`, `STUDY`, `SERIES`, `IMAGE` (`INSTANCE` is accepted as an alias for `IMAGE`). Use DICOM keywords such as `PatientID`, `AccessionNumber`, `StudyInstanceUID`, or hex tags such as `00100020`.

### Retrieve with C-GET

Use C-GET for local downloads when there is no known/registered destination AE. It does not require the remote PACS to know a separate destination AE, but some PACS reject many C-STORE suboperations during C-GET. If the source study uses only a known SOP Class (for example CT Image Storage), you can narrow incoming C-STORE negotiation with one or more `--store-sop-class` flags to avoid exceeding the association presentation-context limit. If the final status has failed suboperations or the received count is lower than expected, do **not** assume the study was copied; try C-MOVE to a known destination AE and verify there.

```bash
python scripts/dicom_dimse.py retrieve \
  --method get \
  --host pacs.local --port 104 --aet PACS --calling-aet AGENT \
  --study-uid 1.2.3.4.5 \
  --out /mnt/data/dicom_downloads \
  --summary --out-json /mnt/data/audit/retrieve_get.json
```

### Transfer a study to a known DICOM destination with C-MOVE

Use this for source → backup/PACS copies when the source already knows the destination AE. This avoids writing a local partial copy and is usually more reliable for large studies than C-GET + C-STORE.

```bash
python scripts/dicom_dimse.py retrieve \
  --method move --no-temp-orthanc \
  --host pacs.local --port 104 --aet PACS --calling-aet AGENT \
  --destination-aet BACKUP \
  --study-uid 1.2.3.4.5 \
  --out /mnt/data/audit/move_out \
  --summary --out-json /mnt/data/audit/move_to_backup.json
```

After the move, verify on the destination with C-FIND using the same `StudyInstanceUID` and compare `NumberOfStudyRelatedInstances` when available:

```bash
python scripts/dicom_dimse.py query \
  --host backup.local --port 104 --aet BACKUP --calling-aet AGENT \
  --model study --level STUDY \
  --filter StudyInstanceUID=1.2.3.4.5 \
  --return StudyDate --return StudyDescription --return ModalitiesInStudy \
  --return NumberOfStudyRelatedInstances --return StudyInstanceUID \
  --summary --out-json /mnt/data/audit/verify_backup.json
```

### Retrieve with C-MOVE into temporary Orthanc AGENT:4242

Use this when a C-MOVE destination is required. The script can start a temporary Orthanc receiver with AE title `AGENT` and host DICOM port `4242`, then request C-MOVE to destination `AGENT`, export received instances via Orthanc REST, and stop the container.

```bash
python scripts/dicom_dimse.py retrieve \
  --method move --use-temp-orthanc \
  --host pacs.local --port 104 --aet PACS --calling-aet AGENT \
  --destination-aet AGENT \
  --study-uid 1.2.3.4.5 \
  --out /mnt/data/dicom_downloads
```

Important C-MOVE requirement: the remote PACS must already know that destination AE `AGENT` is reachable at the agent machine address and port `4242`. If the PACS responds with status `0xA801`, register `AGENT` on the PACS/Orthanc side or use C-GET instead.

If host port 4242 is occupied, the helper will fail by default because this skill defaults to the requested `AGENT:4242` receiver. Use a different `--orthanc-dicom-port` only if the remote PACS is configured for that different port.

### Send with C-STORE

```bash
python scripts/dicom_dimse.py send \
  --host destination.local --port 104 --aet DEST_AE --calling-aet AGENT \
  --path /mnt/data/study_or_file
```

For large folders, use `--dry-run` first to verify file discovery. When sending JPEG2000-compressed studies or report objects, use `dicom_dimse.py send` normally; it negotiates the exact transfer syntax present in the files instead of offering every transfer syntax at once.

### JPEG 2000 local compression/decompression

Use `scripts/dicom_jpeg2000.py` only on local DICOM files. It does not connect to DICOM nodes. The default compression syntax is JPEG 2000 Lossless (`1.2.840.10008.1.2.4.90`). Non-pixel DICOM objects such as Encapsulated PDF/SR reports are passed through unchanged so complete mixed studies can be compressed and sent as one output folder.

```bash
python scripts/dicom_jpeg2000.py compress \
  --path /mnt/data/dicom_downloads \
  --out /mnt/data/dicom_j2k \
  --summary --out-json /mnt/data/audit/jpeg2000_compress.json
```

Decompress compressed DICOM files back to Explicit VR Little Endian:

```bash
python scripts/dicom_jpeg2000.py decompress \
  --path /mnt/data/dicom_j2k \
  --out /mnt/data/dicom_uncompressed \
  --summary --out-json /mnt/data/audit/jpeg2000_decompress.json
```

Run `--dry-run --include-files` first for large folders. By default, transcoding generates a new SOP Instance UID; use `--keep-instance-uid` only when preserving the original UID is intentional for the workflow. Lossy JPEG 2000 requires explicit opt-in with `--syntax lossy` plus `--j2k-cr` or `--j2k-psnr`.

### DICOM to PNG preview

Use `scripts/dicom_preview.py` to render a local DICOM instance to PNG for visual inspection. It applies grayscale Modality LUT and VOI LUT/windowing when present, handles `MONOCHROME1` inversion, and emits JSON without patient demographics.

```bash
python scripts/dicom_preview.py \
  --path /mnt/data/dicom_downloads/instance.dcm \
  --out /mnt/data/previews \
  --summary --out-json /mnt/data/audit/preview.json
```

For multiframe instances, the default is the first frame. Use `--frame 5` for a specific 1-based frame or `--all-frames` to render every frame. Use `--max-size 1024` for a smaller preview, or manual grayscale windowing with `--window-center` and `--window-width`.

### DICOM image series to MP4

Use `scripts/dicom_volume_video.py` to export local DICOM image series from a study folder to MP4. By default it applies modality-specific automatic policies:

- CT: export series with more than 100 images at 10 frames/sec.
- MR: export series with 15-100 images at 3 frames/sec.
- MR: export series with more than 100 images at 10 frames/sec.
- Other modalities: list the study series and ask the user which series and frame rate to export.

```bash
python scripts/dicom_volume_video.py \
  --path /mnt/data/dicom_downloads/study \
  --out /mnt/data/videos \
  --summary --out-json /mnt/data/audit/video_export.json
```

To inspect available series before export:

```bash
python scripts/dicom_volume_video.py \
  --path /mnt/data/dicom_downloads/study \
  --out /mnt/data/videos \
  --list-series --include-descriptions \
  --summary --out-json /mnt/data/audit/series_list.json
```

To export one or more specific series, use `--series-uid`, `--series-number`, or `--series-description-contains`. For modalities other than CT or MR, include `--frame-rate` after asking the user for the desired playback speed:

```bash
python scripts/dicom_volume_video.py \
  --path /mnt/data/dicom_downloads/study \
  --out /mnt/data/videos \
  --series-number 3 \
  --plane axial --plane sagittal --plane coronal \
  --frame-rate 15 \
  --summary --out-json /mnt/data/audit/video_series3.json
```

The script applies grayscale Modality LUT/rescale, uses manual `--window-center/--window-width` when supplied, otherwise uses DICOM window values when present, and falls back to percentile/min-max normalization. Use `--percentile-window 0.5,99.5`, `--max-size`, `--reverse`, or `--overwrite` when needed. MP4 exports can contain burned-in annotations and must be treated as sensitive.

Selection rules:

- Without a series selector, export only CT/MR series matching the automatic policy.
- With `--series-uid`, `--series-number`, or `--series-description-contains`, export matching series even when they do not match automatic thresholds.
- For selected non-CT/non-MR series, `--frame-rate` is required.
- Without `--plane`, export only axial MP4 output. Use repeated `--plane` flags or comma-separated values for sagittal and coronal output.
- Without `--frame-rate`, use the CT/MR modality default only when one exists.

### PDF dicomizer

Use `scripts/dicom_pdf.py` to wrap a local PDF as a DICOM Encapsulated PDF Storage instance (`1.2.840.10008.5.1.4.1.1.104.1`). It does not OCR, rasterize, inspect, or anonymize the PDF contents.

```bash
python scripts/dicom_pdf.py \
  --pdf /mnt/data/reports/report.pdf \
  --out /mnt/data/dicomized_pdf \
  --patient-id TEST123 \
  --patient-name "Test^Patient" \
  --summary --out-json /mnt/data/audit/pdf_dicomize.json
```

To attach a PDF to an existing study, use `--metadata-from /path/to/reference.dcm` and override any fields that should differ. The output DICOM uses `MIMETypeOfEncapsulatedDocument=application/pdf` and defaults `BurnedInAnnotation=YES` because the PDF may contain visible PHI.

### Anonymize local DICOM files

Use this only for files already present on disk. It does not contact remote DICOM nodes. The bundled default script is `resources/rsna/default-anonymizer.script`, derived from the RSNA anonymizer script. It removes private groups by default, removes elements not retained by the script, applies `@uid`, `@ptid`, `@acc`, `@hashdate`, `@empty`, and `@round` operations, then writes anonymized DICOM files under the output directory.

```bash
python scripts/dicom_anonymize.py \
  --path /mnt/data/dicom_downloads \
  --out /mnt/data/anonymized \
  --site-id 123456 \
  --summary \
  --out-json /mnt/data/audit/anonymize.json
```

Use `--salt-env ENV_NAME` or `--salt VALUE` for deterministic pseudonymization. Use `--map-json /secure/path/anon_map.json` only when the user needs mappings to persist across runs; the mapping file can contain PHI and must be stored securely. Use `--include-files` only when needed because source paths can contain PHI.

Before sending anonymized output to another DICOM node, run a dry-run C-STORE against the anonymized output folder and keep the anonymization JSON audit artifact.

## Cleanup after successful operations

After the transfer/retrieve/send/transcoding/preview/dicomizer/anonymization operation is complete and verification has passed, clear temporary ZIP, DICOM payload, PDF copies, and PNG preview files created in the workspace. Keep audit artifacts (`*.json`, `*.log`, summaries, study UID lists) unless the user asks to remove them too.

Typical temporary payloads to remove:

- Extracted ZIP folders such as `$AUDIT/extracted`
- Local C-GET/download folders such as `$AUDIT/downloads`
- Temporary C-MOVE/export payload folders such as `$AUDIT/move_out`
- Local preview folders such as `$AUDIT/previews`
- Temporary local MP4 export test folders such as `$AUDIT/video_test_exports`
- Local PDF dicomizer output folders such as `$AUDIT/dicomized_pdf`
- Workspace-local copies of inbound ZIPs, if any were created only for the operation

Do not delete anonymized exports or mapping JSON files unless the user explicitly asks. Mapping JSON files can contain PHI.

Prefer a recoverable delete when available:

```bash
for p in "$AUDIT/extracted" "$AUDIT/downloads" "$AUDIT/move_out" "$AUDIT/previews" "$AUDIT/video_test_exports"; do
  [ -e "$p" ] || continue
  if command -v trash >/dev/null 2>&1; then trash "$p"; else rm -rf "$p"; fi
done
```

Only remove paths inside the explicit workspace/audit directory for the current operation. Never delete remote DICOM data, user source archives outside the workspace, requested MP4 exports, anonymized payloads, or audit logs by default.

## Temporary Orthanc helper

Manual lifecycle:

```bash
python scripts/orthanc_temp.py start --aet AGENT --dicom-port 4242 --http-port 8042
python scripts/dicom_dimse.py echo --host 127.0.0.1 --port 4242 --aet AGENT --calling-aet AGENT
python scripts/orthanc_temp.py export --out /mnt/data/orthanc_export
python scripts/orthanc_temp.py stop --purge
```

The helper exposes Orthanc REST only on `127.0.0.1:8042` by default, while DICOM listens on host port `4242`. It configures the temporary Orthanc to accept incoming C-STORE and C-ECHO without modality host checks.

## Agent workflow

1. Identify the remote node: host/IP, port, called AE title, and local/calling AE title. Default local AE is `AGENT`.
2. Run C-ECHO before query/retrieve/send.
3. For query, keep return tags focused. Include `StudyInstanceUID`, `SeriesInstanceUID`, or `SOPInstanceUID` if the next step may retrieve data.
4. For source → known destination copies, prefer C-MOVE directly to the destination AE and then verify on the destination. Use C-GET primarily for local downloads or when no destination AE is registered.
5. If C-GET returns failed suboperations or retrieves fewer instances than expected, switch to C-MOVE instead of C-STORE-ing a partial local folder.
6. For anonymization, process only explicit local input paths, write to a separate output directory, save `--out-json`, and do not claim pixel PHI has been removed.
7. For local JPEG 2000 compression/decompression, keep source and output folders separate, run a dry-run on large folders, and persist the JSON result with `--out-json`.
8. For PDF dicomization, keep PDF sources and generated DICOM output separate, prefer `--metadata-from` when attaching to an existing study, and do not claim PDF contents were anonymized.
9. For PNG preview rendering, keep previews in an explicit output folder, use `--max-size` when a compact visual check is enough, and remember burned-in pixel annotations can remain visible.
10. For MP4 export, run `--list-series` when the user has not provided a specific series and the folder may contain multiple studies or non-CT/non-MR modalities. Default CT to series with more than 100 images at 10 frames/sec. Default MR to 15-100 images at 3 frames/sec and more than 100 images at 10 frames/sec. For other modalities, ask which series and frame rate to export before running the export command.
11. For send, verify the destination with C-ECHO, run a dry-run on the file set, then send. If the user requests anonymized transfer, send only from the anonymized output directory.
12. Save full command output JSON with `--out-json` for auditability and use `--summary` for terminal/chat output on large studies.
13. After successful verification, clear temporary ZIP/DICOM/PDF/PNG payload files created in the workspace while preserving audit JSON/logs and requested MP4 exports by default.

## Troubleshooting

Association rejected: verify called AE title, host, port, TLS/firewall, and whether the remote node allows the calling AE `AGENT`.

C-FIND returns no matches: confirm query model (`study` vs `patient`), level, date format, wildcard policy, and whether the server supports the requested return tags.

C-GET fails during C-STORE suboperations or retrieves only a few images from a large study: the remote may not support C-GET well or may reject storage presentation contexts. Try C-MOVE directly to a known destination AE, then verify the destination with C-FIND.

C-MOVE status `0xA801`: unknown move destination. Register `AGENT` with host/port on the remote PACS, or use C-GET.

Temporary Orthanc cannot start: Docker may be unavailable, the image may be unavailable, or port `4242`/`8042` may already be in use.

JPEG 2000 compression fails with missing encoder dependencies: install `requirements.txt` and rerun `python scripts/validate_install.py`; encoding requires `numpy`, `pylibjpeg`, and `pylibjpeg-openjpeg`.

PNG preview fails on compressed input: install `requirements.txt` and rerun `python scripts/validate_install.py`; JPEG 2000 and other compressed transfer syntaxes require an available pydicom pixel decoder.

PDF dicomizer output still contains visible patient data: the PDF is embedded as-is in `EncapsulatedDocument`. Run a separate PDF redaction/de-identification workflow before dicomizing if the document contains PHI that should not be shared.

Anonymized files still contain visible burned-in annotations: this skill's anonymizer does not run OCR or pixel masking. Do not share externally until pixel PHI has been reviewed or removed with a separate workflow.

Anonymization mapping differs across separate runs: use `--map-json` to load/update a secure PHI-containing mapping file, or use `--patient-id-strategy hashed` with a stable project salt.
