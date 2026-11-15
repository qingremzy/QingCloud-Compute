from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qingcloud.models import JobRecord, JobState, WorkerRecord, WorkerApprovalStatus


def worker_can_run(worker: WorkerRecord, job: JobRecord, resource_telemetry: dict | None = None) -> bool:
    if worker.state.value != "online":
        return False
    if worker.approval_status != WorkerApprovalStatus.approved:
        return False
    if job.required_cpu_cores and worker.cpu_cores < job.required_cpu_cores:
        return False
    if job.required_ram_mb:
        if worker.ram_mb < job.required_ram_mb:
            return False
        telemetry = resource_telemetry or {}
        sample_time = telemetry.get("created_at")
        if sample_time:
            try:
                stamp = datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - stamp <= timedelta(seconds=120):
                    if int(telemetry.get("ram_available_mb", 0)) < job.required_ram_mb:
                        return False
            except (TypeError, ValueError):
                pass
    if job.required_gpu_vram_mb:
        available = max((gpu.free_vram_mb if gpu.free_vram_mb is not None else gpu.vram_mb for gpu in worker.gpus), default=0)
        if available < job.required_gpu_vram_mb:
            return False
    return all(worker.labels.get(key) == value for key, value in job.required_labels.items())


def worker_suitability_score(
    worker: WorkerRecord,
    job: JobRecord,
    running_jobs: int = 0,
    reliability: dict | None = None,
    resource_telemetry: dict | None = None,
) -> float:
    """Rank compatible workers using reported headroom, load and observed outcomes.

    This is a transparent heuristic, not a performance benchmark. Unknown reliability
    receives a neutral prior and unknown GPU telemetry never becomes fabricated data.
    """
    reliability = reliability or {}
    reliability_score = reliability.get("score")
    reliability_component = float(reliability_score) if reliability_score is not None else 50.0
    load_component = 100.0 / (1 + max(0, running_jobs))
    cpu_component = min(100.0, worker.cpu_cores * 10.0)
    ram_component = min(100.0, worker.ram_mb / 1024.0 * 5.0)
    telemetry = resource_telemetry or {}
    sample_time = telemetry.get("created_at")
    telemetry_fresh = False
    if sample_time:
        try:
            stamp = datetime.fromisoformat(sample_time.replace("Z", "+00:00"))
            telemetry_fresh = datetime.now(timezone.utc) - stamp <= timedelta(seconds=120)
        except (TypeError, ValueError):
            telemetry_fresh = False
    if telemetry_fresh:
        cpu_pressure = float(telemetry.get("cpu_percent", 0.0))
        ram_total = max(1, int(telemetry.get("ram_total_mb", worker.ram_mb or 1)))
        ram_available = max(0, int(telemetry.get("ram_available_mb", 0)))
        live_cpu_score = 100.0 - min(100.0, max(0.0, cpu_pressure))
        live_ram_score = min(100.0, ram_available / ram_total * 100.0)
        live_resource_score = live_cpu_score * 0.4 + live_ram_score * 0.6
    else:
        live_resource_score = 50.0

    if job.required_gpu_vram_mb:
        candidates = [
            gpu for gpu in worker.gpus
            if (gpu.free_vram_mb if gpu.free_vram_mb is not None else gpu.vram_mb) >= job.required_gpu_vram_mb
        ]
        gpu = max(candidates, key=lambda item: item.free_vram_mb if item.free_vram_mb is not None else item.vram_mb)
        available = gpu.free_vram_mb if gpu.free_vram_mb is not None else gpu.vram_mb
        headroom = min(100.0, available / max(job.required_gpu_vram_mb, 1) * 25.0)
        utilization = gpu.utilization_percent
        gpu_component = headroom if utilization is None else headroom * 0.7 + (100.0 - utilization) * 0.3
        return round(gpu_component * 0.35 + reliability_component * 0.30 + load_component * 0.15 + live_resource_score * 0.15 + cpu_component * 0.05, 3)

    capacity_component = cpu_component * 0.6 + ram_component * 0.4
    return round(reliability_component * 0.30 + load_component * 0.25 + capacity_component * 0.15 + live_resource_score * 0.30, 3)


def rank_workers_for_job(
    job: JobRecord,
    workers: list[WorkerRecord],
    running_job_counts: dict[str, int] | None = None,
    reliability_by_worker: dict[str, dict] | None = None,
    telemetry_by_worker: dict[str, dict] | None = None,
) -> list[dict]:
    running_job_counts = running_job_counts or {}
    reliability_by_worker = reliability_by_worker or {}
    telemetry_by_worker = telemetry_by_worker or {}
    ranked = []
    current_time = datetime.now(timezone.utc)
    for worker in workers:
        if current_time - worker.last_seen_at > timedelta(seconds=120):
            continue
        if not worker_can_run(worker, job, telemetry_by_worker.get(worker.worker_id)):
            continue
        score = worker_suitability_score(
            worker, job, running_job_counts.get(worker.worker_id, 0),
            reliability_by_worker.get(worker.worker_id),
            telemetry_by_worker.get(worker.worker_id),
        )
        ranked.append({"worker_id": worker.worker_id, "score": score})
    return sorted(ranked, key=lambda item: (-item["score"], item["worker_id"]))


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
        job.assigned_gpu_index = None
        job.reserved_gpu_vram_mb = 0
        job.reserved_cpu_cores = 0
        job.reserved_ram_mb = 0
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


def reconcile_worker_presence(workers: dict[str, WorkerRecord], current_time: datetime | None = None, stale_after_seconds: int = 120) -> list[WorkerRecord]:
    """Mark workers offline when authenticated heartbeat freshness expires."""
    current_time = current_time or datetime.now(timezone.utc)
    changed = []
    for worker in workers.values():
        stale = current_time - worker.last_seen_at > timedelta(seconds=stale_after_seconds)
        if stale and worker.state == worker.state.__class__.online:
            worker.state = worker.state.__class__.offline
            changed.append(worker)
    return changed
