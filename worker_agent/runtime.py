from __future__ import annotations
import hashlib
import time
from typing import Any, Callable


def _diagnostic(payload: dict[str, Any]) -> dict[str, Any]:
    from worker_agent.agent import discover_worker
    info = discover_worker()
    return {"hostname": info["hostname"], "os": info["os"], "cpu_model": info["cpu_model"], "cpu_cores": info["cpu_cores"], "ram_mb": info["ram_mb"], "gpus": info["gpus"], "agent_version": info["labels"]["agent_version"]}


def _echo(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("text", "")
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("echo requires text of at most 2000 characters")
    return {"echo": value}


def _sha256(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("text", "")
    if not isinstance(value, str) or len(value) > 10000:
        raise ValueError("sha256 requires text of at most 10000 characters")
    return {"algorithm": "sha256", "hex_digest": hashlib.sha256(value.encode("utf-8")).hexdigest()}


def _sum_range(payload: dict[str, Any]) -> dict[str, Any]:
    start = payload.get("start")
    end = payload.get("end")
    if type(start) is not int or type(end) is not int:
        raise ValueError("sum_range requires integer start and end values")
    if end < start:
        raise ValueError("sum_range end must be greater than or equal to start")
    if end - start + 1 > 10000:
        raise ValueError("sum_range chunk cannot exceed 10000 integers")
    return {"start": start, "end": end, "count": end - start + 1, "sum": (start + end) * (end - start + 1) // 2}


def _gpu_matrix_multiply(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded CUDA proof-of-execution. No submitted code or model files are loaded."""
    try:
        import torch
    except ImportError as exc:
        raise ValueError("GPU task requires a worker environment with PyTorch CUDA installed") from exc
    if not torch.cuda.is_available():
        raise ValueError("PyTorch is installed but CUDA is not available on this worker")
    size = payload.get("matrix_size", 1024)
    device_index = payload.get("device_index", 0)
    if type(size) is not int or size < 32 or size > 2048:
        raise ValueError("matrix_size must be an integer between 32 and 2048")
    if type(device_index) is not int or device_index < 0 or device_index >= torch.cuda.device_count():
        raise ValueError("assigned CUDA device index is not available")
    device = torch.device("cuda:" + str(device_index))
    torch.cuda.set_device(device)
    torch.manual_seed(7)
    started = time.perf_counter()
    left = torch.rand((size, size), device=device)
    right = torch.rand((size, size), device=device)
    output = torch.matmul(left, right)
    torch.cuda.synchronize(device)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "backend": "pytorch-cuda",
        "device_index": device_index,
        "device_name": torch.cuda.get_device_name(device_index),
        "matrix_size": size,
        "elapsed_ms": elapsed_ms,
        "checksum": round(float(output.sum().item()), 6),
    }


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "diagnostic": _diagnostic,
    "echo": _echo,
    "sha256": _sha256,
    "sum_range": _sum_range,
    "gpu_matrix_multiply": _gpu_matrix_multiply,
}


def execute_safe_job(kind: str, payload: dict[str, Any] | None) -> tuple[bool, dict[str, Any], str | None]:
    """Run only bounded built-in operations. Payloads are never evaluated as code."""
    handler = _HANDLERS.get(kind)
    if handler is None:
        return False, {}, "Unsupported job kind: " + kind
    try:
        return True, handler(payload or {}), None
    except (TypeError, ValueError, RuntimeError) as exc:
        return False, {}, str(exc)
