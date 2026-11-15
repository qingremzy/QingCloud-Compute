from worker_agent.agent import discover_worker, detect_gpus, process_job_batch, resource_snapshot

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
