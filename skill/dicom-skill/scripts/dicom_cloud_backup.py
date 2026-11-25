#!/usr/bin/env python3
"""Bounded, resumable per-study DICOM backup to an rclone remote.

The workflow retrieves one study at a time with C-GET, creates one stored
(uncompressed) ZIP64 archive, verifies it locally, uploads it atomically with
rclone, and persists PHI-light state in SQLite. It never sends individual DICOM
files to cloud storage, never overwrites a differing remote object, and never
modifies or deletes anything on the source DICOM node.

Any rclone remote that reports SHA-1 hashes can be used as the destination.
Nothing site-specific is built in: the source node, the remote root, and the
backfill date range are all supplied on the command line.

Two lanes exist so a historical backfill and a daily job can run side by side:

  run --lane daily    --node NODE [--date YYYYMMDD]          (default: yesterday)
  run --lane backfill --node NODE --start YYYYMMDD --end YYYYMMDD

Archive names and logs contain only dates, node names, technical UIDs, counts,
sizes, hashes and states; patient demographics are never queried or logged.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from pydicom import dcmread
from pydicom.uid import JPEG2000Lossless, UID


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_DICOM_CONFIG = SKILL_DIR / "examples" / "dicom_nodes.local.yaml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "dicom-cloud-backup"
DEFAULT_WORK_DIR = Path.home() / "dicom-backup-work"
REMOTE_ROOT_ENV = "DICOM_BACKUP_REMOTE_ROOT"
DATE_RE = re.compile(r"^\d{8}$")
UID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")
NODE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REMOTE_ROOT_RE = re.compile(r"^[A-Za-z0-9_.-]+:.*$")
GIB = 1024**3
DISK_WATCH_INTERVAL_SECONDS = 5
DISK_OPERATION_RESERVE_BYTES = 64 * 1024**2
BACKFILL_WINDOW_DAYS = 31


class BackupError(RuntimeError):
    pass


class DiskPressure(BackupError):
    pass


@dataclass(frozen=True)
class Study:
    node: str
    study_uid: str
    study_date: str
    study_time: str = ""
    advertised_instances: int | None = None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.study_date, self.study_time, self.study_uid)

    @property
    def remote_path_parts(self) -> tuple[str, str, str, str]:
        parsed = parse_date(self.study_date)
        return (f"{parsed.year:04d}", f"{parsed.month:02d}", f"{parsed.day:02d}", f"{self.study_uid}.zip")


@dataclass(frozen=True)
class BackupConfig:
    dicom_config: Path
    state_dir: Path
    work_dir: Path
    remote_root: str
    rclone: str
    python: str
    dimse_script: Path
    backfill_pause_bytes: int
    all_pause_bytes: int

    @property
    def database(self) -> Path:
        return self.state_dir / "state.sqlite3"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "timestamp": utc_now(), **fields}, sort_keys=True), flush=True)


def parse_date(value: str) -> dt.date:
    if not DATE_RE.fullmatch(value):
        raise BackupError(f"Invalid DICOM date {value!r}; expected YYYYMMDD")
    try:
        return dt.datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise BackupError(f"Invalid DICOM date {value!r}") from exc


def format_date(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def validate_date_filter(value: str) -> str:
    if "-" in value:
        start, end = value.split("-", 1)
        if not start or not end:
            raise BackupError("Open-ended StudyDate queries are forbidden")
        if parse_date(start) > parse_date(end):
            raise BackupError("StudyDate range starts after it ends")
        return value
    parse_date(value)
    return value


def safe_uid(value: str) -> str:
    if not UID_RE.fullmatch(value):
        raise BackupError(f"Invalid DICOM UID {value!r}")
    return value


def safe_node(value: str) -> str:
    if not NODE_RE.fullmatch(value or ""):
        raise BackupError(f"Invalid node name {value!r}; use the key from the YAML config")
    return value


def restrict_permissions(path: Path, mode: int) -> None:
    """Best-effort owner-only permissions; a no-op where POSIX modes do not apply."""
    try:
        path.chmod(mode)
    except OSError:
        pass


@contextlib.contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a non-blocking exclusive lock on ``path``; raise BlockingIOError if held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError(str(exc)) from exc
            try:
                yield
            finally:
                handle.seek(0)
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class StateDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self.path.parent, 0o700)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS studies (
                    node TEXT NOT NULL,
                    study_uid TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    study_date TEXT NOT NULL,
                    study_time TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    advertised_instances INTEGER,
                    expected_instances INTEGER,
                    received_instances INTEGER,
                    transfer_syntaxes_json TEXT,
                    archive_size INTEGER,
                    archive_sha1 TEXT,
                    remote_path TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (node, study_uid)
                );
                CREATE INDEX IF NOT EXISTS studies_status_idx ON studies(status, study_date);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
        restrict_permissions(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def discover(self, study: Study, lane: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO studies (
                    node, study_uid, lane, study_date, study_time, status,
                    advertised_instances, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'discovered', ?, ?, ?)
                ON CONFLICT(node, study_uid) DO UPDATE SET
                    lane=excluded.lane,
                    study_date=excluded.study_date,
                    study_time=excluded.study_time,
                    advertised_instances=excluded.advertised_instances,
                    updated_at=excluded.updated_at
                """,
                (
                    study.node,
                    study.study_uid,
                    lane,
                    study.study_date,
                    study.study_time,
                    study.advertised_instances,
                    now,
                    now,
                ),
            )

    def get(self, node: str, study_uid: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM studies WHERE node=? AND study_uid=?", (node, study_uid)
            ).fetchone()
        return dict(row) if row else None

    def mark(self, study: Study, status: str, **fields: Any) -> None:
        allowed = {
            "expected_instances",
            "received_instances",
            "transfer_syntaxes_json",
            "archive_size",
            "archive_sha1",
            "remote_path",
            "error",
            "completed_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown state fields: {sorted(unknown)}")
        assignments = ["status=?", "updated_at=?"]
        values: list[Any] = [status, utc_now()]
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(value)
        if status == "retrieving":
            # The retrieval phase is updated again after the instance C-FIND;
            # count an attempt only on the transition into this phase.
            assignments.append("attempts=attempts+CASE WHEN status!='retrieving' THEN 1 ELSE 0 END")
            assignments.append("error=NULL")
        values.extend([study.node, study.study_uid])
        with self.connect() as conn:
            conn.execute(
                f"UPDATE studies SET {', '.join(assignments)} WHERE node=? AND study_uid=?",
                values,
            )

    def list_rows(self, completed_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE status='completed'" if completed_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM studies {where} ORDER BY study_date, study_time, study_uid"
            ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = conn.execute(
                "SELECT status, COUNT(*) AS count FROM studies GROUP BY status ORDER BY status"
            ).fetchall()
            recent = conn.execute(
                """
                SELECT node, lane, study_uid, study_date, status, archive_size,
                       archive_sha1, remote_path, attempts, error, updated_at
                FROM studies ORDER BY updated_at DESC LIMIT 20
                """
            ).fetchall()
            coverage = conn.execute(
                """
                SELECT node, study_date, status, COUNT(*) AS count
                FROM studies
                GROUP BY node, study_date, status
                ORDER BY study_date DESC, node, status
                LIMIT 120
                """
            ).fetchall()
            metadata = conn.execute(
                "SELECT key, value, updated_at FROM metadata ORDER BY key"
            ).fetchall()
        return {
            "database": str(self.path),
            "counts": {row["status"]: row["count"] for row in counts},
            "coverage": [dict(row) for row in coverage],
            "metadata": {row["key"]: {"value": row["value"], "updated_at": row["updated_at"]} for row in metadata},
            "recent": [dict(row) for row in recent],
        }

    def metadata(self, key: str, default: str) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )


