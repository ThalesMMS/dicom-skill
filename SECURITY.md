# Security policy

`dicom-skill` moves medical images between systems, so security and privacy
problems are treated as high priority.

## Reporting a vulnerability

Please do **not** open a public issue for a suspected vulnerability.

Use GitHub's private reporting instead: on the repository page open
**Security → Report a vulnerability**. Include the affected script and version
or commit, a description of the impact, and steps to reproduce.

Never attach real patient data, real DICOM files, screenshots of clinical
systems, hostnames, AE titles, or credentials to a report. Reproduce with
synthetic data (the test suite's `make_ct_dataset` helper is enough for most
cases).

You can expect an acknowledgement within a few days. Fixes are released on the
default branch and noted in [`CHANGELOG.md`](CHANGELOG.md). Only the latest
release is supported.

## In scope

- Writing files outside the requested output directory.
- Leaking patient data or credentials through stdout, logs, audit JSON
  permissions, archive names, or manifests.
- The temporary Orthanc receiver accepting more than it should.
- Weaknesses in pseudonym generation (salts, UID/date mapping).
- The cloud backup overwriting, deleting, or mis-verifying remote objects.
- Instructions in `SKILL.md` or `references/` that would lead an agent to move
  data without the user's authorization.

## Out of scope

- Security of the PACS, VNA, Orthanc, Docker, rclone, or cloud provider
  themselves.
- Plain-TCP DICOM being unencrypted when TLS flags are not used. That is how
  DIMSE works; use `--tls` where the peer supports it and keep DICOM traffic on
  trusted networks.
- Burned-in pixel annotations surviving metadata anonymization. This is a
  documented limitation.

## Operating notes for deployers

- The skill is not a medical device and is not validated for clinical use.
  Institutional authorization, data-protection compliance, and validation are
  the deployer's responsibility.
- Keep real endpoints and site conventions in the gitignored `*.local.yaml` and
  `*.local.md` files.
- Audit JSON, mapping files, salt files, and the Orthanc state file are written
  owner-only (mode 0600) on POSIX systems. On Windows, protect the folders with
  NTFS permissions.
- The temporary Orthanc publishes its DICOM port on all interfaces by default
  so a PACS can reach it. Use `--dicom-bind`, `--allow-aet`, and a host
  firewall on shared networks, and purge it after use.
- The cloud backup uploads identifiable, unencrypted DICOM. Use an approved
  destination, or an `rclone crypt` remote when client-side encryption is
  required.
- Anonymization here is metadata pseudonymization. Review output before any
  external sharing.
