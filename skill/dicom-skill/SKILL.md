---
name: dicom-skill
description: Perform DICOM DIMSE C-ECHO, C-FIND, C-GET/C-MOVE retrieval, C-STORE send operations, local DICOM anonymization, PDF-to-DICOM encapsulation, JPEG 2000 compression/decompression, DICOM-to-PNG preview rendering, and local DICOM image-series MP4 export from an agent shell. Covers patient exam requests with exact patient-name verification and source fallback, study/patient listings, ZIP packaging for delivery, and resumable per-study backup to cloud storage via rclone. Includes helper scripts to create a temporary Orthanc receiver with AE title AGENT on DICOM port 4242 when a C-MOVE destination is needed.
compatibility: Requires Python 3.10+ with the packages in requirements.txt, a shell, and network access to the DICOM nodes. Docker is needed only for the temporary Orthanc receiver; the rclone CLI only for cloud backup.
---

# dicom-skill

Use this skill when the user asks an agent to interact with DICOM nodes/PACS/VNA/Orthanc using DIMSE operations: verify connectivity, query metadata, retrieve instances, send DICOM files, anonymize local DICOM files, wrap local PDFs as DICOM Encapsulated PDF instances, locally compress/decompress DICOM pixel data with JPEG 2000, render local DICOM instances to PNG previews, or export local DICOM image series to MP4 videos. It also covers the routine requests built on those operations: finding a named patient's exam and verifying it is the right patient, listing the studies of a day, packaging a retrieved study for delivery, and running an unattended per-study backup to cloud storage.

This is a shell/CLI skill, not an MCP server. Do not start an MCP server. Use the scripts in `scripts/` directly.

## Safety and data handling

DICOM metadata and files can contain patient-identifying information. Do not connect to clinical systems, retrieve studies, send images, anonymize clinical payloads, export videos, or encapsulate PDFs as DICOM unless the user has authorization and has provided the endpoint or file details. Keep outputs in local, user-controlled folders. Avoid printing full patient data unless the user explicitly asks for it. PNG previews, MP4 videos, and encapsulated PDFs can preserve visible PHI already present in pixel data or document contents. Anonymization must be an explicit local file operation; never assume retrieved, received, dicomized, previewed, or video-exported files are de-identified until `scripts/dicom_anonymize.py` has been run and checked.

For troubleshooting, prefer C-ECHO first. For data movement, prefer the least invasive operation that satisfies the request. When the user wants to copy studies from one DICOM node to another known DICOM destination, prefer C-MOVE directly to that destination AE over C-GET + C-STORE. Never delete, overwrite, or modify remote DICOM data from this skill. The anonymizer removes metadata PHI according to the bundled script, but it does not perform OCR or burned-in pixel PHI removal, so its output is not guaranteed to be fully de-identified.

Authorization is per action and per destination. Permission to query or retrieve a study does not cover sending it anywhere else. Before any transfer that leaves the DICOM network (a chat or messaging attachment, e-mail, a cloud drive, an rclone remote, any third-party service), make sure the user asked for that specific delivery to that specific destination; if they only asked to find or download the exam, ask first. Previews, videos, ZIPs, and audit JSON are identifiable data unless they were produced from anonymized files and checked. Recurring or unattended transfers (scheduled jobs, the cloud backup lanes, timers) need their own explicit instruction that names the source, the destination, and the schedule; never set one up as a side effect of a one-off request, and do not widen an existing schedule on your own.

## Install dependencies

From the skill folder:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/validate_install.py
```

The DIMSE, anonymization, PDF dicomizer, JPEG 2000, PNG preview, and MP4 export scripts require Python packages only. JPEG 2000 encoding requires `pylibjpeg-openjpeg`; PNG preview rendering requires `Pillow`; MP4 export requires `imageio` and `imageio-ffmpeg`. The temporary Orthanc helper requires Docker and the `orthancteam/orthanc` image, pulled automatically by Docker if absent. The cloud backup script additionally requires the `rclone` CLI with a configured remote; `validate_install.py` reports Docker and rclone as optional.

## Configuration

Use either explicit command-line parameters or a YAML config. Minimal config:

```yaml
calling_aet: AGENT
current_node: orthanc
# Optional: ordered source nodes to try when the user names an exam but not a
# node. Query each in order and stop at the first match.
source_fallback:
  - orthanc
  - pacs
nodes:
  orthanc:
    host: localhost
    port: 4242
    ae_title: ORTHANC
  pacs:
    host: pacs.example.local
    port: 104
    ae_title: PACS
