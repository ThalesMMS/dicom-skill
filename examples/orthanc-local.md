# Local Orthanc smoke flow

Install dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python scripts/validate_install.py
```

Start a temporary Orthanc receiver with AE title `AGENT` on host DICOM port `4242`:

```bash
python scripts/orthanc_temp.py start --aet AGENT --dicom-port 4242 --http-port 8042
```

Verify it with C-ECHO:

```bash
python scripts/dicom_dimse.py echo --host 127.0.0.1 --port 4242 --aet AGENT --calling-aet AGENT
```

Send a local DICOM folder into it:

```bash
python scripts/dicom_dimse.py send --host 127.0.0.1 --port 4242 --aet AGENT --calling-aet AGENT --path /path/to/dicom --dry-run --include-files
python scripts/dicom_dimse.py send --host 127.0.0.1 --port 4242 --aet AGENT --calling-aet AGENT --path /path/to/dicom
```

Query studies stored in it:

```bash
python scripts/dicom_dimse.py query --host 127.0.0.1 --port 4242 --aet AGENT --calling-aet AGENT --model study --level STUDY --filter StudyDate= --return PatientID --return StudyDate --return StudyDescription --return StudyInstanceUID
```

Export everything currently stored in the temporary Orthanc:

```bash
python scripts/orthanc_temp.py export --out /mnt/data/orthanc_export
```

Stop and purge the temporary receiver:

```bash
python scripts/orthanc_temp.py stop --purge
```
