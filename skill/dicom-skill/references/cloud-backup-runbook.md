# Cloud backup runbook (`scripts/dicom_cloud_backup.py`)

This runbook is for an agent or operator who starts and watches an unattended
backup of DICOM studies to cloud storage. The process is deterministic: the
script queries, retrieves, archives, and uploads without any language-model
involvement. The agent's job is to operate the services, read the events, watch
disk space, and stop and escalate when a safeguard trips.

Do not put passwords, patient names, or other clinical identifiers in commands,
logs, or reports. Normal logs contain only dates, node names, technical UIDs,
counts, sizes, hashes, and states.

Every value below (`primary`, `secondary`, `myremote:dicom-backup`, paths,
dates) is a placeholder. Real values belong in your private
`dicom_nodes.local.yaml`, in the service environment file, and in a private
`site-workflows.local.md`.

## Before you use it

- Sending identifiable studies to cloud storage needs institutional approval
  for that provider, account, and region. The script uploads original,
  non-anonymized DICOM and adds no encryption of its own. If you need
  client-side encryption, point `--remote-root` at an `rclone crypt` remote;
  that is a deployment decision, not something to switch on mid-backup.
- Requirements: the skill's Python environment, `rclone` on `PATH` (or
  `--rclone /path/to/rclone`) with a configured remote that reports SHA-1
  hashes, and a source node that supports C-GET and IMAGE-level C-FIND.
- The source node is only ever read (C-ECHO, C-FIND, C-GET).
- The machine clock must be correct and in the intended time zone: the daily
  lane computes "yesterday" from local time.

## What the script does

Destination layout, one object per study:

```text
<remote-root>/YYYY/MM/DD/<StudyInstanceUID>.zip
```

The name carries no node prefix and no patient information. Each ZIP contains
`DICOM/<SeriesInstanceUID>/<SOPInstanceUID>.dcm` and a `manifest.json`.
Individual `.dcm` files are never uploaded.

For each study:

1. C-FIND with a mandatory bounded `StudyDate`. Open-ended ranges are rejected.
2. IMAGE-level C-FIND for the expected SOP Instance UIDs and SOP Class UIDs.
3. C-GET of that single study, offering JPEG 2000 first and keeping JPEG-LS and
   other syntaxes as fallback.
4. Check the final status, C-STORE errors, and that the received set of SOP
   Instance UIDs equals the expected set exactly.
5. Count only objects with Pixel Data to decide the compression policy.
6. If uncompressed images are a strict majority, transcode every uncompressed
   image of the study to JPEG 2000 lossless (`1.2.840.10008.1.2.4.90`),
   preserving `StudyInstanceUID` and `SOPInstanceUID`. Already-compressed
   images are not recompressed. SR, PDF, and other non-pixel objects are kept
   as they are.
7. If any conversion fails, the whole study fails and nothing is uploaded.
8. Create a ZIP64 with `ZIP_STORED` (no second compression pass over pixel data
   that is already compressed).
9. Validate structure, member count, and CRC. The manifest records received
   transfer syntaxes and the compression decision.
10. Remove the temporary DICOM files only after the ZIP validates.
11. Upload as `<UID>.zip.uploading`, compare size and SHA-1, rename to
    `<UID>.zip`, and verify again.
12. Record completion in SQLite and delete the local ZIP.

If the final object already exists with the same size and SHA-1, the study is
treated as complete. If it exists with different content the run stops with
`Remote conflict` and nothing is overwritten or deleted.

## Lanes

| Lane | Purpose | Command |
|---|---|---|
| `daily` | one exact date, default yesterday | `run --lane daily --node primary [--date YYYYMMDD]` |
| `backfill` | a closed historical range, oldest first | `run --lane backfill --node secondary --start YYYYMMDD --end YYYYMMDD` |

- Each lane holds a lock, so only one worker per lane runs at a time. The two
  lanes can run concurrently.
- The backfill walks 31-day windows and stores its next start date per node
  (`metadata.backfill_cursor_date:<node>`). Restarting resumes from there.
  `--rescan` ignores the cursor; completed studies are still skipped.
- If the same studies exist on two nodes, pick date ranges that do not overlap
  (for example backfill the archive up to a cutover date and run the daily lane
  from the next day) so each study is copied once.
- `--max-studies N` stops after N new studies. Use it for a first controlled
  test.

Global options go **before** the subcommand:

```bash
python scripts/dicom_cloud_backup.py \
  --dicom-config examples/dicom_nodes.local.yaml \
  --remote-root myremote:dicom-backup \
  run --lane daily --node primary --date 20250101 --max-studies 2
```

`--remote-root` can also come from `DICOM_BACKUP_REMOTE_ROOT`. State defaults
to `~/.local/state/dicom-cloud-backup` and scratch space to
`~/dicom-backup-work`; override with `--state-dir` and `--work-dir`.

## Disk-space safeguard

The default hard floor is **30 GiB free** (`--min-free-gib`); the backfill lane
uses a more conservative **40 GiB** (`--backfill-min-free-gib`). Size both to
your disk and largest studies.

- Before each study the lane's floor must be met.
- During C-GET a watchdog checks the disk every 5 seconds. Crossing the floor
  ends the transfer, cleans the partial retrieval, and leaves the study
  resumable.
- Before transcoding and before creating the ZIP the script reserves the lane
  floor plus the size being written plus 64 MiB.

While workers are active, also check from outside roughly every 10 minutes:

```bash
df -BG --output=source,size,used,avail,pcent,target /var/lib/dicom-backup
du -sh /var/lib/dicom-backup/work /var/lib/dicom-backup/state
```

If free space falls under the hard floor, stop the timer and workers and tell
the person responsible:

