# Local DICOM series video export workflow

This example exports local DICOM image series to MP4 videos. It does not
connect to a DICOM node. The generated videos can contain burned-in patient
information.

List available series first when the folder may contain multiple exams or when
the modality has no automatic export policy:

```bash
mkdir -p audit videos

python scripts/dicom_volume_video.py \
  --path downloads/study \
  --out videos/study \
  --list-series \
  --include-descriptions \
  --summary \
  --out-json audit/series_list.json
```

Export the automatic CT/MR default set:

- CT: all series with more than 100 images at 10 frames/sec.
- MR: series with 15-100 images at 3 frames/sec.
- MR: series with more than 100 images at 10 frames/sec.

```bash
python scripts/dicom_volume_video.py \
  --path downloads/study \
  --out videos/study \
  --summary \
  --out-json audit/video_export.json
```

Export a specific series in all three planes at an explicit frame rate. Use this
for modalities without automatic policy, or whenever the user requests a
specific playback speed:

```bash
python scripts/dicom_volume_video.py \
  --path downloads/study \
  --out videos/study \
  --series-number 3 \
  --plane axial --plane sagittal --plane coronal \
  --frame-rate 15 \
  --summary \
  --out-json audit/video_series3.json
```
