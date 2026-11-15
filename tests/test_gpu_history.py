from qingcloud.gpu_history import classify_gpu_inventory_change, gpu_inventory_snapshot
from qingcloud.models import GPUDevice


def gpu(name="NVIDIA T4", vram=16000, free=12000, utilization=10):
    return GPUDevice(
        name=name, vendor="NVIDIA", vram_mb=vram,
        free_vram_mb=free, utilization_percent=utilization,
        driver_version="555.42", runtime="CUDA",
    )


def test_inventory_snapshot_ignores_volatile_memory_and_utilization():
    first = gpu_inventory_snapshot([gpu(free=12000, utilization=10)])
    second = gpu_inventory_snapshot([gpu(free=1000, utilization=95)])
    assert first == second
    assert classify_gpu_inventory_change(first, second) is None


def test_classifies_initial_add_remove_and_changed_inventory():
    empty = []
    t4 = gpu_inventory_snapshot([gpu()])
    a10 = gpu_inventory_snapshot([gpu("NVIDIA A10", 23000)])
    assert classify_gpu_inventory_change(empty, t4) == "initial_gpu_inventory"
    assert classify_gpu_inventory_change(t4, t4 + a10) == "gpu_added"
    assert classify_gpu_inventory_change(t4 + a10, t4) == "gpu_removed"
    assert classify_gpu_inventory_change(t4, a10) == "gpu_inventory_changed"
    assert classify_gpu_inventory_change(t4, empty) == "all_gpus_removed"
