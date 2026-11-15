from __future__ import annotations

import os
import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from qingcloud.models import JobRecord, JobState, WorkerRecord
from qingcloud.scheduler import worker_can_run

_DB_PATH = os.getenv("QCLOUD_DB_PATH", "qingcloud.sqlite3")
_CONNECTION = sqlite3.connect(_DB_PATH, check_same_thread=False)
_CONNECTION.row_factory = sqlite3.Row
_LOCK = RLock()


def init_db() -> None:
    with _LOCK, _CONNECTION:
        _CONNECTION.execute("PRAGMA foreign_keys = ON")
        _CONNECTION.execute("PRAGMA busy_timeout = 5000")
        _CONNECTION.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL
            )
        """)
        _CONNECTION.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL
            )
        """)
        _CONNECTION.execute("""
            CREATE TABLE IF NOT EXISTS gpu_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL
            )
        """)
        _CONNECTION.execute("CREATE INDEX IF NOT EXISTS idx_gpu_events_worker ON gpu_events(worker_id, event_id DESC)")
        _CONNECTION.execute("""
            CREATE TABLE IF NOT EXISTS worker_tokens (
                worker_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL,
                FOREIGN KEY(worker_id) REFERENCES workers(worker_id) ON DELETE CASCADE
            )
        """)


def save_worker(worker: WorkerRecord) -> None:
    with _LOCK, _CONNECTION:
        _CONNECTION.execute(
            "INSERT INTO workers(worker_id, data_json) VALUES(?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET data_json=excluded.data_json",
            (worker.worker_id, worker.model_dump_json()),
        )


def save_job(job: JobRecord) -> None:
    with _LOCK, _CONNECTION:
        _CONNECTION.execute(
            "INSERT INTO jobs(job_id, data_json) VALUES(?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET data_json=excluded.data_json",
            (job.job_id, job.model_dump_json()),
        )


def record_gpu_event(worker_id: str, event_type: str, before: list[dict[str, Any]], after: list[dict[str, Any]], created_at: datetime | None = None) -> None:
    stamp = (created_at or datetime.now(timezone.utc)).isoformat()
    with _LOCK, _CONNECTION:
        _CONNECTION.execute(
            "INSERT INTO gpu_events(worker_id, event_type, created_at, before_json, after_json) VALUES(?, ?, ?, ?, ?)",
            (worker_id, event_type, stamp, json.dumps(before, sort_keys=True), json.dumps(after, sort_keys=True)),
        )


def list_gpu_events(worker_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK:
        rows = _CONNECTION.execute(
            "SELECT event_id, worker_id, event_type, created_at, before_json, after_json FROM gpu_events WHERE worker_id = ? ORDER BY event_id DESC LIMIT ?",
            (worker_id, max(1, min(limit, 200))),
        ).fetchall()
    return [{
        "event_id": row["event_id"], "worker_id": row["worker_id"],
        "event_type": row["event_type"], "created_at": row["created_at"],
        "before": json.loads(row["before_json"]), "after": json.loads(row["after_json"]),
    } for row in rows]


def save_worker_token(worker_id: str, token_hash: str) -> None:
    with _LOCK, _CONNECTION:
        _CONNECTION.execute(
            "INSERT INTO worker_tokens(worker_id, token_hash) VALUES(?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET token_hash=excluded.token_hash",
            (worker_id, token_hash),
        )


def load_state(
    workers: dict[str, WorkerRecord],
    jobs: dict[str, JobRecord],
    token_hashes: dict[str, str],
) -> None:
    with _LOCK:
        workers.clear()
        jobs.clear()
        token_hashes.clear()
        for row in _CONNECTION.execute("SELECT data_json FROM workers"):
            worker = WorkerRecord.model_validate_json(row["data_json"])
            workers[worker.worker_id] = worker
        for row in _CONNECTION.execute("SELECT data_json FROM jobs"):
            job = JobRecord.model_validate_json(row["data_json"])
            jobs[job.job_id] = job
        for row in _CONNECTION.execute("SELECT worker_id, token_hash FROM worker_tokens"):
            token_hashes[row["worker_id"]] = row["token_hash"]


def clear_all() -> None:
    """Clear persistent state. Intended for tests and explicit maintenance."""
    with _LOCK, _CONNECTION:
        _CONNECTION.execute("DELETE FROM worker_tokens")
        _CONNECTION.execute("DELETE FROM jobs")
        _CONNECTION.execute("DELETE FROM workers")


def claim_job_atomic(worker: WorkerRecord, jobs: dict[str, JobRecord]) -> JobRecord | None:
    """Claim one compatible queued job using a SQLite write transaction.

    BEGIN IMMEDIATE serializes claim transactions across API processes that share
    this SQLite database file. The in-memory mapping is refreshed for the claimed row.
    """
    with _LOCK:
        _CONNECTION.execute("BEGIN IMMEDIATE")
        try:
            rows = _CONNECTION.execute("SELECT job_id, data_json FROM jobs").fetchall()
            candidates = []
            for row in rows:
                candidate = JobRecord.model_validate_json(row["data_json"])
                if candidate.state == JobState.queued and worker_can_run(worker, candidate):
                    candidates.append(candidate)
            candidates.sort(key=lambda job: (job.created_at, job.job_id))
            if not candidates:
                _CONNECTION.commit()
                return None
            claimed = candidates[0]
            claimed.state = JobState.running
            claimed.assigned_worker_id = worker.worker_id
            claimed.attempts += 1
            claimed.updated_at = datetime.now(timezone.utc)
            cursor = _CONNECTION.execute("UPDATE jobs SET data_json = ? WHERE job_id = ?",
                (claimed.model_dump_json(), claimed.job_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Atomic job claim did not update exactly one row")
            _CONNECTION.commit()
            jobs[claimed.job_id] = claimed
            return claimed
        except Exception:
            _CONNECTION.rollback()
            raise
