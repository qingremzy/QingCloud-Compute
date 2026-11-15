from datetime import datetime, timedelta, timezone

from qingcloud.gpu_health import assess_worker_gpu_health
from qingcloud.models import GPUDevice, WorkerRecord, WorkerState


def make_worker(**kwargs):
    base = {
        "worker_id": "worker-1",
        "name": "gpu-worker",
        "cpu_cores": 4,
        "ram_mb": 8192,
        "gpus": [GPUDevice(
            name="NVIDIA T4", vendor="NVIDIA", vram_mb=16000,
            free_vram_mb=1000, utilization_percent=95,
        )],
    }
    base.update(kwargs)
    return WorkerRecord(**base)


def test_gpu_health_flags_low_free_vram_and_high_utilization():
    worker = make_worker()
    report = assess_worker_gpu_health(worker, worker.capabilities_updated_at)
    assert report["status"] == "healthy"
    assert "low_free_vram" in report["gpus"][0]["flags"]
    assert "high_utilization" in report["gpus"][0]["flags"]
    assert len(report["alerts"]) == 2


def test_gpu_health_marks_stale_capability_report():
    now = datetime.now(timezone.utc)
    worker = make_worker(capabilities_updated_at=now - timedelta(seconds=121))
    report = assess_worker_gpu_health(worker, now)
    assert report["status"] == "stale"
    assert "telemetry_not_current" in report["gpus"][0]["flags"]
    assert report["alerts"]


def test_gpu_health_marks_offline_worker_and_keeps_telemetry_untrusted():
    worker = make_worker(state=WorkerState.offline)
    report = assess_worker_gpu_health(worker, worker.capabilities_updated_at)
    assert report["status"] == "offline"
    assert "telemetry_not_current" in report["gpus"][0]["flags"]


def test_gpu_health_reports_missing_gpu_and_unknown_telemetry():
    worker = make_worker(gpus=[])
    report = assess_worker_gpu_health(worker, worker.capabilities_updated_at)
    assert report["status"] == "no_gpu"
    assert report["gpu_count"] == 0
    worker = make_worker(gpus=[GPUDevice(name="AMD GPU", vendor="AMD")])
    report = assess_worker_gpu_health(worker, worker.capabilities_updated_at)
    assert "free_vram_unknown" in report["gpus"][0]["flags"]
    assert "utilization_unknown" in report["gpus"][0]["flags"]
