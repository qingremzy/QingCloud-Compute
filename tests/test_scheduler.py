from qingcloud.models import WorkerApprovalStatus
from qingcloud.models import GPUDevice, JobCreate, JobRecord, WorkerRecord
from qingcloud.scheduler import claim_next_job, worker_can_run


def make_worker(vram=0, labels=None):
    return WorkerRecord(approval_status=WorkerApprovalStatus.approved,         worker_id="worker-1",
        name="test-worker",
        gpus=[GPUDevice(name="test-gpu", vram_mb=vram)] if vram else [],
        labels=labels or {},
    )


def make_job(vram=0, labels=None):
    return JobRecord(
        job_id="job-1",
        kind="test",
        required_gpu_vram_mb=vram,
        required_labels=labels or {},
    )


def test_cpu_job_can_run_without_gpu():
    assert worker_can_run(make_worker(), make_job())


def test_gpu_requirement_is_checked_per_device():
    assert not worker_can_run(make_worker(vram=8000), make_job(vram=12000))
    assert worker_can_run(make_worker(vram=16000), make_job(vram=12000))


def test_required_labels_must_match():
    assert worker_can_run(make_worker(labels={"runtime": "cuda"}), make_job(labels={"runtime": "cuda"}))
    assert not worker_can_run(make_worker(labels={"runtime": "cpu"}), make_job(labels={"runtime": "cuda"}))


def test_scheduler_claims_compatible_job():
    worker = make_worker(vram=16000)
    job = make_job(vram=12000)
    claimed = claim_next_job(worker, {job.job_id: job})
    assert claimed is job
    assert claimed.assigned_worker_id == worker.worker_id
    assert claimed.attempts == 1


from datetime import datetime, timedelta, timezone
from qingcloud.models import JobState
from qingcloud.scheduler import recover_expired_jobs


def test_expired_job_is_requeued_when_attempts_remain():
    job = make_job()
    job.state = JobState.running
    job.assigned_worker_id = "worker-old"
    job.attempts = 1
    job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    recovered = recover_expired_jobs({job.job_id: job}, lease_seconds=60)
    assert recovered == [job]
    assert job.state == JobState.queued
    assert job.assigned_worker_id is None
    assert job.progress == 0


def test_expired_job_fails_after_max_attempts():
    job = make_job()
    job.max_attempts = 1
    job.state = JobState.running
    job.assigned_worker_id = "worker-old"
    job.attempts = 1
    job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    recover_expired_jobs({job.job_id: job}, lease_seconds=60)
    assert job.state == JobState.failed
    assert job.assigned_worker_id is None


def test_live_job_lease_is_not_recovered():
    job = make_job()
    job.state = JobState.running
    job.assigned_worker_id = "worker-live"
    job.updated_at = datetime.now(timezone.utc)
    assert recover_expired_jobs({job.job_id: job}, lease_seconds=180) == []
    assert job.state == JobState.running


def test_gpu_job_uses_free_vram_instead_of_total_capacity():
    worker = WorkerRecord(
        worker_id="worker-free-vram",
        name="gpu-worker",
        gpus=[GPUDevice(name="GPU", vram_mb=16000, free_vram_mb=3000)],
    )
    assert not worker_can_run(worker, make_job(vram=8000))
    assert worker_can_run(worker, make_job(vram=2000))


def test_gpu_job_falls_back_to_total_when_free_vram_is_unknown():
    worker = WorkerRecord(
        worker_id="worker-unknown-free",
        name="gpu-worker",
        gpus=[GPUDevice(name="GPU", vram_mb=16000)],
    )
    assert worker_can_run(worker, make_job(vram=12000))


from qingcloud.scheduler import rank_workers_for_job, worker_suitability_score


def test_intelligent_scheduler_prefers_reliable_worker_for_cpu_job():
    job = make_job()
    newer = WorkerRecord(worker_id="worker-new", name="new", cpu_cores=8, ram_mb=8192)
    trusted = WorkerRecord(worker_id="worker-trusted", name="trusted", cpu_cores=8, ram_mb=8192)
    ranked = rank_workers_for_job(
        job, [newer, trusted],
        reliability_by_worker={
            "worker-new": {"score": 50.0},
            "worker-trusted": {"score": 95.0},
        },
    )
    assert ranked[0]["worker_id"] == "worker-trusted"


def test_intelligent_scheduler_accounts_for_live_gpu_headroom_and_utilization():
    job = make_job(vram=8000)
    busy = WorkerRecord(
        worker_id="worker-busy", name="busy",
        gpus=[GPUDevice(name="GPU", vram_mb=24000, free_vram_mb=9000, utilization_percent=95)],
    )
    available = WorkerRecord(
        worker_id="worker-available", name="available",
        gpus=[GPUDevice(name="GPU", vram_mb=24000, free_vram_mb=18000, utilization_percent=15)],
    )
    ranked = rank_workers_for_job(job, [busy, available])
    assert ranked[0]["worker_id"] == "worker-available"


def test_intelligent_scheduler_accounts_for_running_job_load():
    job = make_job()
    a = WorkerRecord(worker_id="worker-a", name="a")
    b = WorkerRecord(worker_id="worker-b", name="b")
    ranked = rank_workers_for_job(job, [a, b], running_job_counts={"worker-a": 3, "worker-b": 0})
    assert ranked[0]["worker_id"] == "worker-b"


def test_intelligent_scheduler_excludes_workers_with_stale_heartbeats():
    from datetime import datetime, timedelta, timezone
    job = make_job()
    stale = WorkerRecord(
        worker_id="worker-stale", name="stale",
        last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    fresh = WorkerRecord(worker_id="worker-fresh", name="fresh")
    ranked = rank_workers_for_job(job, [stale, fresh])
    assert [item["worker_id"] for item in ranked] == ["worker-fresh"]