class BackupRunner:
    def __init__(self, config: BackupConfig):
        os.umask(0o077)
        self.config = config
        self.db = StateDB(config.database)
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        restrict_permissions(self.config.work_dir, 0o700)

    def _run(self, command: Sequence[str], *, allow_failure: bool = False) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("LC_ALL", "C.UTF-8")
        proc = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
        if proc.returncode and not allow_failure:
            detail = (proc.stderr or proc.stdout).strip()[-2000:]
            raise BackupError(f"Command failed ({proc.returncode}): {command[0]}: {detail}")
        return proc

    def _dimse(
        self,
        arguments: Sequence[str],
        *,
        monitor_free_bytes: int | None = None,
    ) -> dict[str, Any]:
        command = [self.config.python, str(self.config.dimse_script), *arguments]
        if monitor_free_bytes is None:
            proc = self._run(command)
        else:
            env = os.environ.copy()
            env.setdefault("LC_ALL", "C.UTF-8")
            with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(mode="w+") as stderr:
                process = subprocess.Popen(command, text=True, stdout=stdout, stderr=stderr, env=env)
                while True:
                    try:
                        returncode = process.wait(timeout=DISK_WATCH_INTERVAL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        free = shutil.disk_usage(self.config.work_dir).free
                        if free >= monitor_free_bytes:
                            continue
                        process.terminate()
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise DiskPressure(
                            f"C-GET interrupted at {free / GIB:.1f} GiB free; "
                            f"minimum is {monitor_free_bytes / GIB:.1f} GiB"
                        )
                stdout.seek(0)
                stderr.seek(0)
                proc = subprocess.CompletedProcess(
                    command, returncode, stdout.read(), stderr.read()
                )
                if proc.returncode:
                    detail = (proc.stderr or proc.stdout).strip()[-2000:]
                    raise BackupError(f"DIMSE command failed ({proc.returncode}): {detail}")
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackupError(f"DIMSE command returned invalid JSON: {proc.stdout[-1000:]}") from exc
        if result.get("error"):
            raise BackupError(f"DIMSE error: {result['error']}")
        if not result.get("established"):
            raise BackupError("DICOM association was not established")
        return result

    def echo(self, node: str) -> None:
        result = self._dimse(
            ["echo", "--config", str(self.config.dicom_config), "--node", node, "--timeout", "30"]
        )
        if (result.get("status") or {}).get("StatusHex") != "0x0000":
            raise BackupError(f"C-ECHO failed for {node}")

    def query_studies(self, node: str, date_filter: str) -> list[Study]:
        validate_date_filter(date_filter)
        args = [
            "query",
            "--config",
            str(self.config.dicom_config),
            "--node",
            node,
            "--model",
            "study",
            "--level",
            "STUDY",
            "--filter",
            f"StudyDate={date_filter}",
        ]
        for keyword in (
            "StudyDate",
            "StudyTime",
            "StudyInstanceUID",
            "NumberOfStudyRelatedInstances",
        ):
            args.extend(["--return", keyword])
        result = self._dimse(args)
        studies: list[Study] = []
        for match in result.get("matches") or []:
            uid = str(match.get("StudyInstanceUID") or "")
            date = str(match.get("StudyDate") or "")
            if not uid or not date:
                raise BackupError(f"C-FIND from {node} returned a study without UID or StudyDate")
            safe_uid(uid)
            parse_date(date)
            studies.append(
                Study(
                    node=node,
                    study_uid=uid,
                    study_date=date,
                    study_time=str(match.get("StudyTime") or ""),
                    advertised_instances=int_or_none(match.get("NumberOfStudyRelatedInstances")),
                )
            )
        return sorted({study.study_uid: study for study in studies}.values(), key=lambda item: item.sort_key)

    def query_instances(self, study: Study) -> dict[str, str]:
        args = [
            "query",
            "--config",
            str(self.config.dicom_config),
            "--node",
            study.node,
            "--model",
            "study",
            "--level",
            "IMAGE",
            "--filter",
            f"StudyDate={study.study_date}",
            "--filter",
            f"StudyInstanceUID={study.study_uid}",
        ]
        for keyword in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "SOPClassUID"):
            args.extend(["--return", keyword])
        result = self._dimse(args)
        instances: dict[str, str] = {}
        for match in result.get("matches") or []:
            sop_uid = str(match.get("SOPInstanceUID") or "")
            sop_class = str(match.get("SOPClassUID") or "")
            if not sop_uid or not sop_class:
                raise BackupError(f"Image C-FIND for {study.study_uid} returned incomplete identifiers")
            safe_uid(sop_uid)
            safe_uid(sop_class)
            previous = instances.setdefault(sop_uid, sop_class)
            if previous != sop_class:
                raise BackupError(f"SOP Instance {sop_uid} was reported with two SOP classes")
        if not instances:
            raise BackupError(f"No instances found for study {study.study_uid}")
        return instances

    def retrieve(
        self,
        study: Study,
        sop_classes: set[str],
        out_dir: Path,
        minimum_free_bytes: int,
    ) -> dict[str, Any]:
        args = [
            "retrieve",
            "--config",
            str(self.config.dicom_config),
            "--node",
            study.node,
            "--method",
            "get",
            "--model",
            "study",
            "--level",
            "STUDY",
            "--study-uid",
            study.study_uid,
            "--filter",
            f"StudyDate={study.study_date}",
            "--out",
            str(out_dir),
            "--timeout",
            "120",
        ]
        for sop_class in sorted(sop_classes):
            args.extend(["--store-sop-class", sop_class])
        return self._dimse(args, monitor_free_bytes=minimum_free_bytes)

    def inspect_retrieval(
        self,
        study: Study,
        expected: dict[str, str],
        retrieve_result: dict[str, Any],
        out_dir: Path,
    ) -> tuple[list[dict[str, Any]], Counter[str], Counter[str]]:
        responses = retrieve_result.get("responses") or []
        final_status = (responses[-1].get("status") if responses else None) or {}
        if final_status.get("StatusHex") != "0x0000":
            raise BackupError(f"C-GET final status was {final_status.get('StatusHex')}")
        if int_or_none(final_status.get("NumberOfFailedSuboperations")) not in (None, 0):
            raise BackupError("C-GET reported failed suboperations")
        if retrieve_result.get("store_errors"):
            raise BackupError(f"C-GET reported {len(retrieve_result['store_errors'])} local store errors")

        files = sorted(out_dir.rglob("*.dcm"))
        actual_uids: set[str] = set()
        records: list[dict[str, Any]] = []
        syntax_counts: Counter[str] = Counter()
        sop_class_counts: Counter[str] = Counter()
        for path in files:
            # Defer pixel bytes so we can distinguish image instances from SR,
            # PDF and other non-pixel objects without holding a study in RAM.
            ds = dcmread(path, defer_size="1 KB")
            study_uid = str(getattr(ds, "StudyInstanceUID", ""))
            sop_uid = str(getattr(ds, "SOPInstanceUID", ""))
            sop_class = str(getattr(ds, "SOPClassUID", ""))
            series_uid = str(getattr(ds, "SeriesInstanceUID", ""))
            transfer_syntax = str(getattr(ds.file_meta, "TransferSyntaxUID", ""))
            if study_uid != study.study_uid:
                raise BackupError(f"Retrieved instance belongs to unexpected study {study_uid}")
            if not all((sop_uid, sop_class, series_uid, transfer_syntax)):
                raise BackupError(f"Retrieved DICOM lacks required identifiers: {path}")
            if sop_uid in actual_uids:
                raise BackupError(f"Duplicate SOP Instance UID in retrieval: {sop_uid}")
            actual_uids.add(sop_uid)
            syntax_counts[transfer_syntax] += 1
            sop_class_counts[sop_class] += 1
            records.append(
                {
                    "path": str(path),
                    "series_uid": series_uid,
                    "sop_uid": sop_uid,
                    "sop_class_uid": sop_class,
                    "transfer_syntax_uid": transfer_syntax,
                    "has_pixel_data": "PixelData" in ds,
                }
            )

        expected_uids = set(expected)
        if actual_uids != expected_uids:
            missing = len(expected_uids - actual_uids)
            unexpected = len(actual_uids - expected_uids)
            raise BackupError(
                f"Retrieved instance set does not match C-FIND: missing={missing}, unexpected={unexpected}"
            )
        if len(files) != len(expected):
            raise BackupError(f"Expected {len(expected)} files, found {len(files)}")
        return records, syntax_counts, sop_class_counts

    def apply_compression_policy(
        self,
        study: Study,
        records: list[dict[str, Any]],
        minimum_free_bytes: int = 0,
    ) -> tuple[list[dict[str, Any]], Counter[str], Counter[str], dict[str, Any]]:
        """Losslessly transcode a study when uncompressed images predominate."""
        pixel_records = [record for record in records if record.get("has_pixel_data")]
        uncompressed = [
            record
            for record in pixel_records
            if not UID(str(record["transfer_syntax_uid"])).is_compressed
        ]
        compressed_count = len(pixel_records) - len(uncompressed)
        triggered = len(uncompressed) > compressed_count
        report: dict[str, Any] = {
            "policy": "transcode when uncompressed pixel instances are a strict majority",
            "pixel_instances": len(pixel_records),
            "received_uncompressed_pixel_instances": len(uncompressed),
            "received_compressed_pixel_instances": compressed_count,
            "triggered": triggered,
            "target_transfer_syntax_uid": str(JPEG2000Lossless) if triggered else None,
            "transcoded_instances": 0,
        }

        if triggered:
            source_bytes = sum(Path(str(record["path"])).stat().st_size for record in records)
            for record in uncompressed:
                path = Path(str(record["path"]))
                required = minimum_free_bytes + path.stat().st_size + DISK_OPERATION_RESERVE_BYTES
                free = shutil.disk_usage(self.config.work_dir).free
                if free < required:
                    raise DiskPressure(
                        f"Transcoding stopped at {free / GIB:.1f} GiB free; "
                        f"this instance requires {required / GIB:.1f} GiB including reserve"
                    )
                temporary = path.with_name(f".{path.name}.transcoding")
                temporary.unlink(missing_ok=True)
                try:
                    ds = dcmread(path)
                    original_sop_uid = str(ds.SOPInstanceUID)
                    original_study_uid = str(ds.StudyInstanceUID)
                    if "PixelData" not in ds:
                        raise BackupError(f"Pixel Data disappeared while preparing {original_sop_uid}")
                    ds.compress(JPEG2000Lossless, generate_instance_uid=False)
                    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
                    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
                    ds.save_as(temporary, enforce_file_format=True)
                    restrict_permissions(temporary, 0o600)

                    check = dcmread(temporary, stop_before_pixels=True)
                    if str(check.SOPInstanceUID) != original_sop_uid:
                        raise BackupError("Lossless transcoding changed SOPInstanceUID")
                    if str(check.StudyInstanceUID) != original_study_uid or original_study_uid != study.study_uid:
                        raise BackupError("Lossless transcoding changed StudyInstanceUID")
                    if str(check.file_meta.TransferSyntaxUID) != str(JPEG2000Lossless):
                        raise BackupError("Lossless transcoding did not produce JPEG 2000 lossless")
                    temporary.replace(path)
                    record["transfer_syntax_uid"] = str(JPEG2000Lossless)
                    report["transcoded_instances"] += 1
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise

            final_bytes = sum(Path(str(record["path"])).stat().st_size for record in records)
            report["source_bytes"] = source_bytes
            report["prepared_bytes"] = final_bytes
            report["size_ratio_source_to_prepared"] = (
                round(source_bytes / final_bytes, 4) if final_bytes else None
            )

        syntax_counts: Counter[str] = Counter(
            str(record["transfer_syntax_uid"]) for record in records
        )
        sop_class_counts: Counter[str] = Counter(str(record["sop_class_uid"]) for record in records)
        remaining_uncompressed = sum(
            1
            for record in records
            if record.get("has_pixel_data")
            and not UID(str(record["transfer_syntax_uid"])).is_compressed
        )
        report["prepared_uncompressed_pixel_instances"] = remaining_uncompressed
        report["prepared_transfer_syntax_counts"] = dict(sorted(syntax_counts.items()))
        if triggered and remaining_uncompressed:
            raise BackupError("Compression policy left uncompressed pixel instances after transcoding")
        return records, syntax_counts, sop_class_counts, report

    def check_space(self, lane: str) -> int:
        free = shutil.disk_usage(self.config.work_dir).free
        minimum = self.minimum_free_bytes(lane)
        if free < minimum:
            raise DiskPressure(
                f"Free space {free / GIB:.1f} GiB is below {minimum / GIB:.1f} GiB threshold for {lane}"
            )
        return free

    def minimum_free_bytes(self, lane: str) -> int:
        return self.config.backfill_pause_bytes if lane == "backfill" else self.config.all_pause_bytes

    def remote_path(self, study: Study) -> str:
        return "/".join([self.config.remote_root.rstrip("/"), *study.remote_path_parts])

    def remote_stat(self, remote_path: str) -> dict[str, Any] | None:
        proc = self._run(
            [self.config.rclone, "lsjson", "--stat", "--hash", remote_path], allow_failure=True
        )
        if proc.returncode:
            lowered = (proc.stderr or proc.stdout).lower()
            if "not found" in lowered or "doesn't exist" in lowered or "directory not found" in lowered:
                return None
            raise BackupError(f"Could not inspect remote object: {(proc.stderr or proc.stdout).strip()[-1000:]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BackupError("rclone lsjson returned invalid JSON") from exc

    def remote_sha1(self, remote_path: str, stat: dict[str, Any] | None = None) -> str:
        hashes = (stat or {}).get("Hashes") or {}
        for key, value in hashes.items():
            if key.lower().replace("-", "") == "sha1" and value:
                return str(value).lower()
        proc = self._run([self.config.rclone, "sha1sum", remote_path])
        first = proc.stdout.strip().split(maxsplit=1)
        if not first or not re.fullmatch(r"[0-9a-fA-F]{40}", first[0]):
            raise BackupError("The rclone remote did not return a usable SHA-1")
        return first[0].lower()

    def remote_matches(self, remote_path: str, size: int, sha1: str) -> bool:
        stat = self.remote_stat(remote_path)
        if stat is None or int(stat.get("Size", -1)) != size:
            return False
        return self.remote_sha1(remote_path, stat) == sha1.lower()

    def create_archive(
        self,
        study: Study,
        records: list[dict[str, Any]],
        syntax_counts: Counter[str],
        sop_class_counts: Counter[str],
        archive: Path,
        compression_report: dict[str, Any] | None = None,
        minimum_free_bytes: int = 0,
    ) -> tuple[int, str]:
        archive.parent.mkdir(parents=True, exist_ok=True)
        source_bytes = sum(Path(str(record["path"])).stat().st_size for record in records)
        required = minimum_free_bytes + source_bytes + DISK_OPERATION_RESERVE_BYTES
        free = shutil.disk_usage(self.config.work_dir).free
        if free < required:
            raise DiskPressure(
                f"ZIP creation stopped at {free / GIB:.1f} GiB free; "
                f"archive requires {required / GIB:.1f} GiB including reserve"
            )
        temporary = archive.with_suffix(".zip.partial")
        temporary.unlink(missing_ok=True)
        archive.unlink(missing_ok=True)
        parsed = parse_date(study.study_date)
        zip_time = (parsed.year, parsed.month, parsed.day, 0, 0, 0)
        instances: list[dict[str, str]] = []

        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for record in sorted(records, key=lambda item: (item["series_uid"], item["sop_uid"])):
                arcname = f"DICOM/{record['series_uid']}/{record['sop_uid']}.dcm"
                info = zipfile.ZipInfo(arcname, date_time=zip_time)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100600 << 16
                with Path(record["path"]).open("rb") as source, bundle.open(info, "w", force_zip64=True) as target:
                    shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
                instances.append(
                    {
                        "path": arcname,
                        "sop_instance_uid": record["sop_uid"],
                        "sop_class_uid": record["sop_class_uid"],
                        "transfer_syntax_uid": record["transfer_syntax_uid"],
                    }
                )

            manifest = {
                "schema_version": 1,
                "source_node": study.node,
                "study_instance_uid": study.study_uid,
                "study_date": study.study_date,
                "study_time": study.study_time,
                "instance_count": len(records),
                "sop_class_counts": dict(sorted(sop_class_counts.items())),
                "transfer_syntax_counts": dict(sorted(syntax_counts.items())),
                "compression_policy": compression_report,
                "instances": instances,
            }
            payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("ascii")
            info = zipfile.ZipInfo("manifest.json", date_time=zip_time)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100600 << 16
            bundle.writestr(info, payload)

        with zipfile.ZipFile(temporary, "r") as bundle:
            bad_member = bundle.testzip()
            if bad_member is not None:
                raise BackupError(f"ZIP CRC validation failed for {bad_member}")
            dcm_members = [name for name in bundle.namelist() if name.endswith(".dcm")]
            if len(dcm_members) != len(records) or "manifest.json" not in bundle.namelist():
                raise BackupError("ZIP contents do not match the retrieved study")
            if any(info.compress_type != zipfile.ZIP_STORED for info in bundle.infolist()):
                raise BackupError("ZIP unexpectedly contains compressed members")

        temporary.replace(archive)
        return archive.stat().st_size, sha1_file(archive)

    def upload_archive(self, archive: Path, remote_path: str, size: int, sha1: str) -> None:
        existing = self.remote_stat(remote_path)
        if existing is not None:
            if int(existing.get("Size", -1)) == size and self.remote_sha1(remote_path, existing) == sha1:
                emit("upload_already_complete", remote_path=remote_path, bytes=size, sha1=sha1)
                return
            raise BackupError(f"Remote conflict: {remote_path} already exists with different content")

        uploading = f"{remote_path}.uploading"
        stale = self.remote_stat(uploading)
        if stale is not None:
            self._run([self.config.rclone, "deletefile", uploading])
        self._run(
            [
                self.config.rclone,
                "copyto",
                "--no-traverse",
                "--transfers",
                "1",
                "--checkers",
                "2",
                str(archive),
                uploading,
            ]
        )
        if not self.remote_matches(uploading, size, sha1):
            raise BackupError("Uploaded temporary object failed size/SHA-1 validation")
        self._run([self.config.rclone, "moveto", uploading, remote_path])
        if not self.remote_matches(remote_path, size, sha1):
            raise BackupError("Final remote object failed size/SHA-1 validation")

    def process_study(self, study: Study, lane: str) -> dict[str, Any]:
        self.db.discover(study, lane)
        remote_path = self.remote_path(study)
        current = self.db.get(study.node, study.study_uid) or {}
        if current.get("status") == "completed":
            size = int(current.get("archive_size") or -1)
            sha1 = str(current.get("archive_sha1") or "")
            if size >= 0 and sha1 and self.remote_matches(remote_path, size, sha1):
                emit("study_skipped", node=study.node, study_uid=study.study_uid, reason="verified_complete")
                return {"study": asdict(study), "status": "skipped", "remote_path": remote_path}

        free = self.check_space(lane)
        emit(
            "study_started",
            lane=lane,
            node=study.node,
            study_uid=study.study_uid,
            study_date=study.study_date,
            free_gib=round(free / GIB, 2),
        )
        lane_dir = self.config.work_dir / lane
        lane_dir.mkdir(parents=True, exist_ok=True)
        restrict_permissions(lane_dir, 0o700)
        retrieve_dir = lane_dir / f"{study.study_uid}.retrieve"
        archive = lane_dir / f"{study.study_uid}.zip"
        if retrieve_dir.exists():
            shutil.rmtree(retrieve_dir)
        retrieve_dir.mkdir(parents=True, mode=0o700)

        try:
            self.db.mark(study, "retrieving")
            expected = self.query_instances(study)
            self.db.mark(study, "retrieving", expected_instances=len(expected))
            minimum_free_bytes = self.minimum_free_bytes(lane)
            result = self.retrieve(
                study,
                set(expected.values()),
                retrieve_dir,
                minimum_free_bytes,
            )
            self.check_space(lane)
            records, syntax_counts, sop_class_counts = self.inspect_retrieval(
                study, expected, result, retrieve_dir
            )
            received_syntax_counts = dict(sorted(syntax_counts.items()))
            emit(
                "study_retrieved",
                node=study.node,
                study_uid=study.study_uid,
                instances=len(records),
                transfer_syntaxes=received_syntax_counts,
            )
            records, syntax_counts, sop_class_counts, compression_report = self.apply_compression_policy(
                study, records, minimum_free_bytes
            )
            self.db.mark(
                study,
                "retrieved",
                received_instances=len(records),
                transfer_syntaxes_json=json.dumps(dict(sorted(syntax_counts.items())), sort_keys=True),
            )
            emit(
                "study_compression_prepared",
                node=study.node,
                study_uid=study.study_uid,
                **compression_report,
            )

            size, sha1 = self.create_archive(
                study,
                records,
                syntax_counts,
                sop_class_counts,
                archive,
                compression_report,
                minimum_free_bytes,
            )
            self.db.mark(study, "archived", archive_size=size, archive_sha1=sha1)
            shutil.rmtree(retrieve_dir)
            emit("study_archived", node=study.node, study_uid=study.study_uid, bytes=size, sha1=sha1)

            self.upload_archive(archive, remote_path, size, sha1)
            completed_at = utc_now()
            self.db.mark(
                study,
                "completed",
                archive_size=size,
                archive_sha1=sha1,
                remote_path=remote_path,
                completed_at=completed_at,
                error=None,
            )
            archive.unlink(missing_ok=True)
            emit(
                "study_completed",
                lane=lane,
                node=study.node,
                study_uid=study.study_uid,
                remote_path=remote_path,
                instances=len(records),
                bytes=size,
                sha1=sha1,
            )
            return {
                "study": asdict(study),
                "status": "completed",
                "remote_path": remote_path,
                "instances": len(records),
                "transfer_syntaxes": dict(sorted(syntax_counts.items())),
                "bytes": size,
                "sha1": sha1,
            }
        except Exception as exc:
            self.db.mark(study, "failed", error=str(exc)[-2000:])
            if retrieve_dir.exists():
                shutil.rmtree(retrieve_dir)
            emit("study_failed", node=study.node, study_uid=study.study_uid, error=str(exc))
            raise

    @contextlib.contextmanager
    def lane_lock(self, lane: str) -> Iterator[None]:
        lock_path = self.config.state_dir / f"{lane}.lock"
        try:
            with exclusive_file_lock(lock_path):
                yield
        except BlockingIOError as exc:
            raise BackupError(f"Another {lane} worker is already running") from exc

    def preflight(self, nodes: Sequence[str], lanes: Sequence[str]) -> None:
        for required in (self.config.dicom_config, self.config.dimse_script):
            if not required.is_file():
                raise BackupError(f"Required file is missing: {required}")
        if shutil.which(self.config.rclone) is None and not Path(self.config.rclone).is_file():
            raise BackupError(f"rclone is unavailable: {self.config.rclone}")
        require_remote_root(self.config.remote_root)
        self._run([self.config.rclone, "about", self.config.remote_root.split(":", 1)[0] + ":", "--json"])
        for node in nodes:
            self.echo(node)
        for lane in lanes:
            self.check_space(lane)
        emit("preflight_complete", database=str(self.config.database), work_dir=str(self.config.work_dir))

    def run_daily(self, node: str, date: str, max_studies: int | None) -> list[dict[str, Any]]:
        parse_date(date)
        studies = self.query_studies(node, date)
        if max_studies is not None:
            studies = studies[:max_studies]
        return [self.process_study(study, "daily") for study in studies]

    def run_backfill(
        self,
        node: str,
        start: str,
        end: str,
        max_studies: int | None,
        rescan: bool = False,
    ) -> list[dict[str, Any]]:
        """Walk a closed date range oldest-first in bounded windows.

        The next window start is persisted per node, so an interrupted backfill
        resumes where it stopped. ``rescan`` ignores the stored cursor; studies
        already completed are still skipped through the state database.
        """
        start_date = parse_date(start)
        end_date = parse_date(end)
        if start_date > end_date:
            raise BackupError("Backfill --start is after --end")
        cursor_key = f"backfill_cursor_date:{node}"
        cursor = start_date
        if not rescan:
            cursor = max(start_date, parse_date(self.db.metadata(cursor_key, start)))
        processed: list[dict[str, Any]] = []
        while cursor <= end_date:
            window_end = min(end_date, cursor + dt.timedelta(days=BACKFILL_WINDOW_DAYS - 1))
            studies = self.query_studies(node, f"{format_date(cursor)}-{format_date(window_end)}")
            for study in studies:
                row = self.db.get(study.node, study.study_uid)
                if row and row.get("status") == "completed":
                    continue
                if max_studies is not None and len(processed) >= max_studies:
                    return processed
                processed.append(self.process_study(study, "backfill"))
            cursor = window_end + dt.timedelta(days=1)
            self.db.set_metadata(cursor_key, format_date(cursor))
            emit("backfill_cursor_advanced", node=node, next_date=format_date(cursor))
        emit("backfill_gap_closed", node=node, through=end)
        return processed

    def verify_completed(self) -> dict[str, Any]:
        checked: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for row in self.db.list_rows(completed_only=True):
            path = str(row.get("remote_path") or "")
            size = int(row.get("archive_size") or -1)
            sha1 = str(row.get("archive_sha1") or "")
            ok = bool(path and size >= 0 and sha1 and self.remote_matches(path, size, sha1))
            item = {
                "node": row["node"],
                "study_uid": row["study_uid"],
                "remote_path": path,
                "bytes": size,
                "sha1": sha1,
                "verified": ok,
            }
            checked.append(item)
            if not ok:
                failed.append(item)
        return {"checked": checked, "checked_count": len(checked), "failure_count": len(failed), "failures": failed}


def require_remote_root(value: str) -> str:
    if not value:
        raise BackupError(
            f"No destination configured; pass --remote-root REMOTE:PATH or set {REMOTE_ROOT_ENV}"
        )
    if not REMOTE_ROOT_RE.fullmatch(value):
        raise BackupError("--remote-root must be an rclone remote path such as myremote:dicom-backup")
    return value


def yesterday_local() -> str:
    return format_date(dt.date.today() - dt.timedelta(days=1))


def make_config(args: argparse.Namespace) -> BackupConfig:
    if args.min_free_gib < 0 or args.backfill_min_free_gib < 0:
        raise BackupError("Free-space thresholds cannot be negative")
    return BackupConfig(
        dicom_config=Path(args.dicom_config).expanduser().resolve(),
        state_dir=Path(args.state_dir).expanduser().resolve(),
        work_dir=Path(args.work_dir).expanduser().resolve(),
        remote_root=(args.remote_root or "").rstrip("/"),
        rclone=args.rclone,
        python=args.python,
        dimse_script=Path(args.dimse_script).expanduser().resolve(),
        backfill_pause_bytes=int(args.backfill_min_free_gib * GIB),
        all_pause_bytes=int(args.min_free_gib * GIB),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resumable per-study DICOM ZIP backup to an rclone remote",
        epilog="Global options go before the subcommand, e.g. "
        "dicom_cloud_backup.py --remote-root myremote:dicom-backup run --lane daily --node pacs",
    )
    parser.add_argument("--dicom-config", default=str(DEFAULT_DICOM_CONFIG), help="YAML node config used by dicom_dimse.py")
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR), help="Directory for the SQLite state and lane locks")
    parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR), help="Scratch directory for retrieved studies and ZIPs")
    parser.add_argument(
        "--remote-root",
        default=os.environ.get(REMOTE_ROOT_ENV, ""),
        help=f"rclone destination such as myremote:dicom-backup (or set {REMOTE_ROOT_ENV})",
    )
    parser.add_argument("--rclone", default="rclone", help="rclone executable name or path")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used to run dicom_dimse.py")
    parser.add_argument("--dimse-script", default=str(SCRIPT_DIR / "dicom_dimse.py"))
    parser.add_argument("--min-free-gib", type=float, default=30.0, help="Hard free-space floor for every lane")
    parser.add_argument("--backfill-min-free-gib", type=float, default=40.0, help="More conservative floor for the backfill lane")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one backup lane against one source node")
    run_parser.add_argument("--lane", choices=("backfill", "daily"), required=True)
    run_parser.add_argument("--node", required=True, help="Source node name from the YAML config")
    run_parser.add_argument("--date", help="Daily lane: exact YYYYMMDD; defaults to yesterday (local time)")
    run_parser.add_argument("--start", help="Backfill lane: first StudyDate, YYYYMMDD (inclusive)")
    run_parser.add_argument("--end", help="Backfill lane: last StudyDate, YYYYMMDD (inclusive)")
    run_parser.add_argument("--rescan", action="store_true", help="Backfill lane: ignore the stored cursor and re-walk the range")
    run_parser.add_argument("--max-studies", type=int, help="Stop after this many new studies (use a small value for a first controlled test)")

    subparsers.add_parser("status", help="Show PHI-light local backup state")
    subparsers.add_parser("verify", help="Recheck size and SHA-1 for all completed remote ZIPs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = make_config(args)
        runner = BackupRunner(config)
        if args.command == "run":
            if args.max_studies is not None and args.max_studies < 1:
                raise BackupError("--max-studies must be positive")
            node = safe_node(args.node)
            date = ""
            if args.lane == "daily":
                if args.start or args.end or args.rescan:
                    raise BackupError("--start/--end/--rescan apply only to the backfill lane")
                date = args.date or yesterday_local()
                parse_date(date)
            else:
                if args.date:
                    raise BackupError("--date applies only to the daily lane")
                if not args.start or not args.end:
                    raise BackupError("The backfill lane requires a closed range: --start and --end")
                validate_date_filter(f"{args.start}-{args.end}")
            runner.preflight(nodes=(node,), lanes=(args.lane,))
            with runner.lane_lock(args.lane):
                if args.lane == "daily":
                    result: Any = runner.run_daily(node, date, args.max_studies)
                else:
                    result = runner.run_backfill(node, args.start, args.end, args.max_studies, args.rescan)
        elif args.command == "status":
            result = runner.db.summary()
        elif args.command == "verify":
            require_remote_root(config.remote_root)
            result = runner.verify_completed()
            if result["failure_count"]:
                print(json.dumps(result, indent=2, sort_keys=True))
                return 1
        else:  # pragma: no cover
            raise BackupError(f"Unknown command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        emit("fatal_error", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