```

Save as `dicom_nodes.yaml`, then use `--config dicom_nodes.yaml --node orthanc`.

Keep real, machine-specific endpoints out of the repository: copy `examples/dicom_nodes.local.yaml.example` to `examples/dicom_nodes.local.yaml` (already gitignored), fill in your nodes, and pass `--config examples/dicom_nodes.local.yaml`. When the user asks for an exam and does not name a node, query the nodes in `source_fallback` order and stop at the first match.

Site conventions that are not endpoints (what a shorthand request means, where deliveries go, how the chat client attaches files) belong in a private `references/site-workflows.local.md`, created from `references/site-workflows.local.md.example` and also gitignored. Read it when it exists.

## Core commands

### C-ECHO

```bash
python scripts/dicom_dimse.py echo \
  --host 127.0.0.1 --port 4242 --aet ORTHANC --calling-aet AGENT
```

All DIMSE commands accept TLS flags when the remote node requires DICOM over TLS: `--tls` enables TLS with system CA verification, `--tls-ca ca.pem` supplies a private CA bundle, `--tls-cert client.pem --tls-key client.key` add a client certificate for mutual TLS, and `--tls-no-verify` disables verification for testing only. Without these flags the association uses plain TCP.

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

When the user asks for a patient's exam without giving a date, default to a study-level query filtered by today's date (`StudyDate=YYYYMMDD`) plus a patient-name wildcard, and widen the date range only if there is no match. When the user asks for all exams or explicitly sets no date limit, omit `StudyDate` instead of defaulting to today. Many source nodes support only `patient` and `study` query models, not `series`. Do not force a series-level C-FIND: retrieve the study locally and inspect its series with the tools below instead.

`--summary` is PHI-light: C-FIND matches show date, time, modalities, description, UID, and instance count, and omit `PatientName`/`PatientID` even when requested with `--return`. The full identifiers are always in the `--out-json` file (`matches[]` and `responses[].identifier`). Add `--summary-demographics` when the registered patient must be visible in the terminal output, which is the case before any patient-specific retrieval (see "Patient exam requests" below).

### Retrieve with C-GET

Use C-GET for local downloads when there is no known/registered destination AE. It does not require the remote PACS to know a separate destination AE, but some PACS reject many C-STORE suboperations during C-GET. If the source study uses only a known SOP Class (for example CT Image Storage), you can narrow incoming C-STORE negotiation with one or more `--store-sop-class` flags to avoid exceeding the association presentation-context limit. If the final status has failed suboperations or the received count is lower than expected, do **not** assume the study was copied; try C-MOVE to a known destination AE and verify there.

C-GET offers JPEG 2000 first, then JPEG-LS, JPEG, RLE, and the native syntaxes, so a PACS that stores or can transcode to a compressed syntax sends compact files. Instances are written exactly as received; check the transfer syntax (or run `scripts/dicom_jpeg2000.py decompress`) if a downstream tool needs uncompressed pixel data.

Each retrieve command handles one UID. `--study-uid`, `--series-uid`, and `--sop-uid` are rejected when repeated; for several studies run one command per `StudyInstanceUID`, each with its own `--out-json`.

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

Use `scripts/dicom_volume_video.py` to export local DICOM image series from a study folder to MP4. When a study contains multiple series, run `--list-series --include-descriptions` first and export the exact series by `--series-number`/`--series-uid` rather than trusting the auto-selected default.

```bash
python scripts/dicom_volume_video.py \
  --path /mnt/data/dicom_downloads/study \
  --out /mnt/data/videos \
  --list-series --include-descriptions \
  --summary --out-json /mnt/data/audit/series_list.json

python scripts/dicom_volume_video.py \
  --path /mnt/data/dicom_downloads/study \
  --out /mnt/data/videos \
  --series-number 3 --plane axial \
  --summary --out-json /mnt/data/audit/video_series3.json
