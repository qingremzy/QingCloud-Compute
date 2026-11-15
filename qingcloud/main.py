from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from threading import RLock

from fastapi import FastAPI, Header, HTTPException

from qingcloud.db import claim_job_atomic, init_db, load_state, save_job, save_worker, save_worker_token
from qingcloud.models import (
    DistributedSumCreate, JobCompletion, JobCreate, JobHeartbeat, JobRecord, JobState,
    WorkerEnrollmentResponse, WorkerRecord, WorkerRegistration, WorkerState,
)
from qingcloud.scheduler import recover_expired_jobs
from qingcloud.security import (
    hash_worker_token, issue_worker_token, require_worker_token,
    verify_enrollment_token, verify_admin_token,
)

app = FastAPI(
    title="QingCloud Compute",
    version="0.3.0",
    description="A $0-first distributed compute control-plane prototype with SQLite persistence.",
)

workers: dict[str, WorkerRecord] = {}
jobs: dict[str, JobRecord] = {}
worker_token_hashes: dict[str, str] = {}
claim_lock = RLock()

init_db()
load_state(workers, jobs, worker_token_hashes)
# A worker cannot be assumed alive after the control plane restarts.
for _worker in workers.values():
    _worker.state = WorkerState.offline
    save_worker(_worker)


def now():
    return datetime.now(timezone.utc)


