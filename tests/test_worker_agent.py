from worker_agent.agent import discover_worker, detect_gpus

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
