from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qingcloud.models import JobRecord, JobState, WorkerRecord


def worker_can_run(worker: WorkerRecord, job: JobRecord) -> bool:
    if worker.state.value != "online":
        return False
    if job.required_gpu_vram_mb:
        available = max((gpu.vram_mb for gpu in worker.gpus), default=0)
        if available < job.required_gpu_vram_mb:
            return False
    return all(worker.labels.get(key) == value for key, value in job.required_labels.items())


def claim_next_job(worker: WorkerRecord, jobs: dict[str, JobRecord]) -> JobRecord | None:
    candidates = sorted(
        (job for job in jobs.values() if job.state == JobState.queued and worker_can_run(worker, job)),
        key=lambda job: (job.created_at, job.job_id),
    )
    if not candidates:
        return None
    job = candidates[0]
    job.state = JobState.running
    job.assigned_worker_id = worker.worker_id
    job.attempts += 1
    job.updated_at = job.updated_at.__class__.now(job.updated_at.tzinfo)
    return job


def recover_expired_jobs(jobs: dict[str, JobRecord], current_time: datetime | None = None, lease_seconds: int = 180) -> list[JobRecord]:
    """Requeue abandoned jobs or fail them after their configured attempt limit."""
    current_time = current_time or datetime.now(timezone.utc)
    recovered: list[JobRecord] = []
    for job in jobs.values():
        if job.state != JobState.running:
            continue
        if current_time - job.updated_at <= timedelta(seconds=lease_seconds):
            continue
        job.assigned_worker_id = None
        job.progress = 0
        job.updated_at = current_time
        if job.attempts < job.max_attempts:
            job.state = JobState.queued
            job.error = "Worker lease expired. Job returned to queue."
        else:
            job.state = JobState.failed
            job.error = "Worker lease expired after maximum attempts."
        recovered.append(job)
    return recovered
