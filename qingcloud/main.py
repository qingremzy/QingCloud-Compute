from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from fastapi import FastAPI, HTTPException

from qingcloud.models import (
    JobCompletion, JobCreate, JobHeartbeat, JobRecord,
    WorkerRecord, WorkerRegistration,
)
from qingcloud.scheduler import claim_next_job

app = FastAPI(
    title="QingCloud Compute",
    version="0.1.0",
    description="A $0-first distributed compute control-plane prototype.",
)

workers: dict[str, WorkerRecord] = {}
jobs: dict[str, JobRecord] = {}


def now():
    return datetime.now(timezone.utc)


@app.get("/health")
def health():
    return {"status": "ok", "service": "qingcloud-compute", "version": app.version}


@app.get("/v1/overview")
def overview():
    gpu_vram_mb = sum(gpu.vram_mb for worker in workers.values() for gpu in worker.gpus)
    return {
        "workers": len(workers),
        "online_workers": sum(w.state.value == "online" for w in workers.values()),
        "jobs": len(jobs),
        "queued_jobs": sum(j.state.value == "queued" for j in jobs.values()),
        "running_jobs": sum(j.state.value == "running" for j in jobs.values()),
        "reported_gpu_vram_mb_across_network": gpu_vram_mb,
        "note": "Aggregate VRAM is inventory only; it is not one unified GPU memory pool.",
    }


@app.post("/v1/workers/register", response_model=WorkerRecord)
def register_worker(data: WorkerRegistration):
    worker_id = str(uuid4())
    record = WorkerRecord(**data.model_dump(), worker_id=worker_id)
    workers[worker_id] = record
    return record


@app.get("/v1/workers", response_model=list[WorkerRecord])
def list_workers():
    return list(workers.values())


@app.post("/v1/workers/{worker_id}/heartbeat")
def worker_heartbeat(worker_id: str):
    worker = workers.get(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    worker.last_seen_at = now()
    worker.state = "online"
    return {"ok": True, "last_seen_at": worker.last_seen_at}


@app.post("/v1/jobs", response_model=JobRecord)
def submit_job(data: JobCreate):
    job_id = str(uuid4())
    record = JobRecord(**data.model_dump(), job_id=job_id)
    jobs[job_id] = record
    return record


@app.get("/v1/jobs", response_model=list[JobRecord])
def list_jobs():
    return list(jobs.values())


@app.get("/v1/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/v1/workers/{worker_id}/claim", response_model=JobRecord | None)
def worker_claim(worker_id: str):
    worker = workers.get(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    worker.last_seen_at = now()
    job = claim_next_job(worker, jobs)
    return job


@app.post("/v1/jobs/{job_id}/heartbeat")
def job_heartbeat(job_id: str, data: JobHeartbeat):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state.value != "running":
        raise HTTPException(status_code=409, detail="Job is not running")
    job.progress = data.progress
    job.updated_at = now()
    return {"ok": True, "progress": job.progress, "message": data.message}


@app.post("/v1/jobs/{job_id}/complete", response_model=JobRecord)
def complete_job(job_id: str, data: JobCompletion):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state.value != "running":
        raise HTTPException(status_code=409, detail="Job is not running")
    job.state = "succeeded" if data.success else "failed"
    job.result = data.result if data.success else None
    job.error = data.error if not data.success else None
    job.progress = 100 if data.success else job.progress
    job.updated_at = now()
    return job
