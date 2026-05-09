# Local PDF dicomizer workflow

This example wraps a local PDF as a DICOM Encapsulated PDF Storage instance.
It does not OCR, anonymize, or inspect the PDF contents.

```bash
mkdir -p dicomized audit

python scripts/dicom_pdf.py \
  --pdf reports/report.pdf \
  --out dicomized/pdf \
  --patient-id TEST123 \
  --patient-name "Test^Patient" \
  --accession-number ACC123 \
  --summary \
  --out-json audit/pdf_dicomize.json
```

To attach the PDF to an existing study, copy patient/study metadata from a
reference DICOM instance:

```bash
python scripts/dicom_pdf.py \
  --pdf reports/report.pdf \
  --metadata-from downloads/study/instance.dcm \
  --out dicomized/pdf \
  --document-title "Final report" \
  --summary
```

The generated DICOM stores the PDF bytes in `EncapsulatedDocument` with
`MIMETypeOfEncapsulatedDocument=application/pdf` and
`SOPClassUID=EncapsulatedPDFStorage`.
