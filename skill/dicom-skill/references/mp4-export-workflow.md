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