def authenticate_worker(worker_id: str, token: str | None) -> WorkerRecord:
    require_worker_token(worker_id, token, worker_token_hashes)
    worker = workers.get(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


@app.get("/health")
def health():
    return {"status": "ok", "service": "qingcloud-compute", "version": app.version}


@app.get("/v1/overview")
def overview():
    gpu_vram_mb = sum(gpu.vram_mb for worker in workers.values() for gpu in worker.gpus)
    online = [w for w in workers.values() if w.state.value == "online"]
    return {
        "workers": len(workers),
        "online_workers": len(online),
        "offline_workers": len(workers) - len(online),
        "reported_cpu_cores_online": sum(w.cpu_cores for w in online),
        "reported_ram_mb_online": sum(w.ram_mb for w in online),
        "reported_gpu_count_online": sum(len(w.gpus) for w in online),
        "reported_gpu_vram_mb_across_network": gpu_vram_mb,
        "jobs": len(jobs),
        "queued_jobs": sum(j.state.value == "queued" for j in jobs.values()),
        "running_jobs": sum(j.state.value == "running" for j in jobs.values()),
        "storage": "sqlite",
        "note": "Hardware totals are reported inventory only. Aggregate VRAM is not one unified GPU memory pool, and reported capacity is not guaranteed available capacity.",
    }


@app.post("/v1/workers/register", response_model=WorkerEnrollmentResponse)
def register_worker(
    data: WorkerRegistration,
    enrollment_token: str | None = Header(default=None, alias="X-QingCloud-Enrollment-Token"),
):
    verify_enrollment_token(enrollment_token)
    worker_id = str(uuid4())
    record = WorkerRecord(**data.model_dump(), worker_id=worker_id)
    access_token = issue_worker_token()
    token_hash = hash_worker_token(access_token)
    workers[worker_id] = record
    worker_token_hashes[worker_id] = token_hash
    save_worker(record)
    save_worker_token(worker_id, token_hash)
    return WorkerEnrollmentResponse(worker=record, access_token=access_token)


@app.get("/v1/workers", response_model=list[WorkerRecord])
def list_workers():
    return list(workers.values())


@app.post("/v1/workers/{worker_id}/heartbeat")
def worker_heartbeat(
    worker_id: str,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    worker = authenticate_worker(worker_id, token)
    worker.last_seen_at = now()
    worker.state = WorkerState.online
    save_worker(worker)
    return {"ok": True, "last_seen_at": worker.last_seen_at}


@app.put("/v1/workers/{worker_id}/capabilities", response_model=WorkerRecord)
def update_worker_capabilities(
    worker_id: str,
    data: WorkerRegistration,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    """Refresh a worker's self-reported hardware inventory using its own token."""
    worker = authenticate_worker(worker_id, token)
    normalized = WorkerRegistration.model_validate(data.model_dump())
    for field in WorkerRegistration.model_fields:
        setattr(worker, field, getattr(normalized, field))
    worker.last_seen_at = now()
    worker.state = WorkerState.online
    save_worker(worker)
    return worker


@app.post("/v1/jobs", response_model=JobRecord)
def submit_job(
    data: JobCreate,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    job_id = str(uuid4())
    record = JobRecord(**data.model_dump(), job_id=job_id)
    jobs[job_id] = record
    save_job(record)
    return record



@app.post("/v1/distributed/sum")
def create_distributed_sum(
    data: DistributedSumCreate,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    """Split an inclusive integer range into independently schedulable CPU jobs."""
    if data.end < data.start:
        raise HTTPException(status_code=422, detail="end must be greater than or equal to start")
    total_count = data.end - data.start + 1
    if total_count > 1_000_000:
        raise HTTPException(status_code=422, detail="A distributed sum is limited to 1,000,000 integers")
    task_id = str(uuid4())
    chunk_ids = []
    chunk_start = data.start
    while chunk_start <= data.end:
        chunk_end = min(chunk_start + data.chunk_size - 1, data.end)
        record = JobRecord(
            job_id=str(uuid4()),
            task_id=task_id,
            kind="sum_range",
            payload={"start": chunk_start, "end": chunk_end},
            max_attempts=2,
        )
        jobs[record.job_id] = record
        save_job(record)
        chunk_ids.append(record.job_id)
        chunk_start = chunk_end + 1
    return {
        "task_id": task_id,
        "kind": "distributed_sum",
        "start": data.start,
        "end": data.end,
        "integer_count": total_count,
        "chunk_count": len(chunk_ids),
        "job_ids": chunk_ids,
        "status": "queued",
    }


@app.get("/v1/distributed/sum/{task_id}")
def get_distributed_sum(
    task_id: str,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    task_jobs = [job for job in jobs.values() if job.task_id == task_id]
    if not task_jobs:
        raise HTTPException(status_code=404, detail="Distributed sum task not found")
    state_of = lambda job: job.state.value if isinstance(job.state, JobState) else str(job.state)
    failed = [job.job_id for job in task_jobs if state_of(job) == "failed"]
    pending = [job.job_id for job in task_jobs if state_of(job) in ("queued", "running")]
    invalid = []
    total = 0
    for job in task_jobs:
        if state_of(job) != "succeeded":
            continue
        start = job.payload.get("start")
        end = job.payload.get("end")
        expected = (start + end) * (end - start + 1) // 2
        result = job.result or {}
        if result.get("start") != start or result.get("end") != end or result.get("count") != end - start + 1 or result.get("sum") != expected:
            invalid.append(job.job_id)
        else:
            total += expected
    if failed or invalid:
        status = "failed"
    elif pending:
        status = "running" if any(state_of(job) == "running" for job in task_jobs) else "queued"
    else:
        status = "succeeded"
    return {
        "task_id": task_id,
        "status": status,
        "chunk_count": len(task_jobs),
        "succeeded_chunks": sum(state_of(job) == "succeeded" for job in task_jobs),
        "pending_chunks": len(pending),
        "failed_chunks": failed,
        "invalid_chunks": invalid,
        "total": total if status == "succeeded" else None,
    }

@app.get("/v1/jobs", response_model=list[JobRecord])
def list_jobs(
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    return list(jobs.values())


@app.get("/v1/jobs/{job_id}", response_model=JobRecord)
def get_job(
    job_id: str,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/v1/workers/{worker_id}/claim", response_model=JobRecord | None)
def worker_claim(
    worker_id: str,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    worker = authenticate_worker(worker_id, token)
    worker.last_seen_at = now()
    # Serialize in-process claims so two API threads cannot take the same queued job.
    # A multi-process deployment will still require a database-level atomic claim.
    with claim_lock:
        for recovered_job in recover_expired_jobs(jobs, now()):
            save_job(recovered_job)
        claimed = claim_job_atomic(worker, jobs)
        save_worker(worker)
        return claimed


@app.post("/v1/jobs/{job_id}/heartbeat")
def job_heartbeat(
    job_id: str,
    data: JobHeartbeat,
    worker_id: str,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    authenticate_worker(worker_id, token)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state.value != "running" or job.assigned_worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Job is not assigned to this worker")
    job.progress = data.progress
    job.updated_at = now()
    save_job(job)
    return {"ok": True, "progress": job.progress, "message": data.message}


@app.post("/v1/jobs/{job_id}/complete", response_model=JobRecord)
def complete_job(
    job_id: str,
    data: JobCompletion,
    worker_id: str,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    authenticate_worker(worker_id, token)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state.value != "running" or job.assigned_worker_id != worker_id:
        raise HTTPException(status_code=403, detail="Job is not assigned to this worker")
    if data.success:
        job.state = JobState.succeeded
        job.result = data.result
        job.error = None
        job.progress = 100
    else:
        job.result = None
        job.error = data.error or "Worker reported an unsuccessful attempt"
        if job.attempts < job.max_attempts:
            job.state = JobState.queued
            job.assigned_worker_id = None
            job.progress = 0
        else:
            job.state = JobState.failed
    job.updated_at = now()
    save_job(job)
    return job
