from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from threading import RLock
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from qingcloud.db import claim_job_atomic, init_db, load_state, save_job, save_worker, save_worker_token, record_gpu_event, list_gpu_events, record_worker_job_event, worker_reliability_stats, create_worker_invitation, redeem_worker_invitation, record_worker_approval_event, list_worker_approval_events, recover_expired_jobs_persisted, save_job_if_current_attempt, record_worker_telemetry, latest_worker_telemetry, list_worker_telemetry
from qingcloud.models import (
    DistributedSumCreate, JobCompletion, JobCreate, JobDecline, JobHeartbeat, JobRecord, JobState, WorkerTelemetry,
    WorkerEnrollmentResponse, WorkerRecord, WorkerRegistration, WorkerState, WorkerInvitationCreate, WorkerApprovalUpdate, WorkerApprovalStatus, WorkerCapacityUpdate,
)
from qingcloud.scheduler import recover_expired_jobs, reconcile_worker_presence
from qingcloud.gpu_health import assess_worker_gpu_health
from qingcloud.gpu_history import gpu_inventory_snapshot, classify_gpu_inventory_change
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
# Recover expired persisted leases immediately after restart so stale GPU
# reservations are released before the coordinator accepts new work.
recover_expired_jobs_persisted(jobs, datetime.now(timezone.utc))
# A worker cannot be assumed alive after the control plane restarts.
for _worker in workers.values():
    _worker.state = WorkerState.offline
    save_worker(_worker)


def now():
    return datetime.now(timezone.utc)