```bash
sudo systemctl stop dicom-backup-daily.timer
sudo systemctl stop dicom-backup-backfill.service
sudo systemctl stop dicom-backup-daily.service
sudo systemctl stop 'dicom-backup-daily-date@*.service'
```

Do not manually delete the SQLite file, ZIPs, `.retrieve` directories, or
remote objects to make room. Once space has been recovered by a human decision,
repeat the same run: resumption is idempotent.

## Starting up

Example units live in `examples/systemd/`. Install them with your own paths and
service user, and create the environment file from `backup.env.example`.

### 1. Pre-checks

```bash
date -Is
timedatectl show -p NTPSynchronized -p Timezone
df -BG --output=avail /var/lib/dicom-backup
rclone about myremote: --json
python scripts/dicom_dimse.py echo --config examples/dicom_nodes.local.yaml --node primary --summary
python scripts/dicom_dimse.py echo --config examples/dicom_nodes.local.yaml --node secondary --summary
```

Continue only if the clock is synchronized, every C-ECHO returns `0x0000`, the
remote answers, and free space is above the backfill floor.

### 2. Controlled test

Run each lane once with `--max-studies 2`, then check `status` and `verify`,
and confirm on the remote that only `<UID>.zip` objects exist under the
expected date folders. Have the person responsible approve the result before
going further.

### 3. Close any daily gap

Process every date between the day after the backfill range and yesterday, in
order, one at a time:

```bash
sudo systemctl start --no-block dicom-backup-daily-date@YYYYMMDD.service
systemctl --no-pager --full status dicom-backup-daily-date@YYYYMMDD.service
```

Wait for `Result=success` before starting the next date. Do not jump to the
timer and leave intermediate days uncovered. A date that was partly copied is
safe to repeat: completed studies are verified and skipped.

### 4. Enable the daily timer

Only after all dates up to yesterday are complete:

```bash
sudo systemctl enable --now dicom-backup-daily.timer
systemctl list-timers dicom-backup-daily.timer --all --no-pager
```

### 5. Start the backfill

```bash
sudo systemctl enable --now dicom-backup-backfill.service
systemctl --no-pager --full status dicom-backup-backfill.service
```

## Monitoring

Every 10 minutes while a worker is active:

```bash
systemctl --no-pager --full status dicom-backup-backfill.service dicom-backup-daily.service dicom-backup-daily.timer
systemctl list-units 'dicom-backup-daily-date@*.service' --all --no-pager
python scripts/dicom_cloud_backup.py --state-dir /var/lib/dicom-backup/state status
journalctl -u dicom-backup-backfill.service -n 100 --no-pager
```

In `status`, look at:

- `counts.failed`: absent or zero.
- `recent[].error`: `null` for completed studies.
- `coverage`: counts per node, date, and state.
- `metadata.backfill_cursor_date:<node>`: next date the backfill will query.

Normal events: `preflight_complete`, `study_started`, `study_retrieved`,
`study_compression_prepared`, `study_archived`, `study_completed`,
`study_skipped`, `upload_already_complete`, `backfill_cursor_advanced`,
`backfill_gap_closed`.

In `study_compression_prepared`, confirm:

- Images received compressed stay compressed.
- If `triggered=true`, `prepared_uncompressed_pixel_instances` is `0` and
  `transcoded_instances` equals the uncompressed images received.
- If `triggered=false`, uncompressed images were not a strict majority.

Events needing immediate attention:

- `study_failed`, `fatal_error`
- `DiskPressure` or any free-space message
- `Remote conflict`
- a size or SHA-1 mismatch
- a C-GET final status other than `0x0000`
- missing or unexpected instances

### Periodic remote verification

`verify` recomputes size and SHA-1 for every completed ZIP. It gets slower as
the backup grows: run it after the first batches, then weekly or when
investigating an anomaly, not every 10 minutes.

```bash
python scripts/dicom_cloud_backup.py --state-dir /var/lib/dicom-backup/state \
  --remote-root myremote:dicom-backup verify
```

Expected: `failure_count: 0`. These two listings must be empty:

```bash
rclone lsf --recursive --files-only --include='*.dcm' myremote:dicom-backup
rclone lsf --recursive --files-only --include='*.uploading' myremote:dicom-backup
```

## Responding to failures

**Low disk space.** Stop the timer and workers as shown above, record `df` and
`du`, and notify the person responsible. Do not free space by deleting state or
clinical objects.

**DICOM node unavailable.** C-ECHO only the affected node. Do not run a manual
C-FIND without an explicit `StudyDate`. The example units restart on failure
after 30 minutes; keep the service stopped if the failure persists or the
retries are loading the node.

**Remote or rclone unavailable.** Check `rclone about myremote: --json`. Do not
recreate the remote, change its region, or introduce encryption without
authorization. A valid local ZIP may remain in the work directory after a
failed upload; repeating the study is safe.

**Remote conflict or hash mismatch.** Stop the lane and escalate. Do not
delete, rename, or overwrite the remote object by hand.

**Duplicate worker.** `Another <lane> worker is already running` means the lock
did its job. Find the existing service; do not remove `.lock` files while a
process is alive.

## Finishing a backfill

When `backfill_gap_closed` appears for the configured end date, run a full
`verify`, confirm the daily lane is healthy, then disable the historical
service and leave the timer enabled:

```bash
sudo systemctl disable --now dicom-backup-backfill.service
```

## Operational prohibitions

- Never C-FIND without an exact `StudyDate` or a closed range.
- Never delete or modify studies on a source node.
- Never upload individual `.dcm` files.
- Never use a patient name in a file name, path, or log.
- Never overwrite a remote conflict.
- Never delete the SQLite state to "start over".
- Never change the destination, region, or encryption setup on your own.
- Never keep workers running below the hard free-space floor.
