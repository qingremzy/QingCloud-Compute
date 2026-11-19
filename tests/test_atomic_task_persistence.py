from uuid import uuid4

from qingcloud.db import _CONNECTION, save_jobs_atomic
from qingcloud.models import JobRecord


def test_save_jobs_atomic_persists_every_chunk_as_one_batch():
    task_id = str(uuid4())
    jobs = [
        JobRecord(
            job_id=str(uuid4()),
            task_id=task_id,
            kind="sum_range",
            payload={"start": start, "end": start + 9},
        )
        for start in (0, 10, 20)
    ]
    job_ids = [job.job_id for job in jobs]
    try:
        save_jobs_atomic(jobs)
        rows = _CONNECTION.execute(
            "SELECT job_id, data_json FROM jobs WHERE job_id IN (?, ?, ?)",
            job_ids,
        ).fetchall()
        assert {row["job_id"] for row in rows} == set(job_ids)
        assert all(task_id in row["data_json"] for row in rows)
    finally:
        with _CONNECTION:
            _CONNECTION.executemany("DELETE FROM jobs WHERE job_id = ?", [(job_id,) for job_id in job_ids])