def refresh_worker_presence():
    """Persist offline transitions for workers whose heartbeat has gone stale."""
    for changed_worker in reconcile_worker_presence(workers, now()):
        save_worker(changed_worker)


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
def overview(admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token")):
    verify_admin_token(admin_token)
    refresh_worker_presence()
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


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    """Serve the static admin UI. Live data remains behind the admin-token API."""
    page = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.get("/v1/dashboard")
def dashboard(admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token")):
    """Return an admin-authenticated, read-only network operations snapshot."""
    verify_admin_token(admin_token)
    refresh_worker_presence()
    worker_rows = []
    for worker in workers.values():
        running = [job for job in jobs.values() if job.assigned_worker_id == worker.worker_id and job.state.value == "running"]
        gpu_reservations = {}
        for active_job in running:
            if active_job.assigned_gpu_index is None or active_job.reserved_gpu_vram_mb <= 0:
                continue
            index = active_job.assigned_gpu_index
            entry = gpu_reservations.setdefault(index, {"device_index": index, "reserved_vram_mb": 0, "job_ids": []})
            entry["reserved_vram_mb"] += active_job.reserved_gpu_vram_mb
            entry["job_ids"].append(active_job.job_id)
        gpu_rows = []
        for position, gpu in enumerate(worker.gpus):
            index = gpu.device_index if gpu.device_index is not None else position
            reserved = gpu_reservations.get(index, {}).get("reserved_vram_mb", 0)
            reported_free = gpu.free_vram_mb if gpu.free_vram_mb is not None else gpu.vram_mb
            gpu_rows.append({
                **gpu.model_dump(), "effective_device_index": index,
                "reserved_vram_mb": reserved,
                "scheduler_headroom_vram_mb": max(0, reported_free - reserved),
                "reservation_job_ids": gpu_reservations.get(index, {}).get("job_ids", []),
            })
        worker_rows.append({
            "worker_id": worker.worker_id, "name": worker.name, "hostname": worker.hostname,
            "os": worker.os, "state": worker.state.value,
            "approval_status": worker.approval_status.value,
            "approval_updated_at": worker.approval_updated_at,
            "approval_note": worker.approval_note,
            "max_concurrent_jobs": worker.max_concurrent_jobs, "last_seen_at": worker.last_seen_at,
            "capabilities_updated_at": worker.capabilities_updated_at,
            "gpu_health": assess_worker_gpu_health(worker, now()),
            "resource_telemetry": latest_worker_telemetry(worker.worker_id),
            "reliability": worker_reliability_stats(worker.worker_id),
            "cpu_cores": worker.cpu_cores, "ram_mb": worker.ram_mb,
            "gpus": gpu_rows, "running_jobs": len(running),
            "reserved_gpu_vram_mb_total": sum(item["reserved_vram_mb"] for item in gpu_reservations.values()),
            "reserved_cpu_cores_total": sum(item.reserved_cpu_cores for item in running),
            "reserved_ram_mb_total": sum(item.reserved_ram_mb for item in running),
            "gpu_reservations": list(gpu_reservations.values()),
        })
    job_rows = [{
        "job_id": job.job_id, "task_id": job.task_id, "kind": job.kind, "state": job.state.value,
        "assigned_worker_id": job.assigned_worker_id, "assigned_gpu_index": job.assigned_gpu_index,
        "reserved_gpu_vram_mb": job.reserved_gpu_vram_mb, "required_gpu_vram_mb": job.required_gpu_vram_mb,
        "required_cpu_cores": job.required_cpu_cores, "required_ram_mb": job.required_ram_mb,
        "reserved_cpu_cores": job.reserved_cpu_cores, "reserved_ram_mb": job.reserved_ram_mb,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts, "progress": job.progress,
        "created_at": job.created_at, "updated_at": job.updated_at, "error": job.error,
    } for job in jobs.values()]
    task_groups = {}
    for job in jobs.values():
        if not job.task_id:
            continue
        group = task_groups.setdefault(job.task_id, {
            "task_id": job.task_id, "kind": "distributed_sum", "chunks": 0,
            "succeeded": 0, "running": 0, "queued": 0, "failed": 0,
        })
        group["chunks"] += 1
        group[job.state.value] += 1
    return {
        "overview": overview(admin_token), "workers": worker_rows, "jobs": job_rows,
        "tasks": list(task_groups.values()), "generated_at": now(),
        "note": "Reported hardware is inventory only. Aggregate GPU VRAM is not a unified memory pool and does not guarantee available capacity.",
    }


@app.post("/v1/invitations")
def create_invitation(
    data: WorkerInvitationCreate,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    """Create a one-time provider invitation. Plaintext is returned only once."""
    verify_admin_token(admin_token)
    invitation = issue_worker_token()
    expires_at = now() + timedelta(hours=data.expires_in_hours)
    create_worker_invitation(hash_worker_token(invitation), expires_at)
    return {"invitation_token": invitation, "expires_at": expires_at, "expires_in_hours": data.expires_in_hours,
            "note": "Store this token securely. It is shown only once and can enroll one worker."}


@app.post("/v1/workers/register", response_model=WorkerEnrollmentResponse)
def register_worker(
    data: WorkerRegistration,
    enrollment_token: str | None = Header(default=None, alias="X-QingCloud-Enrollment-Token"),
    invitation_token: str | None = Header(default=None, alias="X-QingCloud-Invitation-Token"),
):
    if invitation_token:
        if not redeem_worker_invitation(hash_worker_token(invitation_token), now()):
            raise HTTPException(status_code=401, detail="Invalid, expired, or already-used invitation token")
    else:
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
def list_workers(admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token")):
    verify_admin_token(admin_token)
    refresh_worker_presence()
    return list(workers.values())


@app.get("/v1/providers")
def provider_registry(
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    """Return a concise live registry of enrolled compute providers."""
    verify_admin_token(admin_token)
    refresh_worker_presence()
    current = now()
    entries = []
    for worker in workers.values():
        age = max(0.0, (current - worker.last_seen_at).total_seconds())
        running = sum(1 for job in jobs.values() if job.state == JobState.running and job.assigned_worker_id == worker.worker_id)
        entries.append({
            "worker_id": worker.worker_id,
            "name": worker.name,
            "hostname": worker.hostname,
            "state": worker.state.value,
            "approval_status": worker.approval_status.value,
            "approval_updated_at": worker.approval_updated_at,
            "approval_note": worker.approval_note,
            "max_concurrent_jobs": worker.max_concurrent_jobs,
            "running_jobs": running,
            "available_slots": max(0, worker.max_concurrent_jobs - running),
            "last_seen_at": worker.last_seen_at,
            "heartbeat_age_seconds": round(age, 1),
            "cpu_cores": worker.cpu_cores,
            "ram_mb": worker.ram_mb,
            "gpu_count": len(worker.gpus),
            "labels": worker.labels,
        })
    return {
        "providers": entries,
        "total": len(entries),
        "online": sum(item["state"] == "online" for item in entries),
        "offline": sum(item["state"] == "offline" for item in entries),
        "heartbeat_timeout_seconds": 120,
        "note": "Providers are enrolled machines reporting authenticated heartbeats. Offline status is inferred after 120 seconds without a heartbeat. This registry does not discover or scan arbitrary internet-connected devices.",
    }



@app.put("/v1/workers/{worker_id}/approval")
def update_worker_approval(
    worker_id: str,
    data: WorkerApprovalUpdate,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    """Approve, reject, or return an enrolled provider to pending review."""
    verify_admin_token(admin_token)
    worker = workers.get(worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    previous = worker.approval_status.value
    if previous != data.status.value:
        worker.approval_status = data.status
        worker.approval_updated_at = now()
        worker.approval_note = data.note
        save_worker(worker)
        record_worker_approval_event(worker_id, previous, data.status.value, data.note, worker.approval_updated_at)
    return {"worker_id": worker_id, "approval_status": worker.approval_status.value, "approval_updated_at": worker.approval_updated_at, "approval_note": worker.approval_note}


@app.get("/v1/workers/{worker_id}/approval-history")
def worker_approval_history(
    worker_id: str,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    if worker_id not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")
    events = list_worker_approval_events(worker_id)
    return {"worker_id": worker_id, "events": events, "total": len(events)}



@app.put("/v1/workers/{worker_id}/capacity")
def update_worker_capacity(
    worker_id: str,
    data: WorkerCapacityUpdate,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    """Set an administrator-controlled upper bound on simultaneous jobs."""
    verify_admin_token(admin_token)
    worker = workers.get(worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    worker.max_concurrent_jobs = data.max_concurrent_jobs
    save_worker(worker)
    running = sum(1 for job in jobs.values() if job.state == JobState.running and job.assigned_worker_id == worker_id)
    return {
        "worker_id": worker_id,
        "max_concurrent_jobs": worker.max_concurrent_jobs,
        "running_jobs": running,
        "available_slots": max(0, worker.max_concurrent_jobs - running),
    }


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


@app.post("/v1/workers/{worker_id}/telemetry")
def submit_worker_telemetry(
    worker_id: str,
    data: WorkerTelemetry,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    """Accept an authenticated resource snapshot from an enrolled worker."""
    worker = authenticate_worker(worker_id, token)
    snapshot = data.model_dump()
    if snapshot["ram_used_mb"] + snapshot["ram_available_mb"] > snapshot["ram_total_mb"] + 256:
        raise HTTPException(status_code=422, detail="RAM used plus available exceeds reported total")
    worker.last_seen_at = now()
    worker.state = WorkerState.online
    save_worker(worker)
    return record_worker_telemetry(worker_id, snapshot, worker.last_seen_at)


@app.get("/v1/workers/{worker_id}/telemetry")
def get_worker_telemetry(
    worker_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    if worker_id not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")
    samples = list_worker_telemetry(worker_id, limit)
    return {"worker_id": worker_id, "samples": samples, "total": len(samples)}


@app.put("/v1/workers/{worker_id}/capabilities", response_model=WorkerRecord)
def update_worker_capabilities(
    worker_id: str,
    data: WorkerRegistration,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    """Refresh a worker's self-reported hardware inventory using its own token."""
    worker = authenticate_worker(worker_id, token)
    normalized = WorkerRegistration.model_validate(data.model_dump())
    before_gpus = gpu_inventory_snapshot(worker.gpus)
    after_gpus = gpu_inventory_snapshot(normalized.gpus)
    event_type = classify_gpu_inventory_change(before_gpus, after_gpus)
    for field in WorkerRegistration.model_fields:
        setattr(worker, field, getattr(normalized, field))
    worker.last_seen_at = now()
    worker.capabilities_updated_at = worker.last_seen_at
    worker.state = WorkerState.online
    save_worker(worker)
    if event_type:
        record_gpu_event(worker_id, event_type, before_gpus, after_gpus, worker.capabilities_updated_at)
    return worker


@app.get("/v1/workers/{worker_id}/reliability")
def worker_reliability(
    worker_id: str,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    if worker_id not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"worker_id": worker_id, **worker_reliability_stats(worker_id),
            "note": "Score uses only completed job-attempt outcomes recorded by the coordinator. Claims, queued jobs, running jobs, and abandoned attempts are not counted as successes or failures."}


@app.get("/v1/workers/{worker_id}/gpu-history")
def worker_gpu_history(
    worker_id: str,
    limit: int = 50,
    admin_token: str | None = Header(default=None, alias="X-QingCloud-Admin-Token"),
):
    verify_admin_token(admin_token)
    if worker_id not in workers:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {
        "worker_id": worker_id,
        "events": list_gpu_events(worker_id, limit),
        "note": "History records stable GPU hardware inventory changes. Free VRAM and utilization are volatile live telemetry and are not logged as hardware changes.",
    }


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
    worker.state = WorkerState.online
    save_worker(worker)
    refresh_worker_presence()
    # Serialize in-process claims so two API threads cannot take the same queued job.
    # A multi-process deployment will still require a database-level atomic claim.
    with claim_lock:
        recover_expired_jobs_persisted(jobs, now())
        claimed = claim_job_atomic(worker, jobs, workers)
        save_worker(worker)
        return claimed


@app.post("/v1/jobs/{job_id}/heartbeat")
def job_heartbeat(
    job_id: str,
    data: JobHeartbeat,
    worker_id: str,
    attempt: int = Query(..., ge=1, description="Fencing token returned by the claim endpoint"),
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    authenticate_worker(worker_id, token)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.running or job.assigned_worker_id != worker_id or job.attempts != attempt:
        raise HTTPException(status_code=409, detail="Worker lease is stale or no longer assigned")
    updated = job.model_copy(deep=True)
    updated.progress = data.progress
    updated.updated_at = now()
    if not save_job_if_current_attempt(updated, worker_id, attempt):
        raise HTTPException(status_code=409, detail="Worker lease expired or was superseded")
    jobs[job_id] = updated
    return {"ok": True, "progress": updated.progress, "message": data.message}


@app.post("/v1/jobs/{job_id}/decline", response_model=JobRecord)
def decline_job(
    job_id: str,
    data: JobDecline,
    worker_id: str,
    attempt: int = Query(..., ge=1, description="Fencing token returned by the claim endpoint"),
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    """Release a claimed job that this worker cannot safely execute, without recording a failed attempt."""
    authenticate_worker(worker_id, token)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.running or job.assigned_worker_id != worker_id or job.attempts != attempt:
        raise HTTPException(status_code=409, detail="Worker lease is stale or no longer assigned")
    updated = job.model_copy(deep=True)
    updated.state = JobState.queued
    updated.assigned_worker_id = None
    updated.assigned_gpu_index = None
    updated.reserved_gpu_vram_mb = 0
    updated.reserved_cpu_cores = 0
    updated.reserved_ram_mb = 0
    updated.declined_attempts += 1
    updated.progress = 0
    updated.result = None
    updated.error = "Worker declined job: " + data.reason
    updated.updated_at = now()
    if not save_job_if_current_attempt(updated, worker_id, attempt):
        raise HTTPException(status_code=409, detail="Worker lease expired or was superseded")
    jobs[job_id] = updated
    return updated


@app.post("/v1/jobs/{job_id}/complete", response_model=JobRecord)
def complete_job(
    job_id: str,
    data: JobCompletion,
    worker_id: str,
    attempt: int = Query(..., ge=1, description="Fencing token returned by the claim endpoint"),
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
):
    authenticate_worker(worker_id, token)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state != JobState.running or job.assigned_worker_id != worker_id or job.attempts != attempt:
        raise HTTPException(status_code=409, detail="Worker lease is stale or no longer assigned")
    updated = job.model_copy(deep=True)
    if data.success:
        updated.state = JobState.succeeded
        updated.result = data.result
        updated.error = None
        updated.progress = 100
    else:
        updated.result = None
        updated.error = data.error or "Worker reported an unsuccessful attempt"
        if updated.attempts - updated.declined_attempts < updated.max_attempts:
            updated.state = JobState.queued
            updated.assigned_worker_id = None
            updated.assigned_gpu_index = None
            updated.progress = 0
        else:
            updated.state = JobState.failed
        updated.reserved_gpu_vram_mb = 0
    if data.success:
        updated.reserved_gpu_vram_mb = 0
    updated.reserved_cpu_cores = 0
    updated.reserved_ram_mb = 0
    updated.updated_at = now()
    event_type = "succeeded" if data.success else "failed"
    if not save_job_if_current_attempt(updated, worker_id, attempt, event_type):
        raise HTTPException(status_code=409, detail="Worker lease expired or was superseded")
    jobs[job_id] = updated
    return updated
