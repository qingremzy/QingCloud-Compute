from __future__ import annotations

import os
import sqlite3
from threading import RLock
from typing import Any

from qingcloud.models import JobRecord, WorkerRecord

_DB_PATH = os.getenv("QCLOUD_DB_PATH", "qingcloud.sqlite3")
_CONNECTION = sqlite3.connect(_DB_PATH, check_same_thread=False)
_CONNECTION.row_factory = sqlite3.Row
_LOCK = RLock()


def init_db() -> None:
    with _LOCK, _CONNECTION:
        _CONNECTION.execute("PRAGMA foreign_keys = ON")
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
