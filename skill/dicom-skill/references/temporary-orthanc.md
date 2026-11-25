# Temporary Orthanc helper

Manual lifecycle:

```bash
python scripts/orthanc_temp.py start --aet AGENT --dicom-port 4242 --http-port 8042
python scripts/dicom_dimse.py echo --host 127.0.0.1 --port 4242 --aet AGENT --calling-aet AGENT
python scripts/orthanc_temp.py export --out /mnt/data/orthanc_export
python scripts/orthanc_temp.py stop --purge
```

The helper exposes Orthanc REST only on `127.0.0.1:8042` by default, while DICOM listens on host port `4242`. The temporary Orthanc is receive-only: it accepts C-ECHO and C-STORE addressed to its own AE title and refuses C-FIND, C-GET, and C-MOVE, so studies parked there cannot be listed or pulled by other nodes. The DICOM port is published on all host interfaces by default because the remote PACS must be able to connect back; narrow it with `--dicom-bind <interface IP>` and restrict senders with one or more `--allow-aet AET` (or `AET@HOST`) when the network is shared. Allowed senders are registered store-only (C-ECHO and C-STORE; query/retrieve stays refused for them too). Host checking applies to every sender, so use `AET@HOST` for all entries or for none; mixing the two forms is rejected. The retrieve flow exposes the same controls as `--orthanc-dicom-bind` and `--orthanc-allow-aet`. The container image is pinned to a fixed release; pass `--image`/`--orthanc-image` to change it. Stop the receiver with `stop --purge` as soon as the export is done.

The REST API requires authentication by default: `start` generates a random password for user `agent` and saves it (owner-readable only) in the helper's data dir, which defaults to a deterministic per-container-name location. Later `status`/`export`/`stop` calls with the same `--name` (or default name) find the credentials and data dir automatically, so no password handling is needed in the normal flow. The password is never printed: command output and audit JSON show only `http_password_saved`. Override with `--data-dir`, `--http-user`/`--http-password`, or `$DICOM_SKILL_ORTHANC_PASSWORD` only for custom setups. Use `start --no-http-auth` only when an unauthenticated localhost REST API is acceptable. The retrieve `--use-temp-orthanc` flow handles credentials automatically.
