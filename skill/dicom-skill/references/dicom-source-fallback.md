# DICOM source fallback

When the user asks to retrieve an exam but does not name a specific node, try
the configured source nodes in order instead of asking which node to use.

Define the order in your (private) config with a `source_fallback` list — see
`examples/dicom_nodes.local.yaml`, which is gitignored so real endpoints stay
out of the repository. The public `examples/dicom_nodes.yaml` shows the same
keys with placeholder nodes.

## Pattern

1. C-ECHO the first node in `source_fallback` to confirm connectivity. If the
   association is rejected or cannot connect, retry once, then move on to
   the next node.
2. Study-level C-FIND with the patient-name wildcard and, when no date is
   given, today's `StudyDate`.
3. If there is no match, repeat on the next node in the list.
4. Stop at the first node that returns the study, verify the exact registered
   `PatientName` there, then retrieve from that node.
5. Conclude "not found" only after every node in the list answered a
   successful association and returned no match; report which nodes were
   checked. A verified no-match is a result, not a retrieval failure.

## Example (placeholder nodes)

```bash
# First source
python scripts/dicom_dimse.py query --config examples/dicom_nodes.local.yaml --node primary \
  --model study --level STUDY \
  --filter PatientName=*doe* --filter StudyDate=20250101 \
  --return StudyDate --return StudyDescription --return StudyInstanceUID

# If no match, retry on the next configured source
python scripts/dicom_dimse.py query --config examples/dicom_nodes.local.yaml --node secondary \
  --model study --level STUDY \
  --filter PatientName=*doe* --filter StudyDate=20250101 \
  --return StudyDate --return StudyDescription --return StudyInstanceUID
```

Notes:

- Node hosts, ports, and AE titles belong only in the private local config.
- Run the same date/name criteria on every node, including inclusive ranges
  such as `StudyDate=20250101-20250107`.
- See `clinical-request-workflow.md` for exact-name verification and
  multi-study retrieval.
- Some sources support only `patient`/`study` query models; do not force a
  `series` C-FIND. Retrieve the study and inspect series locally.
