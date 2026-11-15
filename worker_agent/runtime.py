from __future__ import annotations
import hashlib
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

_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {"diagnostic": _diagnostic, "echo": _echo, "sha256": _sha256, "sum_range": _sum_range}

def execute_safe_job(kind: str, payload: dict[str, Any] | None) -> tuple[bool, dict[str, Any], str | None]:
    """Run only bounded built-in operations. Payloads are never evaluated as code."""
    handler = _HANDLERS.get(kind)
    if handler is None:
        return False, {}, "Unsupported job kind: " + kind
    try:
        return True, handler(payload or {}), None
    except (TypeError, ValueError) as exc:
        return False, {}, str(exc)
