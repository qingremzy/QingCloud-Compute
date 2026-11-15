from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from qingcloud.models import WorkerRecord


def assess_worker_gpu_health(
    worker: WorkerRecord,
    current_time: datetime | None = None,
    stale_after_seconds: int = 120,
    high_utilization_percent: float = 90.0,
    low_free_ratio: float = 0.10,
) -> dict[str, Any]:
    """Summarize reported GPU telemetry. Thresholds are operational heuristics."""
    current_time = current_time or datetime.now(timezone.utc)
    updated = worker.capabilities_updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (current_time - updated).total_seconds())
    stale = age_seconds > stale_after_seconds
    worker_state = worker.state.value if hasattr(worker.state, "value") else str(worker.state)

    if worker_state != "online":
        status = "offline"
    elif stale:
        status = "stale"
    elif not worker.gpus:
        status = "no_gpu"
    else:
        status = "healthy"

    gpu_rows = []
    alerts = []
    if worker_state != "online":
        alerts.append("Worker is offline. GPU telemetry cannot be treated as current.")
    elif stale:
        alerts.append("GPU capability report is stale. Waiting for a worker refresh.")

    for index, gpu in enumerate(worker.gpus):
        flags = []
        if gpu.free_vram_mb is None:
            flags.append("free_vram_unknown")
        elif gpu.vram_mb > 0 and gpu.free_vram_mb / gpu.vram_mb <= low_free_ratio:
            flags.append("low_free_vram")
        if gpu.utilization_percent is None:
            flags.append("utilization_unknown")
        elif gpu.utilization_percent >= high_utilization_percent:
            flags.append("high_utilization")
        if stale or worker_state != "online":
            flags.append("telemetry_not_current")
        gpu_rows.append({
            "index": index,
            "name": gpu.name,
            "vendor": gpu.vendor,
            "vram_mb": gpu.vram_mb,
            "free_vram_mb": gpu.free_vram_mb,
            "utilization_percent": gpu.utilization_percent,
            "driver_version": gpu.driver_version,
            "compute_capability": gpu.compute_capability,
            "flags": flags,
        })
        for flag in flags:
            if flag not in ("free_vram_unknown", "utilization_unknown", "telemetry_not_current"):
                alerts.append(f"GPU {index} ({gpu.name}): {flag}")

    return {
        "status": status,
        "capabilities_updated_at": worker.capabilities_updated_at,
        "telemetry_age_seconds": round(age_seconds, 1),
        "stale_after_seconds": stale_after_seconds,
        "gpu_count": len(worker.gpus),
        "gpus": gpu_rows,
        "alerts": alerts,
        "thresholds": {
            "high_utilization_percent": high_utilization_percent,
            "low_free_vram_ratio": low_free_ratio,
            "note": "Heuristic warning thresholds, not hardware failure diagnoses.",
        },
    }
