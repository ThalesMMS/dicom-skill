# dicom-skill

`dicom-skill` is an agent-ready shell skill for DICOM DIMSE and local pixel-data workflows. It gives an agent a small, auditable command-line toolkit for verifying DICOM nodes, querying metadata, retrieving studies, anonymizing local payloads, sending DICOM files between PACS, VNA or other DIMSE-compatible systems, transcoding local DICOM files to or from JPEG 2000, and rendering PNG previews from local instances.

This repository contains the publishable skill package under
[`skill/dicom-skill/`](skill/dicom-skill/). The repository root holds GitHub
presentation files such as this README, the license, and screenshots. The
operational contract lives in
[`skill/dicom-skill/SKILL.md`](skill/dicom-skill/SKILL.md); the scripts in
[`skill/dicom-skill/scripts/`](skill/dicom-skill/scripts/) are meant to be run
directly from an agent shell.

<p align="center">
  <img src="screenshot1.png" alt="WhatsApp conversation showing DICOM study transfer and verification through dicom-skill" width="48%">
  <img src="screenshot2.png" alt="WhatsApp conversation showing scheduled PACS backup automation and test-run report through dicom-skill" width="48%">
</p>

## What it does

- Run C-ECHO connectivity checks before touching data.
- Run C-FIND queries at `PATIENT`, `STUDY`, `SERIES`, or `IMAGE` level.
- Retrieve data locally with C-GET.
- Request C-MOVE transfers to a known destination AE.
- Start a temporary Orthanc receiver for C-MOVE workflows that need a local destination AE.
- Send DICOM files or folders with C-STORE.
- Anonymize local DICOM files or folders with an RSNA-anonymizer-derived script workflow.
- Compress or decompress local DICOM pixel data with JPEG 2000.
- Render local DICOM instances to PNG preview images.
- Emit JSON output for audit trails, with an optional PHI-light summary mode for terminal or chat output.

## Safety model

DICOM metadata and pixel data can contain patient-identifying information. This skill is intentionally conservative:

- Do not connect to clinical systems unless the user has authorization and has provided the target endpoint details.
- Prefer C-ECHO before query, retrieve, or send operations.
- Prefer the least invasive DIMSE operation that satisfies the task.
- Never delete, overwrite, or modify remote DICOM data.
- Keep retrieved payloads in explicit, user-controlled folders.
- Preserve JSON/log audit artifacts by default.
- Do not print full patient data unless the user explicitly asks for it.
- Treat anonymization as an explicit, local file operation. Do not assume retrieved data is anonymous until `scripts/dicom_anonymize.py` has been run and the result has been checked.
- The anonymizer removes metadata PHI according to the bundled script, but it does not perform OCR or burned-in pixel PHI removal.
- Treat PNG previews as sensitive because burned-in pixel annotations can remain visible.

## Repository layout

```text
.
├── README.md                # GitHub/project overview
├── LICENSE.txt              # Repository license
├── screenshot1.png          # README media
├── screenshot2.png          # README media
└── skill/
    └── dicom-skill/
        ├── SKILL.md                 # Agent-facing operating instructions
        ├── manifest.txt             # Skill package manifest
        ├── requirements.txt         # Python runtime dependencies
        ├── examples/
        │   ├── anonymize-local.md
        │   ├── dicom_nodes.yaml
        │   └── orthanc-local.md
        ├── resources/
        │   └── rsna/                # Bundled RSNA anonymizer script and license
        └── scripts/
            ├── dicom_dimse.py
            ├── dicom_anonymize.py
            ├── dicom_jpeg2000.py
            ├── dicom_preview.py
            ├── orthanc_temp.py
            └── validate_install.py
```

## Requirements

- Python 3.10+
- `pydicom`
- `pynetdicom`
- `requests`
- `PyYAML`
- `numpy`
- `pylibjpeg`
- `pylibjpeg-openjpeg`
- `Pillow`
- Docker, only when using the temporary Orthanc helper

Install from the skill package folder:

```bash
cd skill/dicom-skill
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/validate_install.py
```