```

Automatic policy when no series selector is given: CT series with more than 100 images at 10 frames/sec; MR series with 15-100 images at 3 frames/sec and with more than 100 images at 10 frames/sec. For any other modality, list the series and ask the user which series and frame rate to export (`--frame-rate` is then required). Without `--plane`, only axial output is produced.

When delivering an exported video to the user over a chat client, send it as a native attachment using a bare `MEDIA:` line with the absolute file path (for example `MEDIA:/tmp/videos/study_axial.mp4`). Do not prepend emoji, labels, quotes, or other text on that line, or the client may render it as plain text instead of a media attachment.

MP4 exports can contain burned-in annotations and must be treated as sensitive. See `references/mp4-export-workflow.md` for every selector, plane, windowing, and frame-rate option, and `references/dicom-source-fallback.md` for querying multiple source nodes in order.

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

Pseudonyms (UIDs, patient IDs, accession numbers, date shift) are keyed by a secret project salt. Supply it with `--salt-env ENV_NAME` (preferred) or `--salt VALUE`. When neither is given, the script uses a per-site/project secret file under `~/.config/dicom-skill/salts/` (or `--salt-file PATH`), creating it with a random value on first use, so results stay consistent on that machine and cannot be recomputed by someone who only knows the site ID and project name. Keep the same salt for a project when UIDs and dates must match across runs or machines, and never publish it. The JSON result reports `salt_source` and always states `deidentification_complete: false`: this is metadata pseudonymization, not a certificate of de-identification. Use `--map-json /secure/path/anon_map.json` only when the user needs mappings to persist across runs; the mapping file is written with owner-only permissions because it can contain PHI. Use `--include-files` only when needed because source paths can contain PHI.

Before sending anonymized output to another DICOM node, run a dry-run C-STORE against the anonymized output folder and keep the anonymization JSON audit artifact.

## Patient exam requests

Requests such as "get the CT of Jane Doe" or "which CTs were done today" combine the commands above. The rules below prevent the mistakes that matter most: delivering the wrong patient's study, reporting a partial retrieval as complete, and answering a different question than the one asked. `references/clinical-request-workflow.md` has the full procedure and edge cases.

1. C-ECHO, then study-level C-FIND with a patient-name wildcard and the requested date (today when none is given). Return `PatientName`, `PatientID`, `StudyDate`, `StudyTime`, `StudyDescription`, `ModalitiesInStudy`, `NumberOfStudyRelatedInstances`, and `StudyInstanceUID`, and save `--out-json`.
2. Verify the exact registered patient before retrieving or sending anything. A wildcard hit is only a candidate: read the full returned `PatientName` (`--summary-demographics` or the audit JSON) and reject near matches, longer names that merely start with the request, and reordered names unless the user asks for them. Requested spellings often differ from the registration, so search by a distinctive token and inspect the complete names.
3. If several materially different patients match, list them (registered name, modality, description, time, instance count) and ask which is intended. Do not choose, and do not retrieve on a token alone.
4. Retrieve each verified `StudyInstanceUID` with its own command. Only report success when every retrieval ends in `0x0000` with zero failed suboperations and a received count consistent with the query.
5. If the first source rejects or refuses the association, retry once, then continue with the next node in `source_fallback`. Report "not found" only after every configured source answered a successful association, and name the sources checked.
6. Report the DICOM `StudyDescription` verbatim. Do not infer anatomy or scope from the instance count.

For listings ("which exams today?"), query all patients for the date and modality; do not carry over a patient filter from earlier in the conversation. Count studies and unique patients (by `PatientID`) from the saved JSON. A listing does not authorize retrieving the listed studies.

A DICOM query cannot tell whether a report was written or signed; that lives in the RIS/EHR. Deleting a study is never done through this skill, including on a local viewer; see the reference for how to handle such a request.

## Package and deliver a retrieved study

When the user wants the study as a file (cloud drive, chat attachment, handover), retrieve it, ZIP it, upload it with whatever upload tool the environment provides, and verify the upload. Details are in `references/study-delivery-workflow.md`.

```bash
cd "$AUDIT" && zip -qr study.zip downloads
unzip -Z1 study.zip | head
```

Run `zip` from the directory that contains the download folder and archive a relative folder name. Never pass an absolute path: it embeds the machine's filesystem layout in the archive. Before uploading, confirm the entries start with a relative top-level folder and that the `.dcm` count matches the retrieve summary. After uploading, read back the file's ID, size, and link before reporting success. The archive contains identifiable DICOM; anonymize first when the recipient must not receive PHI.

## Scheduled backup to cloud storage

Use `scripts/dicom_cloud_backup.py` for an unattended, resumable copy of whole studies to an rclone remote. It retrieves one study at a time, checks the received instance set against an IMAGE-level C-FIND, losslessly transcodes studies that arrive mostly uncompressed, writes one stored ZIP64 per study with a PHI-light manifest, uploads it atomically, and verifies size and SHA-1. State lives in SQLite, so runs can be repeated safely. Remote objects are named `<remote-root>/YYYY/MM/DD/<StudyInstanceUID>.zip`.

```bash
# Yesterday's studies from one node (use --date YYYYMMDD for a specific day)
python scripts/dicom_cloud_backup.py \
  --dicom-config examples/dicom_nodes.local.yaml \
  --remote-root myremote:dicom-backup \
  run --lane daily --node primary

# Historical range, oldest first, resumable; start with a small --max-studies
python scripts/dicom_cloud_backup.py \
  --dicom-config examples/dicom_nodes.local.yaml \
  --remote-root myremote:dicom-backup \
  run --lane backfill --node secondary --start 20200101 --end 20241231 --max-studies 2

