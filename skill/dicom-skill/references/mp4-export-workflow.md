# MP4 export workflow notes

Observed workflow for `scripts/dicom_volume_video.py`:

- Start with `--list-series --include-descriptions` when the study folder may contain multiple series.
- The listing output identifies which series was auto-selected by the CT/MR policy and why.
- For a user-requested view plane, pass an explicit `--plane` value such as `sagittal` (or repeat `--plane` for multiple views).
- For a specific series, prefer `--series-number` or `--series-uid` rather than relying on the auto-selected series.
- Use `--summary --out-json` to capture the chosen series, frame rate, and output paths for audit/debugging.

Example pattern:

```bash
python scripts/dicom_volume_video.py \
  --path /tmp/dicom_study \
  --out /tmp/videos \
  --list-series --include-descriptions

python scripts/dicom_volume_video.py \
  --path /tmp/dicom_study \
  --out /tmp/videos \
  --series-number 2 \
  --plane sagittal \
  --frame-rate 10 \
  --summary --out-json /tmp/audit/video.json
```

Notes:

- CT series with more than 100 images are typically exported at 10 fps by default.
- In mixed or multi-series studies, inspect the listing before exporting to avoid choosing the wrong series.
- MP4 exports can contain burned-in annotations and should be treated as sensitive.

## Full option reference

Use `scripts/dicom_volume_video.py` to export local DICOM image series from a study folder to MP4. When a study contains multiple series, run `--list-series --include-descriptions` first and export the exact series by `--series-number`/`--series-uid` rather than trusting the auto-selected default; the listing shows which series was selected and why, which is the safest way to avoid exporting the wrong one in mixed studies.

When delivering an exported video to the user over a chat client, send it as a native attachment using a bare `MEDIA:` line with the absolute file path (for example `MEDIA:/tmp/videos/study_axial.mp4`). Do not prepend emoji, labels, quotes, or other text on that line, or the client may render it as plain text instead of a media attachment.

By default it applies modality-specific automatic policies:

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

The script applies grayscale Modality LUT/rescale, uses manual `--window-center/--window-width` when supplied, otherwise uses DICOM window values when present, and falls back to percentile/min-max normalization. Use `--percentile-window 0.5,99.5`, `--max-size`, `--reverse`, or `--overwrite` when needed. MP4 exports can contain burned-in annotations and must be treated as sensitive. See `references/mp4-export-workflow.md` for a concise export pattern and selection notes, and `references/dicom-source-fallback.md` for querying multiple source nodes in order.

Selection rules:

- Without a series selector, export only CT/MR series matching the automatic policy.
- With `--series-uid`, `--series-number`, or `--series-description-contains`, export matching series even when they do not match automatic thresholds.
- For selected non-CT/non-MR series, `--frame-rate` is required.
- Without `--plane`, export only axial MP4 output. Use repeated `--plane` flags or comma-separated values for sagittal and coronal output.
- Without `--frame-rate`, use the CT/MR modality default only when one exists.
