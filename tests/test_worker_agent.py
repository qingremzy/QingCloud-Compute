from worker_agent.agent import check_job_resources, discover_worker, detect_gpus, load_or_enroll, process_job_batch, resource_snapshot
from worker_agent.runtime import execute_safe_job

def test_discovery_reports_valid_cpu_and_ram(monkeypatch):
    monkeypatch.setattr("worker_agent.agent.memory_mb", lambda: 8192)
    monkeypatch.setattr("worker_agent.agent.detect_gpus", lambda: [])
    monkeypatch.setattr("worker_agent.agent.platform.node", lambda: "test-node")
    data = discover_worker()
    assert data["name"] == "test-node"
    assert data["cpu_cores"] >= 1
    assert data["ram_mb"] == 8192
    assert data["gpus"] == []

def test_nvidia_detection_parses_multiple_devices(monkeypatch):
    monkeypatch.setattr("worker_agent.agent._run",
        lambda command, timeout=8: "NVIDIA A10, 23028\nNVIDIA T4, 15360" if command[0] == "nvidia-smi" else "")
    devices = detect_gpus()
    assert len(devices) == 2
    assert devices[0]["vendor"] == "NVIDIA"
    assert devices[0]["vram_mb"] == 23028
    assert devices[1]["runtime"] == "CUDA"

def test_unparseable_gpu_output_does_not_create_fake_gpu(monkeypatch):
    monkeypatch.setattr("worker_agent.agent._run", lambda command, timeout=8: "unparseable")
    assert detect_gpus() == []



def test_worker_batch_processes_until_queue_empty(monkeypatch):
    outcomes = iter([True, True, False])
    calls = []

    def fake_claim(base_url, config):
        calls.append((base_url, config["worker_id"]))
        return next(outcomes)

    monkeypatch.setattr("worker_agent.agent.claim_one_job", fake_claim)
    processed = process_job_batch("http://localhost:8000", {"worker_id": "w-1"}, 5)
    assert processed == 2
    assert len(calls) == 3


def test_worker_batch_enforces_safe_job_limit(monkeypatch):
    import pytest
    with pytest.raises(ValueError):
        process_job_batch("http://localhost:8000", {"worker_id": "w-1"}, 17)



def test_nvidia_detection_reports_live_memory_utilization_and_driver(monkeypatch):
    output = "NVIDIA RTX 4090, 24564, 18000, 35, 555.42, 8.9"
    monkeypatch.setattr("worker_agent.agent._run",
        lambda command, timeout=8: output if command[0] == "nvidia-smi" else "")
    gpu = detect_gpus()[0]
    assert gpu["vram_mb"] == 24564
    assert gpu["free_vram_mb"] == 18000
    assert gpu["utilization_percent"] == 35.0
    assert gpu["driver_version"] == "555.42"
    assert gpu["compute_capability"] == "8.9"


def test_nvidia_detection_handles_unknown_optional_telemetry(monkeypatch):
    output = "NVIDIA GPU, 8192, [N/A], [N/A], 550.1, [N/A]"
    monkeypatch.setattr("worker_agent.agent._run",
        lambda command, timeout=8: output if command[0] == "nvidia-smi" else "")
    gpu = detect_gpus()[0]
    assert gpu["vram_mb"] == 8192
    assert "free_vram_mb" not in gpu
    assert "utilization_percent" not in gpu


def test_resource_snapshot_reports_cpu_ram_and_gpu_metrics(monkeypatch):
    class Memory:
        total = 8 * 1024 * 1024 * 1024
        used = 3 * 1024 * 1024 * 1024
        available = 5 * 1024 * 1024 * 1024
        percent = 37.5
    monkeypatch.setattr("worker_agent.agent.psutil.virtual_memory", lambda: Memory())
    monkeypatch.setattr("worker_agent.agent.psutil.cpu_percent", lambda interval=0.1: 23.4)
    monkeypatch.setattr("worker_agent.agent.detect_gpus", lambda: [
        {"device_index": 0, "name": "Test GPU", "vram_mb": 8192,
         "free_vram_mb": 4096, "utilization_percent": 55.0}
    ])
    data = resource_snapshot()
    assert data["cpu_percent"] == 23.4
    assert data["ram_total_mb"] == 8192
    assert data["ram_used_mb"] == 3072
    assert data["ram_available_mb"] == 5120
    assert data["ram_percent"] == 37.5
    assert data["gpus"][0]["utilization_percent"] == 55.0



