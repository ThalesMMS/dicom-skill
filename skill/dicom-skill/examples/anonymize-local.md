# Local anonymization workflow

This example anonymizes a local folder of DICOM files, writes anonymized files to a separate directory, and stores a JSON audit artifact.

```bash
mkdir -p audit anonymized

python scripts/dicom_anonymize.py \
  --path downloads/study \
  --out anonymized/study \
  --site-id 123456 \
  --project-name local_test \
  --summary \
  --out-json audit/anonymize_result.json
```

For a deterministic project salt without putting it in shell history:

```bash
export DICOM_ANON_SALT="replace-with-project-secret"

python scripts/dicom_anonymize.py \
  --path downloads/study \
  --out anonymized/study \
  --site-id 123456 \
  --salt-env DICOM_ANON_SALT \
  --summary
```

To preserve patient mappings across multiple batches, use `--map-json`. This file can contain PHI because original identifiers may be used as keys.

```bash
python scripts/dicom_anonymize.py \
  --path downloads/batch2 \
  --out anonymized/batch2 \
  --site-id 123456 \
  --map-json secure/anon_map.json \
  --summary
```

This anonymizer does not remove burned-in pixel annotations. Review pixel data separately before external sharing.
