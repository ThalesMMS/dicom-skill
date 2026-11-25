# Clinical exam request workflow

Use this for patient-specific exam requests ("get the CT for Jane Doe") and for
general study or patient listings on a PACS. It distils failure modes observed
while validating the skill in routine use. Node names below (`primary`,
`secondary`) are placeholders for the entries in your private
`examples/dicom_nodes.local.yaml`; all patient names are fictional.

## Standard workflow

1. **C-ECHO** the first node in `source_fallback`.
2. **Query** at STUDY level for the requested date or date range with a
   patient-name wildcard. Return `PatientName`, `PatientID`, `StudyDate`,
   `StudyTime`, `StudyDescription`, `ModalitiesInStudy`,
   `NumberOfStudyRelatedInstances`, and `StudyInstanceUID`. Always save
   `--out-json`.
3. **Verify the exact patient** before anything is retrieved (next section).
4. **Retrieve** each verified study with C-GET, one command per
   `StudyInstanceUID`.
5. **Confirm** final status `0x0000`, zero failed suboperations, no local store
   errors, and a received count consistent with the query.
6. **Deliver** as the user asked (local folder, MP4, ZIP upload, C-STORE to
   another node). For ZIP packaging and uploads see
   `study-delivery-workflow.md`.
7. **Report** the exact registered patient name, study date and description
   (verbatim), modality set, instance count, retrieve status, and where the
   result was delivered.

### Which dates to query

- No date given for a named patient: start with today (`StudyDate=YYYYMMDD`)
  and widen only if there is no match.
- "All exams", "every day", or an explicit request without a date restriction:
  omit the `StudyDate` filter entirely and enumerate every returned study UID
  before retrieval. Do not silently limit the answer to today.
- A date range: use the inclusive DICOM form `StudyDate=YYYYMMDD-YYYYMMDD` on
  every source you check.
- A historical month: query the whole month, then select only the exact
  registered patient.

## Exact patient verification

A wildcard match is a *candidate*, not a confirmation.

- `--summary` output is PHI-light by design and omits `PatientName` and
  `PatientID` even when they are requested with `--return`. To verify identity
  either add `--summary-demographics`, or read `matches[]` /
  `responses[].identifier` in the saved `--out-json`. The full audit JSON is
  authoritative.
- Compare the complete returned `PatientName`. Similar, substring, reordered,
  or partial names must not be reported as exact matches. `DOE^JAN*` can return
  `DOE^JANET MARIE` when the request was for `DOE^JANE`.
- Names are often misspelled by the requester or registered differently in the
  PACS (transposed letters, an extra family name, an administrative suffix
  marking a provisional or unidentified registration). Search by a distinctive
  token such as the first name or one surname, then inspect the full registered
  names.
- If exactly one candidate is plainly the requested patient, report the
  registered name as found. If the candidates differ materially, list each one
  with modality, description, time, and instance count, and ask which is
  intended. Never pick among same-token candidates, and never retrieve on the
  token alone.
- Once the user confirms a candidate, record the verified registered name in
  the audit and in the report.
- When writing a direct `pynetdicom` C-FIND, set matching keys such as
  `StudyDate` once. Re-assigning them later as empty return keys
  (`ds.StudyDate = ""`) silently widens the query to all dates.
- Report the DICOM `StudyDescription` literally. Do not infer an anatomic scope
  ("whole body") from a large instance count or a vague description unless
  local series inspection confirms it.

## General study and patient listings

When the user asks which exams of a modality were done on a date without naming
a patient, query **all patients**:

```bash
python scripts/dicom_dimse.py query --config examples/dicom_nodes.local.yaml --node primary \
  --model study --level STUDY \
  --filter StudyDate=20250101 --filter ModalitiesInStudy=CT \
  --return PatientName --return PatientID --return StudyTime \
  --return StudyDescription --return StudyInstanceUID \
  --return NumberOfStudyRelatedInstances \
  --summary --out-json "$AUDIT/census.json"
```

- Do not inherit a patient filter from the preceding conversation; that turns a
  census into a single-patient answer without saying so.
- Count programmatically from the saved JSON. Report both the total study count
  and the unique-patient count (deduplicate by `PatientID`), keeping every
  `StudyInstanceUID` for study-level detail.
- `StudyDate`/`StudyTime` are acquisition metadata, not proof that a report was
  written or signed.
- A listing does not authorize downloading or uploading the listed studies.

## Multiple patients and multiple studies

- Several patient names in one request: query and verify each patient
  independently, then retrieve every verified study. Do not merge studies just
  because they share a date or modality.
- Several study UIDs (one patient or many): run **one retrieve command per
  UID** with its own `--out-json`. `dicom_dimse.py retrieve` rejects a repeated
  `--study-uid`. Package results together only after every retrieval reports
  `0x0000` with zero failed suboperations.
- Do not claim a multi-study retrieval succeeded unless each UID has its own
  successful result.
- A study with image instances plus a Structured Report or other non-image
  objects is delivered complete; report the modality set and total instance
  count.

## "Is there a new exam?"

Compare the current study list with the `StudyInstanceUID`s already retrieved
or reported in this session (use `StudyTime` when available). Retrieve only the
newly appearing UIDs.

If the user adds another patient while an operation is in progress, verify that
patient and include the study in the same delivery; do not ship an earlier
partial package first.

## Source fallback and transient connectivity

- If the first C-ECHO is rejected or cannot connect, retry once. If it still
  fails, move to the next node in `source_fallback` and repeat the same
  date/name query there.
- "No match" is a valid conclusion only after every configured source was
  checked with a successful association. Report a verified no-match as exactly
  that, naming the sources checked; it is not a retrieval failure.
- Never retrieve without a successful association and exact-name verification
  on the node being retrieved from.

See `dicom-source-fallback.md` for the config keys and command pattern.

## Requests this skill must not fulfil with DIMSE

**Deleting a study.** This skill never deletes, overwrites, or modifies data on
a DICOM node. If the user wants a study removed from a local viewer or
workstation database, treat it as a separate destructive operation performed
through that application's own interface:

1. Identify the exact target by full `PatientName`, `StudyInstanceUID`, study
   date/time, and description. Distinguish it from similarly named corrected or
   replacement studies.
2. Confirm with the user that deletion is intended, where, and which studies
   must be kept.
3. Afterwards verify that the target is gone and the retained study is still
   present.
4. If the application cannot be operated, say so. Do not claim deletion and do
   not substitute a remote DICOM operation or direct edits to database files.

**Confirming that a report exists.** A DICOM study result does not establish
whether a radiology report was written, finalized, or signed, and a missing SR
object does not mean there is no report. Report status lives in the RIS/EHR;
query that system separately if a tool for it is available, and confirm patient
identity there too. If a report is stored only as a scanned image without text,
report its existence and status but do not invent or paraphrase findings.

## Pitfalls checklist

- A broad patient-name match is not sufficient; verify the full returned
  `PatientName`.
- `--summary` hides demographics; use `--summary-demographics` or the audit
  JSON.
- One retrieve per `StudyInstanceUID`, one audit JSON per retrieve.
- Verify on every source before concluding "not found".
- Keep audit JSON; clean only the temporary clinical payloads created for the
  operation.
- If nothing is found or connectivity fails, state the exact source checked and
  the result. Never describe an exam that was not returned by a query.
