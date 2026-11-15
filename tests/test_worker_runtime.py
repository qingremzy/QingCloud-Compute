from worker_agent.runtime import execute_safe_job

def test_diagnostic_returns_machine_inventory(monkeypatch):
    monkeypatch.setattr("worker_agent.agent.discover_worker", lambda: {"hostname":"node-1", "os":"Linux", "cpu_model":"Test CPU", "cpu_cores":4, "ram_mb":8192, "gpus":[], "labels":{"agent_version":"0.1.0"}})
    ok, result, error = execute_safe_job("diagnostic", {})
    assert ok is True
    assert result["hostname"] == "node-1"
    assert result["ram_mb"] == 8192
    assert error is None

def test_echo_is_bounded():
    assert execute_safe_job("echo", {"text":"hello"}) == (True, {"echo":"hello"}, None)
    ok, result, error = execute_safe_job("echo", {"text":"x" * 2001})
    assert ok is False and result == {} and "2000" in error

def test_sha256_returns_expected_digest():
    ok, result, error = execute_safe_job("sha256", {"text":"qingcloud"})
    assert ok is True
    assert result["hex_digest"] == "94fd83a0c6138c27ed82ca06f8b05128da8c8bad73be7388d9e98775f6d00312"
    assert error is None

def test_unknown_job_kind_is_rejected():
    assert execute_safe_job("shell", {"command":"rm -rf /"}) == (False, {}, "Unsupported job kind: shell")


def test_sum_range_returns_bounded_partial_sum():
    assert execute_safe_job("sum_range", {"start": 1, "end": 100}) == (
        True, {"start": 1, "end": 100, "count": 100, "sum": 5050}, None
    )


def test_sum_range_rejects_oversized_or_invalid_chunks():
    assert execute_safe_job("sum_range", {"start": 1, "end": 10001})[0] is False
    assert execute_safe_job("sum_range", {"start": 5, "end": 4})[0] is False
    assert execute_safe_job("sum_range", {"start": True, "end": 4})[0] is False
