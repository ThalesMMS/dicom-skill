---
name: dicom-skill
description: Perform DICOM DIMSE C-ECHO, C-FIND, C-GET/C-MOVE retrieval, C-STORE send, and local DICOM anonymization operations from an agent shell. Includes helper scripts to create a temporary Orthanc receiver with AE title AGENT on DICOM port 4242 when a C-MOVE destination is needed.
---

# dicom-skill

Use this skill when the user asks an agent to interact with DICOM nodes/PACS/VNA/Orthanc using DIMSE operations: verify connectivity, query metadata, retrieve instances, send DICOM files, or anonymize local DICOM files before further handling.

This is a shell/CLI skill, not an MCP server. Do not start an MCP server. Use the scripts in `scripts/` directly.

## Safety and data handling

DICOM metadata and files can contain patient-identifying information. Do not connect to clinical systems, retrieve studies, send images, or anonymize clinical payloads unless the user has authorization and has provided the endpoint or file details. Keep outputs in local, user-controlled folders. Avoid printing full patient data unless the user explicitly asks for it. Anonymization must be an explicit local file operation; never assume retrieved or received files are de-identified until `scripts/dicom_anonymize.py` has been run and checked.

For troubleshooting, prefer C-ECHO first. For data movement, prefer the least invasive operation that satisfies the request. When the user wants to copy studies from one DICOM node to another known DICOM destination, prefer C-MOVE directly to that destination AE over C-GET + C-STORE. Never delete, overwrite, or modify remote DICOM data from this skill. The anonymizer removes metadata PHI according to the bundled script, but it does not perform OCR or burned-in pixel PHI removal.

## Install dependencies

From the skill folder:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/validate_install.py
```

The DIMSE and anonymization scripts require Python packages only. The temporary Orthanc helper requires Docker and the `orthancteam/orthanc` image, pulled automatically by Docker if absent.

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

Use C-GET for local downloads when there is no known/registered destination AE. It does not require the remote PACS to know a separate destination AE, but some PACS reject many C-STORE suboperations during C-GET. If the final status has failed suboperations or the received count is lower than expected, do **not** assume the study was copied; try C-MOVE to a known destination AE and verify there.

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

For large folders, use `--dry-run` first to verify file discovery.

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

After the transfer/retrieve/send is complete and verification has passed, clear temporary ZIP and DICOM payload files created in the workspace. Keep audit artifacts (`*.json`, `*.log`, summaries, study UID lists) unless the user asks to remove them too.

Typical temporary payloads to remove:

- Extracted ZIP folders such as `$AUDIT/extracted`
- Local C-GET/download folders such as `$AUDIT/downloads`
- Temporary C-MOVE/export payload folders such as `$AUDIT/move_out`
- Workspace-local copies of inbound ZIPs, if any were created only for the operation

Do not delete anonymized exports or mapping JSON files unless the user explicitly asks. Mapping JSON files can contain PHI.

Prefer a recoverable delete when available:

```bash
for p in "$AUDIT/extracted" "$AUDIT/downloads" "$AUDIT/move_out"; do
  [ -e "$p" ] || continue
  if command -v trash >/dev/null 2>&1; then trash "$p"; else rm -rf "$p"; fi
done
```

Only remove paths inside the explicit workspace/audit directory for the current operation. Never delete remote DICOM data, user source archives outside the workspace, or audit logs by default.

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
7. For send, verify the destination with C-ECHO, run a dry-run on the file set, then send. If the user requests anonymized transfer, send only from the anonymized output directory.
8. Save full command output JSON with `--out-json` for auditability and use `--summary` for terminal/chat output on large studies.
9. After successful verification, clear temporary ZIP/DICOM payload files created in the workspace while preserving audit JSON/logs by default.

## Troubleshooting

Association rejected: verify called AE title, host, port, TLS/firewall, and whether the remote node allows the calling AE `AGENT`.

C-FIND returns no matches: confirm query model (`study` vs `patient`), level, date format, wildcard policy, and whether the server supports the requested return tags.

C-GET fails during C-STORE suboperations or retrieves only a few images from a large study: the remote may not support C-GET well or may reject storage presentation contexts. Try C-MOVE directly to a known destination AE, then verify the destination with C-FIND.

C-MOVE status `0xA801`: unknown move destination. Register `AGENT` with host/port on the remote PACS, or use C-GET.

Temporary Orthanc cannot start: Docker may be unavailable, the image may be unavailable, or port `4242`/`8042` may already be in use.

Anonymized files still contain visible burned-in annotations: this skill's anonymizer does not run OCR or pixel masking. Do not share externally until pixel PHI has been reviewed or removed with a separate workflow.

Anonymization mapping differs across separate runs: use `--map-json` to load/update a secure PHI-containing mapping file, or use `--patient-id-strategy hashed` with a stable project salt.