The DIMSE, anonymization, JPEG 2000, and PNG preview commands only require
Python dependencies. JPEG 2000 encoding uses `pylibjpeg-openjpeg`; preview
rendering uses `Pillow`. Docker is only needed for the helper that launches a
temporary Orthanc receiver.

All command examples below assume the current working directory is
`skill/dicom-skill/`.

## Configuration

Every command can use explicit connection flags:

```bash
python scripts/dicom_dimse.py echo \
  --host 127.0.0.1 \
  --port 4242 \
  --aet ORTHANC \
  --calling-aet AGENT
```

For repeated use, define nodes in YAML:

```yaml
calling_aet: AGENT
current_node: orthanc
nodes:
  orthanc:
    host: 127.0.0.1
    port: 4242
    ae_title: ORTHANC
```

Then reference the node:

```bash
python scripts/dicom_dimse.py echo --config examples/dicom_nodes.yaml --node orthanc
```

## Common workflows

### 1. Verify connectivity with C-ECHO

```bash
python scripts/dicom_dimse.py echo \
  --host pacs.local \
  --port 104 \
  --aet PACS \
  --calling-aet AGENT \
  --summary
```

Run this first. If C-ECHO fails, fix AE titles, host, port, firewall rules, TLS
expectations, or remote AE authorization before attempting query or transfer.

### 2. Query studies with C-FIND

```bash
python scripts/dicom_dimse.py query \
  --host pacs.local \
  --port 104 \
  --aet PACS \
  --calling-aet AGENT \
  --model study \
  --level STUDY \
  --filter StudyDate=20250101-20250131 \
  --filter ModalitiesInStudy=CT \
  --return PatientID \
  --return AccessionNumber \
  --return StudyDate \
  --return StudyDescription \
  --return NumberOfStudyRelatedInstances \
  --return StudyInstanceUID \
  --summary \
  --out-json audit/find_ct_january.json
```

`--filter` and `--return` accept DICOM keywords such as `PatientID`,
`AccessionNumber`, and `StudyInstanceUID`. Hex tags such as `00100020` are also
accepted.

### 3. Retrieve locally with C-GET

Use C-GET when the goal is a local download and the remote node supports C-GET
storage suboperations reliably.

```bash
python scripts/dicom_dimse.py retrieve \
  --method get \
  --host pacs.local \
  --port 104 \
  --aet PACS \
  --calling-aet AGENT \
  --study-uid 1.2.840.113619.2.55.3.604688435.123.1735689600.1 \
  --out downloads/study \
  --summary \
  --out-json audit/retrieve_get.json
```

If C-GET reports failed suboperations or retrieves fewer instances than expected,
do not assume the study was copied. Prefer C-MOVE to a registered destination AE
and verify the destination afterward.

### 4. Transfer to a known destination with C-MOVE

Use C-MOVE for source-to-destination copies when the source DICOM node already
knows the destination AE title and network address.

```bash
python scripts/dicom_dimse.py retrieve \
  --method move \
  --no-temp-orthanc \
  --host pacs.local \
  --port 104 \
  --aet PACS \
  --calling-aet AGENT \
  --destination-aet BACKUP \
  --study-uid 1.2.840.113619.2.55.3.604688435.123.1735689600.1 \
  --out audit/move_out \
  --summary \
  --out-json audit/move_to_backup.json
```

After the move, verify on the destination with C-FIND and compare
`NumberOfStudyRelatedInstances` when the server provides it.

```bash
python scripts/dicom_dimse.py query \
  --host backup.local \
  --port 104 \
  --aet BACKUP \
  --calling-aet AGENT \
  --model study \
  --level STUDY \
  --filter StudyInstanceUID=1.2.840.113619.2.55.3.604688435.123.1735689600.1 \
  --return StudyDate \
  --return StudyDescription \
  --return NumberOfStudyRelatedInstances \
  --return StudyInstanceUID \
  --summary \
  --out-json audit/verify_backup.json
```

### 5. Retrieve via temporary Orthanc

When a C-MOVE destination is needed and the remote PACS is configured to send to
`AGENT:4242`, the skill can start a temporary Orthanc receiver, move into it,
export received instances to disk, and stop the container.

