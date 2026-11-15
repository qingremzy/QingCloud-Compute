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

_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {"diagnostic": _diagnostic, "echo": _echo, "sha256": _sha256}

def execute_safe_job(kind: str, payload: dict[str, Any] | None) -> tuple[bool, dict[str, Any], str | None]:
    """Run only bounded built-in operations. Payloads are never evaluated as code."""
    handler = _HANDLERS.get(kind)
    if handler is None:
        return False, {}, "Unsupported job kind: " + kind
    try:
        return True, handler(payload or {}), None
    except (TypeError, ValueError) as exc:
        return False, {}, str(exc)