python scripts/dicom_cloud_backup.py status
python scripts/dicom_cloud_backup.py --remote-root myremote:dicom-backup verify
```

Global options go before the subcommand. The script refuses open-ended date ranges, never overwrites a remote object with different content, stops when free disk space drops below `--min-free-gib` (30 by default; 40 for the backfill lane), and allows one worker per lane. It uploads original, identifiable DICOM without adding encryption, so use it only with a destination the institution has approved for clinical data. Read `references/cloud-backup-runbook.md` before starting or monitoring a backup; `examples/systemd/` has example units and a timer.

## Cleanup after successful operations

After the transfer/retrieve/send/transcoding/preview/dicomizer/anonymization operation is complete and verification has passed, clear temporary ZIP, DICOM payload, PDF copies, and PNG preview files created in the workspace. Keep audit artifacts (`*.json`, `*.log`, summaries, study UID lists) unless the user asks to remove them too.

For uploads specifically: after the upload is verified and the remote file's link/ID is captured, remove the temporary download folder, extracted payload, and local ZIP created for that operation before responding with the link. Preserve audit JSON/logs and any user-requested deliverables.

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

`scripts/orthanc_temp.py` runs a disposable Orthanc container as a C-MOVE destination (`AGENT`, DICOM port `4242`, REST on `127.0.0.1:8042`). `retrieve --method move --use-temp-orthanc` starts, exports, and removes it automatically; use the manual lifecycle only when you need to keep it up across commands:

```bash
python scripts/orthanc_temp.py start --aet AGENT --dicom-port 4242 --http-port 8042
python scripts/orthanc_temp.py export --out /mnt/data/orthanc_export
python scripts/orthanc_temp.py stop --purge
```

The receiver is receive-only (C-ECHO and C-STORE; C-FIND/C-GET/C-MOVE are refused), its REST API is password-protected and localhost-only, and the password is kept in an owner-only state file and never printed. Because the DICOM port must be reachable by the PACS it is published on all interfaces by default: narrow it with `--dicom-bind`, restrict senders with `--allow-aet`, and always `stop --purge` when done. Details and custom setups: `references/temporary-orthanc.md`.

## Agent workflow

1. Identify the remote node: host/IP, port, called AE title, and local/calling AE title. Default local AE is `AGENT`.
2. Run C-ECHO before query/retrieve/send.
3. For query, keep return tags focused. Include `StudyInstanceUID`, `SeriesInstanceUID`, or `SOPInstanceUID` if the next step may retrieve data. For a patient-specific request, verify the exact registered `PatientName` (via `--summary-demographics` or the `--out-json` file) before retrieving, and retrieve one `StudyInstanceUID` per command.
4. For source → known destination copies, prefer C-MOVE directly to the destination AE and then verify on the destination. Use C-GET primarily for local downloads or when no destination AE is registered.
5. If C-GET returns failed suboperations or retrieves fewer instances than expected, switch to C-MOVE instead of C-STORE-ing a partial local folder.
6. For anonymization, process only explicit local input paths, write to a separate output directory, save `--out-json`, and do not claim pixel PHI has been removed.
7. For local JPEG 2000 compression/decompression, keep source and output folders separate, run a dry-run on large folders, and persist the JSON result with `--out-json`.
8. For PDF dicomization, keep PDF sources and generated DICOM output separate, prefer `--metadata-from` when attaching to an existing study, and do not claim PDF contents were anonymized.
9. For PNG preview rendering, keep previews in an explicit output folder, use `--max-size` when a compact visual check is enough, and remember burned-in pixel annotations can remain visible.
10. For MP4 export, first retrieve the study locally, then run `--list-series` when the user has not provided a specific series and the folder may contain multiple studies or non-CT/non-MR modalities. If the source only supports study/patient query models, do not force a series-level C-FIND — inspect the retrieved study locally instead. Default CT to series with more than 100 images at 10 frames/sec. Default MR to 15-100 images at 3 frames/sec and more than 100 images at 10 frames/sec. For other modalities, ask which series and frame rate to export before running the export command.
11. For send, verify the destination with C-ECHO, run a dry-run on the file set, then send. If the user requests anonymized transfer, send only from the anonymized output directory.
12. Save full command output JSON with `--out-json` for auditability and use `--summary` for terminal/chat output on large studies.
13. When packaging a study as a ZIP, archive a relative folder from its parent directory, check the entry list and `.dcm` count before upload, and verify the uploaded file's metadata before reporting success.
14. For recurring backups, use `scripts/dicom_cloud_backup.py` with bounded dates and follow `references/cloud-backup-runbook.md`; do not improvise a loop of manual retrieve/upload commands.
15. After successful verification, clear temporary ZIP/DICOM/PDF/PNG payload files created in the workspace while preserving audit JSON/logs and requested MP4 exports by default.

## Troubleshooting

Start with C-ECHO, then read `references/troubleshooting.md`, which lists symptoms and fixes for association failures, empty C-FIND results, partial C-GET, C-MOVE `0xA801`, the temporary Orthanc, codecs, anonymization, and the cloud backup safeguards.
