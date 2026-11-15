from __future__ import annotations

from typing import Any

from qingcloud.models import GPUDevice


def gpu_inventory_snapshot(gpus: list[GPUDevice]) -> list[dict[str, Any]]:
    """Return stable hardware identity fields, excluding volatile live telemetry."""
    fields = (
        "name", "vendor", "vram_mb", "driver_version",
        "compute_capability", "runtime",
    )
    rows = []
    for gpu in gpus:
        item = gpu.model_dump()
        rows.append({key: item.get(key) for key in fields})
    return sorted(rows, key=lambda row: (
        str(row.get("vendor") or ""),
        str(row.get("name") or ""),
        int(row.get("vram_mb") or 0),
        str(row.get("runtime") or ""),
        str(row.get("driver_version") or ""),
        str(row.get("compute_capability") or ""),
    ))


def classify_gpu_inventory_change(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> str | None:
    if before == after:
        return None
    if not before:
        return "initial_gpu_inventory"
    if not after:
        return "all_gpus_removed"
    before_ids = [(x.get("vendor"), x.get("name"), x.get("vram_mb"), x.get("runtime")) for x in before]
    after_ids = [(x.get("vendor"), x.get("name"), x.get("vram_mb"), x.get("runtime")) for x in after]
    if len(after_ids) > len(before_ids) and all(item in after_ids for item in before_ids):
        return "gpu_added"
    if len(after_ids) < len(before_ids) and all(item in before_ids for item in after_ids):
        return "gpu_removed"
    return "gpu_inventory_changed"