def test_local_resource_preflight_accepts_job_within_capacity():
    snapshot = {
        "cpu_cores": 8,
        "ram_available_mb": 8192,
        "gpus": [{"device_index": 0, "free_vram_mb": 12000}],
    }
    job = {"required_cpu_cores": 4, "required_ram_mb": 4096, "required_gpu_vram_mb": 8000}
    assert check_job_resources(job, snapshot) is None


def test_local_resource_preflight_rejects_insufficient_cpu_ram_and_gpu():
    snapshot = {
        "cpu_cores": 2,
        "ram_available_mb": 1024,
        "gpus": [{"device_index": 0, "free_vram_mb": 2048}],
    }
    assert "CPU capacity" in check_job_resources({"required_cpu_cores": 4}, snapshot)
    assert "available RAM" in check_job_resources({"required_ram_mb": 4096}, snapshot)
    assert "GPU" in check_job_resources({"required_gpu_vram_mb": 4096}, snapshot)


def test_local_resource_preflight_uses_live_snapshot_by_default(monkeypatch):
    monkeypatch.setattr("worker_agent.agent.resource_snapshot", lambda: {
        "cpu_cores": 4, "ram_available_mb": 4096, "gpus": []
    })
    assert check_job_resources({"required_cpu_cores": 2, "required_ram_mb": 2048}) is None



def test_worker_can_enroll_with_one_time_invitation_token(monkeypatch, tmp_path):
    captured = {}
    def fake_request(base_url, endpoint, method, payload=None, headers=None):
        captured.update({"base_url": base_url, "endpoint": endpoint, "method": method, "headers": headers})
        return {"worker": {"worker_id": "worker-123"}, "access_token": "worker-access"}
    monkeypatch.setattr("worker_agent.agent.request_json", fake_request)
    config_path = tmp_path / "worker.json"
    config = load_or_enroll("https://compute.example.com", config_path, invitation_token="one-time-invite")
    assert captured["headers"] == {"X-QingCloud-Invitation-Token": "one-time-invite"}
    assert config == {"worker_id": "worker-123", "access_token": "worker-access"}
    assert config_path.exists()


def test_worker_enrollment_prefers_invitation_over_shared_enrollment_secret(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr("worker_agent.agent.request_json", lambda *args, **kwargs: (
        captured.update({"headers": kwargs.get("headers") or args[-1]}) or
        {"worker": {"worker_id": "worker-456"}, "access_token": "worker-access"}
    ))
    load_or_enroll(
        "https://compute.example.com",
        tmp_path / "worker.json",
        enrollment_token="shared-enrollment",
        invitation_token="one-time-invite",
    )
    assert captured["headers"] == {"X-QingCloud-Invitation-Token": "one-time-invite"}



def test_gpu_preflight_checks_the_coordinator_assigned_physical_device():
    snapshot = {
        "cpu_cores": 8,
        "ram_available_mb": 8192,
        "gpus": [
            {"device_index": 0, "free_vram_mb": 12000},
            {"device_index": 1, "free_vram_mb": 2048},
        ],
    }
    job = {"required_gpu_vram_mb": 4096, "assigned_gpu_index": 1}
    assert "assigned GPU" in check_job_resources(job, snapshot)


def test_gpu_preflight_accepts_the_assigned_device_when_it_has_headroom():
    snapshot = {
        "cpu_cores": 8,
        "ram_available_mb": 8192,
        "gpus": [{"device_index": 1, "free_vram_mb": 12000}],
    }
    job = {"required_gpu_vram_mb": 4096, "assigned_gpu_index": 1}
    assert check_job_resources(job, snapshot) is None


def test_worker_advertises_cuda_execution_only_when_torch_cuda_is_available(monkeypatch):
    monkeypatch.setattr("worker_agent.agent.cuda_torch_available", lambda: True)
    monkeypatch.setattr("worker_agent.agent.detect_gpus", lambda: [])
    info = discover_worker()
    assert info["labels"]["gpu_execution"] == "cuda_torch"


def test_gpu_matrix_runtime_fails_closed_without_torch(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "torch", None)
    success, result, error = execute_safe_job("gpu_matrix_multiply", {"matrix_size": 64})
    assert success is False
    assert result == {}
    assert "PyTorch CUDA" in error