```bash
python scripts/dicom_dimse.py retrieve \
  --method move \
  --use-temp-orthanc \
  --host pacs.local \
  --port 104 \
  --aet PACS \
  --calling-aet AGENT \
  --destination-aet AGENT \
  --study-uid 1.2.840.113619.2.55.3.604688435.123.1735689600.1 \
  --out downloads/from_move \
  --summary \
  --out-json audit/move_to_temp_orthanc.json
```

Important C-MOVE requirement: the remote node must already know where destination
AE `AGENT` lives. By default, that means the agent machine must be reachable by
the remote PACS on DICOM port `4242`. If the remote responds with `0xA801`, the
move destination is unknown to that node.

### 6. Compress or decompress local files with JPEG 2000

JPEG 2000 transforms are local file operations; they do not connect to a PACS.
The default compression syntax is JPEG 2000 Lossless
(`1.2.840.10008.1.2.4.90`).

```bash
python scripts/dicom_jpeg2000.py compress \
  --path downloads/study \
  --out downloads/study_j2k \
  --summary \
  --out-json audit/jpeg2000_compress.json
```

Decompress compressed files to Explicit VR Little Endian:

```bash
python scripts/dicom_jpeg2000.py decompress \
  --path downloads/study_j2k \
  --out downloads/study_uncompressed \
  --summary \
  --out-json audit/jpeg2000_decompress.json
```

For large folders, use `--dry-run --include-files` first. Transcoding generates
a new SOP Instance UID by default; pass `--keep-instance-uid` only when the
workflow intentionally preserves the original UID. Lossy JPEG 2000 is opt-in
with `--syntax lossy` plus `--j2k-cr` or `--j2k-psnr`.

### 7. Render PNG previews from local instances

Preview rendering is a local file operation; it does not connect to a PACS. For
grayscale images, the script applies Modality LUT and VOI LUT/windowing when
available and handles `MONOCHROME1` inversion.

```bash
python scripts/dicom_preview.py \
  --path downloads/study/instance.dcm \
  --out previews/study \
  --summary \
  --out-json audit/preview_instance.json
```

For multiframe instances, the default is the first frame. Use `--frame 5` for a
specific 1-based frame or `--all-frames` to render every frame. Use
`--max-size 1024` for compact review PNGs, or override grayscale display with
`--window-center` and `--window-width`.

### 8. Anonymize local DICOM files

Use `scripts/dicom_anonymize.py` only on local files or folders. It does not
contact remote DICOM nodes. The default workflow uses the bundled
`resources/rsna/default-anonymizer.script` file to decide which DICOM attributes
are retained, removed, blanked, UID-remapped, date-shifted, or pseudonymized.

```bash
python scripts/dicom_anonymize.py \
  --path downloads/study \
  --out anonymized/study \
  --site-id 123456 \
  --project-name research_export \
  --summary \
  --out-json audit/anonymize_result.json
```

Use `--salt-env ENV_NAME` or `--salt VALUE` for deterministic pseudonymization.
Use `--map-json secure/anon_map.json` only when mappings must persist across
runs; the mapping file can contain PHI because original identifiers may be used
as keys. The anonymizer marks datasets with `PatientIdentityRemoved=YES`, but it
does not remove burned-in pixel annotations or run OCR.

### 9. Send files with C-STORE

Discover readable DICOM files before sending:

```bash
python scripts/dicom_dimse.py send \
  --host destination.local \
  --port 104 \
  --aet DEST_AE \
  --calling-aet AGENT \
  --path /path/to/dicom \
  --dry-run \
  --include-files \
  --summary
```

Then send:

```bash
python scripts/dicom_dimse.py send \
  --host destination.local \
  --port 104 \
  --aet DEST_AE \
  --calling-aet AGENT \
  --path /path/to/dicom \
  --summary \
  --out-json audit/send_result.json
```

## Temporary Orthanc helper

The helper starts an ephemeral Orthanc container with:

