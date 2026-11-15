from qingcloud.models import GPUDevice, JobCreate, JobRecord, WorkerRecord
from qingcloud.scheduler import claim_next_job, worker_can_run


def make_worker(vram=0, labels=None):
    return WorkerRecord(
        worker_id="worker-1",
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
