# Study delivery: retrieve, ZIP, upload

Use this when the user asks to download a study and send it somewhere as a
file, for example to a cloud drive. This skill does not ship an uploader: use
whatever file-upload tool the agent environment provides (a cloud-drive
connector, `rclone`, a chat attachment) and only a destination the user is
authorized to use for clinical data.

If your site treats a shorthand such as "send me the exam" as a fixed flow
(for example retrieve, ZIP, upload to a specific drive), record that default in
a private `references/site-workflows.local.md` (see the `.example` template).
Without such a note, ask where the study should be delivered.

## Pattern

1. Find and verify the study as described in `clinical-request-workflow.md`
   (source fallback, exact `PatientName` verification).
2. Retrieve locally with C-GET, unless the user gives a destination AE for
   C-MOVE.
3. Verify the retrieve summary: final status `0x0000`, zero failed
   suboperations, zero store errors.
4. Package the study folder as a ZIP **from its parent directory** using a
   relative archive path:

   ```bash
   cd "$AUDIT" && zip -qr study.zip downloads
   unzip -Z1 study.zip | head
   ```

   Never pass an absolute download path to `zip`: that embeds the machine's
   filesystem layout in the archive. Check that entries start with a relative
   top-level folder, not `/` or a machine-specific prefix, and that the number
   of `.dcm` entries matches the retrieve summary's `received_count`.

   Without the `zip` CLI:

   ```bash
   python -c "import shutil,sys; shutil.make_archive(sys.argv[1], 'zip', root_dir=sys.argv[2], base_dir=sys.argv[3])" \
     "$AUDIT/study" "$AUDIT" downloads
   ```

5. Upload the ZIP. Upload tools take a file path, not a directory.
6. Read back the uploaded file's metadata (ID, name, type, size, link) and
   compare the size with the local ZIP before reporting success.
7. Only after that verification, remove the temporary download folder and the
   local ZIP created for this operation. Keep the query/retrieve/upload audit
   JSON and any deliverable the user asked to keep.
8. Report the exact registered patient name, study date and description,
   modality set, instance count, retrieve status, ZIP size, and the link or ID
   of the uploaded file.

## Notes

- One archive per patient/study unless the user asks for a combined package;
  for a combined package, add every verified study before uploading once.
- Patient names in ZIP file names are PHI. Prefer a neutral name (date plus a
  short study label or UID suffix) unless the user asks otherwise and the
  destination is appropriate for it.
- The ZIP contains original, identifiable DICOM. If the recipient must not
  receive PHI, run `scripts/dicom_anonymize.py` first and package the
  anonymized output instead.
- Never delete remote DICOM data or unrelated user folders as part of cleanup.
- For unattended, recurring per-study archival to cloud storage, use
  `scripts/dicom_cloud_backup.py` instead (see `cloud-backup-runbook.md`).
