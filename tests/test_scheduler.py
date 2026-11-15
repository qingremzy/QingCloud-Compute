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
    worker = WorkerRecord(approval_status=WorkerApprovalStatus.approved, 
        worker_id="worker-free-vram",
        name="gpu-worker",
        gpus=[GPUDevice(name="GPU", vram_mb=16000, free_vram_mb=3000)],
    )
    assert not worker_can_run(worker, make_job(vram=8000))
    assert worker_can_run(worker, make_job(vram=2000))


def test_gpu_job_falls_back_to_total_when_free_vram_is_unknown():
    worker = WorkerRecord(approval_status=WorkerApprovalStatus.approved, 
        worker_id="worker-unknown-free",
        name="gpu-worker",
        gpus=[GPUDevice(name="GPU", vram_mb=16000)],
    )
    assert worker_can_run(worker, make_job(vram=12000))


from qingcloud.scheduler import rank_workers_for_job, worker_suitability_score


def test_intelligent_scheduler_prefers_reliable_worker_for_cpu_job():
    job = make_job()
    newer = WorkerRecord(approval_status=WorkerApprovalStatus.approved, worker_id="worker-new", name="new", cpu_cores=8, ram_mb=8192)
    trusted = WorkerRecord(approval_status=WorkerApprovalStatus.approved, worker_id="worker-trusted", name="trusted", cpu_cores=8, ram_mb=8192)
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
    busy = WorkerRecord(approval_status=WorkerApprovalStatus.approved, 
        worker_id="worker-busy", name="busy",
        gpus=[GPUDevice(name="GPU", vram_mb=24000, free_vram_mb=9000, utilization_percent=95)],
    )
    available = WorkerRecord(approval_status=WorkerApprovalStatus.approved, 
        worker_id="worker-available", name="available",
        gpus=[GPUDevice(name="GPU", vram_mb=24000, free_vram_mb=18000, utilization_percent=15)],
    )
    ranked = rank_workers_for_job(job, [busy, available])
    assert ranked[0]["worker_id"] == "worker-available"


def test_intelligent_scheduler_accounts_for_running_job_load():
    job = make_job()
    a = WorkerRecord(approval_status=WorkerApprovalStatus.approved, worker_id="worker-a", name="a")
    b = WorkerRecord(approval_status=WorkerApprovalStatus.approved, worker_id="worker-b", name="b")
    ranked = rank_workers_for_job(job, [a, b], running_job_counts={"worker-a": 3, "worker-b": 0})
    assert ranked[0]["worker_id"] == "worker-b"


def test_intelligent_scheduler_excludes_workers_with_stale_heartbeats():
    from datetime import datetime, timedelta, timezone
    job = make_job()
    stale = WorkerRecord(approval_status=WorkerApprovalStatus.approved, 
        worker_id="worker-stale", name="stale",
        last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    fresh = WorkerRecord(approval_status=WorkerApprovalStatus.approved, worker_id="worker-fresh", name="fresh")
    ranked = rank_workers_for_job(job, [stale, fresh])
    assert [item["worker_id"] for item in ranked] == ["worker-fresh"]


def test_expired_gpu_job_releases_device_reservation():
    job = make_job(vram=6000)
    job.state = JobState.running
    job.assigned_worker_id = "worker-old"
    job.assigned_gpu_index = 1
    job.reserved_gpu_vram_mb = 6000
    job.attempts = 1
    job.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    recovered = recover_expired_jobs({job.job_id: job}, lease_seconds=60)
    assert recovered == [job]
    assert job.state == JobState.queued
    assert job.assigned_worker_id is None
    assert job.assigned_gpu_index is None
    assert job.reserved_gpu_vram_mb == 0


def test_resource_aware_scheduler_prefers_provider_with_more_live_cpu_and_ram_headroom():
    from datetime import datetime, timezone
    job = make_job()
    pressured = WorkerRecord(approval_status=WorkerApprovalStatus.approved,
        worker_id="worker-pressured", name="pressured", cpu_cores=8, ram_mb=16384)
    available = WorkerRecord(approval_status=WorkerApprovalStatus.approved,
        worker_id="worker-available", name="available", cpu_cores=8, ram_mb=16384)
    stamp = datetime.now(timezone.utc).isoformat()
    ranked = rank_workers_for_job(
        job, [pressured, available],
        telemetry_by_worker={
            "worker-pressured": {"created_at": stamp, "cpu_percent": 96,
                "ram_total_mb": 16384, "ram_available_mb": 512, "ram_percent": 97},
            "worker-available": {"created_at": stamp, "cpu_percent": 18,
                "ram_total_mb": 16384, "ram_available_mb": 12288, "ram_percent": 25},
        },
    )
    assert ranked[0]["worker_id"] == "worker-available"


def test_resource_aware_scheduler_ignores_stale_resource_samples():
    from datetime import datetime, timedelta, timezone
    job = make_job()
    a = WorkerRecord(approval_status=WorkerApprovalStatus.approved, worker_id="worker-a", name="a")
    b = WorkerRecord(approval_status=WorkerApprovalStatus.approved, worker_id="worker-b", name="b")
    baseline = rank_workers_for_job(job, [a, b])
    stale_stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with_stale = rank_workers_for_job(job, [a, b], telemetry_by_worker={
        "worker-a": {"created_at": stale_stamp, "cpu_percent": 99,
            "ram_total_mb": 10000, "ram_available_mb": 1, "ram_percent": 99},
    })
    assert with_stale == baseline


def test_job_cpu_and_ram_requirements_filter_workers():
    from qingcloud.models import JobRecord
    capable = WorkerRecord(approval_status=WorkerApprovalStatus.approved,
        worker_id="worker-capable", name="capable", cpu_cores=8, ram_mb=16384)
    small_cpu = WorkerRecord(approval_status=WorkerApprovalStatus.approved,
        worker_id="worker-small-cpu", name="small-cpu", cpu_cores=2, ram_mb=16384)
    small_ram = WorkerRecord(approval_status=WorkerApprovalStatus.approved,
        worker_id="worker-small-ram", name="small-ram", cpu_cores=8, ram_mb=2048)
    job = JobRecord(job_id="resource-job", kind="test", required_cpu_cores=4, required_ram_mb=4096)
    assert worker_can_run(capable, job)
    assert not worker_can_run(small_cpu, job)
    assert not worker_can_run(small_ram, job)


def test_fresh_available_ram_is_a_hard_job_requirement():
    from datetime import datetime, timezone
    from qingcloud.models import JobRecord
    worker = WorkerRecord(approval_status=WorkerApprovalStatus.approved,
        worker_id="worker-live-ram", name="live-ram", cpu_cores=8, ram_mb=16384)
    job = JobRecord(job_id="ram-job", kind="test", required_ram_mb=4096)
    stamp = datetime.now(timezone.utc).isoformat()
    assert not worker_can_run(worker, job, {"created_at": stamp, "ram_available_mb": 2048})
    assert worker_can_run(worker, job, {"created_at": stamp, "ram_available_mb": 8192})


def test_stale_ram_telemetry_does_not_override_reported_capacity():
    from datetime import datetime, timedelta, timezone
    from qingcloud.models import JobRecord
    worker = WorkerRecord(approval_status=WorkerApprovalStatus.approved,
        worker_id="worker-stale-ram", name="stale-ram", cpu_cores=8, ram_mb=8192)
    job = JobRecord(job_id="ram-job-stale", kind="test", required_ram_mb=4096)
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert worker_can_run(worker, job, {"created_at": stamp, "ram_available_mb": 512})