- AE title: `AGENT`
- Host DICOM port: `4242`
- REST API: `http://127.0.0.1:8042`
- Remote access enabled inside the container
- Called AE and modality host checks disabled for the temporary receiver

Manual lifecycle:

```bash
python scripts/orthanc_temp.py start --aet AGENT --dicom-port 4242 --http-port 8042
python scripts/dicom_dimse.py echo --host 127.0.0.1 --port 4242 --aet AGENT --calling-aet AGENT
python scripts/orthanc_temp.py status
python scripts/orthanc_temp.py export --out downloads/orthanc_export
python scripts/orthanc_temp.py stop --purge
```

The REST API binds to localhost by default. The DICOM port is exposed on the host
because remote DICOM nodes must be able to open an association back to the move
destination.

## Output and audit files

All DIMSE, anonymization, JPEG 2000, and PNG preview commands print JSON. Use:

- `--summary` for concise, PHI-light terminal output.
- `--out-json path/to/result.json` to persist the full result for audit/debugging.
- Explicit output directories such as `downloads/`, `anonymized/`, `previews/`,
  or `audit/` for payloads and command results.
- Avoid `--include-files` in chat output unless needed; source paths can contain PHI.

After successful verification, remove temporary ZIP, DICOM payload, or PNG
preview folders that were created only for the operation. Keep JSON summaries,
command logs, and UID lists unless the user asks to remove them.

Do not delete anonymized exports or mapping JSON files unless the user
explicitly asks. Mapping JSON files can contain PHI.

## Troubleshooting

**Association rejected or aborted**

Check the called AE title, calling AE title, host, port, firewall, TLS settings,
and whether the remote node allows the calling AE.

**C-FIND returns no matches**

Confirm the query model (`study` vs. `patient`), query level, date format,
wildcard policy, and whether the server supports the requested return tags.

**C-GET retrieves fewer instances than expected**

Some PACS implementations reject storage presentation contexts or handle C-GET
poorly. Retry with C-MOVE to a known destination AE, then verify on that
destination.

**C-MOVE returns `0xA801`**

The source node does not know the requested destination AE. Register the
destination AE on the source system, ensure the host/port are reachable, or use
C-GET when appropriate.

**Temporary Orthanc cannot start**

Check that Docker is installed and running, the `orthancteam/orthanc` image can
be pulled, and ports `4242` and `8042` are free.

**JPEG 2000 compression fails**

Run `python scripts/validate_install.py` and check the `codecs.jpeg2000` block.
Encoding requires `numpy`, `pylibjpeg`, and `pylibjpeg-openjpeg`.

**PNG preview fails on compressed input**

Run `python scripts/validate_install.py` and check the pixel decoder details.
JPEG 2000 and other compressed transfer syntaxes require an available pydicom
pixel decoder.

**Anonymized files still contain visible burned-in annotations**

This skill's anonymizer does not run OCR or pixel masking. Do not share previews
or images externally until pixel PHI has been reviewed or removed separately.

**Anonymization mapping differs across runs**

Use `--map-json` to load/update a secure PHI-containing mapping file, or use
`--patient-id-strategy hashed` with a stable project salt.

## Development notes

Validate the local install:

```bash
python scripts/validate_install.py
```

Inspect command help:

```bash
python scripts/dicom_dimse.py --help
python scripts/dicom_dimse.py query --help
python scripts/dicom_anonymize.py --help
python scripts/dicom_jpeg2000.py --help
python scripts/dicom_preview.py --help
python scripts/orthanc_temp.py --help
```

For local smoke flows, see
[`skill/dicom-skill/examples/orthanc-local.md`](skill/dicom-skill/examples/orthanc-local.md)
and
[`skill/dicom-skill/examples/anonymize-local.md`](skill/dicom-skill/examples/anonymize-local.md).

## RSNA anonymizer note

The bundled anonymization script is derived from the RSNA DICOM Anonymizer
project at <https://github.com/RSNA/anonymizer>. Copied RSNA material lives in
`skill/dicom-skill/resources/rsna/` and includes the original Apache 2.0
license.

## License

See [`LICENSE.txt`](LICENSE.txt).
