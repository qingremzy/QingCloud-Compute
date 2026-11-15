from __future__ import annotations

import os
import json
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from qingcloud.models import JobRecord, JobState, WorkerRecord
from qingcloud.scheduler import worker_can_run, rank_workers_for_job

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
            CREATE TABLE IF NOT EXISTS worker_job_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        _CONNECTION.execute("CREATE INDEX IF NOT EXISTS idx_worker_job_events_worker ON worker_job_events(worker_id, event_id DESC)")
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


def record_worker_job_event(job_id: str, worker_id: str, event_type: str, attempt: int, created_at: datetime | None = None) -> None:
    if event_type not in {"succeeded", "failed"}:
        raise ValueError("Unsupported worker job event type")
    stamp = (created_at or datetime.now(timezone.utc)).isoformat()
    with _LOCK, _CONNECTION:
        _CONNECTION.execute(
            "INSERT INTO worker_job_events(job_id, worker_id, event_type, attempt, created_at) VALUES(?, ?, ?, ?, ?)",
            (job_id, worker_id, event_type, attempt, stamp),
        )


def worker_reliability_stats(worker_id: str) -> dict[str, Any]:
    with _LOCK:
        row = _CONNECTION.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN event_type='succeeded' THEN 1 ELSE 0 END) AS succeeded, SUM(CASE WHEN event_type='failed' THEN 1 ELSE 0 END) AS failed, MAX(created_at) AS last_completed_at FROM worker_job_events WHERE worker_id = ? AND event_type IN ('succeeded', 'failed')",
            (worker_id,),
        ).fetchone()
    total = int(row["total"] or 0)
    succeeded = int(row["succeeded"] or 0)
    failed = int(row["failed"] or 0)
    if total == 0:
        return {"status": "unrated", "score": None, "sample_size": 0, "succeeded": 0, "failed": 0, "observed_success_rate": None, "last_completed_at": None}
    score = round((succeeded + 1) / (total + 2) * 100, 1)
    return {
        "status": "limited_data" if total < 10 else "established",
        "score": score, "sample_size": total, "succeeded": succeeded, "failed": failed,
        "observed_success_rate": round(succeeded / total * 100, 1),
        "last_completed_at": row["last_completed_at"],
    }


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
        _CONNECTION.execute("DELETE FROM worker_job_events")
        _CONNECTION.execute("DELETE FROM gpu_events")
        _CONNECTION.execute("DELETE FROM jobs")
        _CONNECTION.execute("DELETE FROM workers")


def claim_job_atomic(worker: WorkerRecord, jobs: dict[str, JobRecord], workers: dict[str, WorkerRecord] | None = None) -> JobRecord | None:
    """Claim one compatible queued job using a SQLite write transaction.

    BEGIN IMMEDIATE serializes claim transactions across API processes that share
    this SQLite database file. The in-memory mapping is refreshed for the claimed row.
    """
    with _LOCK:
        _CONNECTION.execute("BEGIN IMMEDIATE")
        try:
            rows = _CONNECTION.execute("SELECT job_id, data_json FROM jobs").fetchall()
            candidates = []
            persisted_jobs = [JobRecord.model_validate_json(row["data_json"]) for row in rows]
            for candidate in persisted_jobs:
                if candidate.state == JobState.queued and worker_can_run(worker, candidate):
                    candidates.append(candidate)
            candidates.sort(key=lambda job: (job.created_at, job.job_id))
            claimed = None
            if workers is not None:
                online_workers = list(workers.values())
                running_counts: dict[str, int] = {}
                for item in persisted_jobs:
                    if item.state == JobState.running and item.assigned_worker_id:
                        running_counts[item.assigned_worker_id] = running_counts.get(item.assigned_worker_id, 0) + 1
                reliability = {item.worker_id: worker_reliability_stats(item.worker_id) for item in online_workers}
                for candidate in candidates:
                    ranking = rank_workers_for_job(candidate, online_workers, running_counts, reliability)
                    if ranking and ranking[0]["worker_id"] == worker.worker_id:
                        claimed = candidate
                        break
            else:
                claimed = candidates[0] if candidates else None
            if claimed is None:
                _CONNECTION.commit()
                return None
            claimed.state = JobState.running
            claimed.assigned_worker_id = worker.worker_id
            claimed.attempts += 1
            claimed.updated_at = datetime.now(timezone.utc)
            cursor = _CONNECTION.execute("UPDATE jobs SET data_json = ? WHERE job_id = ?",
                (claimed.model_dump_json(), claimed.job_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Atomic job claim did not update exactly one row")
            _CONNECTION.execute(
                "INSERT INTO worker_job_events(job_id, worker_id, event_type, attempt, created_at) VALUES(?, ?, 'claimed', ?, ?)",
                (claimed.job_id, worker.worker_id, claimed.attempts, claimed.updated_at.isoformat()),
            )
            _CONNECTION.commit()
            jobs[claimed.job_id] = claimed
            return claimed
        except Exception:
            _CONNECTION.rollback()
            raise
