from __future__ import annotations

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
