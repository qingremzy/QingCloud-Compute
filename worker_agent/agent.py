from __future__ import annotations
import argparse, csv, json, os, platform, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any
import psutil

from worker_agent.runtime import execute_safe_job

def _run(command: list[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""

def memory_mb() -> int:
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
        except (ValueError, OSError):
            pass
    return 0

def detect_gpus() -> list[dict[str, Any]]:
    """Discover NVIDIA telemetry, with best-effort AMD device-name fallback."""
    devices: list[dict[str, Any]] = []
    output = _run([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,memory.free,utilization.gpu,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ])
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) < 2:
            continue
        try:
            indexed = len(row) >= 3 and row[0].strip().isdigit()
            offset = 1 if indexed else 0
            device: dict[str, Any] = {
                "name": row[offset].strip(),
                "vendor": "NVIDIA",
                "vram_mb": max(0, int(float(row[offset + 1].strip()))),
                "runtime": "CUDA",
            }
            if indexed:
                device["device_index"] = int(row[0].strip())
            if len(row) > offset + 2 and row[offset + 2].strip().lower() not in ("", "[n/a]", "n/a"):
                device["free_vram_mb"] = max(0, int(float(row[offset + 2].strip())))
            if len(row) > offset + 3 and row[offset + 3].strip().lower() not in ("", "[n/a]", "n/a"):
                device["utilization_percent"] = min(100.0, max(0.0, float(row[offset + 3].strip())))
            if len(row) > offset + 4 and row[offset + 4].strip():
                device["driver_version"] = row[offset + 4].strip()
            if len(row) > offset + 5 and row[offset + 5].strip().lower() not in ("", "[n/a]", "n/a"):
                device["compute_capability"] = row[offset + 5].strip()
            devices.append(device)
        except (ValueError, IndexError):
            continue
    if devices:
        return devices
    # ROCm product-name output is detected best-effort. VRAM and utilization
    # remain unknown until stable cross-version telemetry parsing is available.
    rocm = _run(["rocm-smi", "--showproductname", "--json"])
    if rocm:
        try:
            data = json.loads(rocm)
            def walk(value: Any, path: str = "") -> None:
                if isinstance(value, dict):
                    for key, item in value.items():
                        walk(item, path + " " + key)
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        walk(item, path + " " + str(index))
                elif isinstance(value, str) and ("card" in path.lower()) and ("product" in path.lower() or "name" in path.lower()):
                    devices.append({
                        "name": value, "vendor": "AMD", "vram_mb": 0,
                        "free_vram_mb": None, "runtime": "ROCm",
                    })
            walk(data)
        except (json.JSONDecodeError, TypeError):
            pass
    return devices

def resource_snapshot() -> dict[str, Any]:
    """Sample host CPU, RAM and currently detectable GPU pressure."""
    memory = psutil.virtual_memory()
    return {
        "cpu_cores": int(psutil.cpu_count(logical=True) or os.cpu_count() or 1),
        "cpu_percent": round(float(psutil.cpu_percent(interval=0.1)), 1),
        "ram_total_mb": int(memory.total / (1024 * 1024)),
        "ram_used_mb": int(memory.used / (1024 * 1024)),
        "ram_available_mb": int(memory.available / (1024 * 1024)),
        "ram_percent": round(float(memory.percent), 1),
        "gpus": [
            {key: gpu[key] for key in ("device_index", "name", "utilization_percent", "vram_mb", "free_vram_mb") if key in gpu}
            for gpu in detect_gpus()
        ],
    }


def check_job_resources(job: dict[str, Any], snapshot: dict[str, Any] | None = None) -> str | None:
    """Reject a claimed job when this host's local snapshot cannot meet its declared requirements."""
    current = snapshot if snapshot is not None else resource_snapshot()
    required_cpu = int(job.get("required_cpu_cores", 0) or 0)
    required_ram = int(job.get("required_ram_mb", 0) or 0)
    required_gpu = int(job.get("required_gpu_vram_mb", 0) or 0)
    if required_cpu > int(current.get("cpu_cores", 0)):
        return f"Local CPU capacity is below the job requirement ({required_cpu} cores required)."
    if required_ram > int(current.get("ram_available_mb", 0)):
        return f"Local available RAM is below the job requirement ({required_ram} MB required)."
    if required_gpu:
        devices = current.get("gpus") or []
        if not any(int(device.get("free_vram_mb") or 0) >= required_gpu for device in devices):
            return f"No locally detected GPU has {required_gpu} MB of reported free VRAM."
    return None


def discover_worker(name: str | None = None) -> dict[str, Any]:
    return {
        "name": name or platform.node() or "qingcloud-worker",
        "hostname": platform.node() or None,
        "os": platform.system() + " " + platform.release(),
        "cpu_model": platform.processor() or None,
        "cpu_cores": os.cpu_count() or 1,
        "ram_mb": memory_mb(),
        "gpus": detect_gpus(),
        "labels": {"agent": "qingcloud-worker-agent", "agent_version": "0.1.0"},
    }

def request_json(base_url: str, path: str, method: str = "GET", payload: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None, timeout: int = 15) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(base_url.rstrip("/") + path, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError("QingCloud API returned HTTP " + str(exc.code) + ": " + detail) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Cannot reach QingCloud control plane: " + str(exc.reason)) from exc

def save_config(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass

def load_or_enroll(base_url: str, config_path: Path, enrollment_token: str) -> dict[str, str]:
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config.get("worker_id") and config.get("access_token"):
            return config
    response = request_json(base_url, "/v1/workers/register", "POST", discover_worker(),
        {"X-QingCloud-Enrollment-Token": enrollment_token})
    config = {"worker_id": response["worker"]["worker_id"], "access_token": response["access_token"]}
    save_config(config_path, config)
    return config

def send_capabilities(base_url: str, config: dict[str, str]) -> dict[str, Any]:
    return request_json(base_url, "/v1/workers/" + config["worker_id"] + "/capabilities", "PUT",
        payload=discover_worker(),
        headers={"X-QingCloud-Worker-Token": config["access_token"]})

def send_telemetry(base_url: str, config: dict[str, str]) -> dict[str, Any]:
    return request_json(base_url, "/v1/workers/" + config["worker_id"] + "/telemetry", "POST",
        payload=resource_snapshot(),
        headers={"X-QingCloud-Worker-Token": config["access_token"]})


def send_heartbeat(base_url: str, config: dict[str, str]) -> dict[str, Any]:
    return request_json(base_url, "/v1/workers/" + config["worker_id"] + "/heartbeat", "POST",
        headers={"X-QingCloud-Worker-Token": config["access_token"]})


def claim_one_job(base_url: str, config: dict[str, str]) -> bool:
    headers = {"X-QingCloud-Worker-Token": config["access_token"]}
    worker_id = config["worker_id"]
    job = request_json(base_url, "/v1/workers/" + worker_id + "/claim", "POST", headers=headers)
    if not job:
        return False
    job_id = job["job_id"]
    attempt = int(job["attempts"])
    request_json(base_url, "/v1/jobs/" + job_id + "/heartbeat?worker_id=" + worker_id + "&attempt=" + str(attempt),
        "POST", payload={"progress": 10, "message": "Validating approved built-in task"}, headers=headers)
    resource_error = check_job_resources(job)
    if resource_error:
        request_json(base_url, "/v1/jobs/" + job_id + "/complete?worker_id=" + worker_id + "&attempt=" + str(attempt),
            "POST", payload={"success": False, "result": {}, "error": resource_error}, headers=headers)
        print(json.dumps({"job_id": job_id, "job_kind": job.get("kind"), "success": False, "error": resource_error}))
        return False
    success, result, error = execute_safe_job(job.get("kind", ""), job.get("payload") or {})
    request_json(base_url, "/v1/jobs/" + job_id + "/complete?worker_id=" + worker_id + "&attempt=" + str(attempt),
        "POST", payload={"success": success, "result": result, "error": error}, headers=headers)
    print(json.dumps({"job_id": job_id, "job_kind": job.get("kind"), "success": success, "error": error}))
    return True

def process_job_batch(base_url: str, config: dict[str, str], max_jobs: int = 1) -> int:
    """Process at most max_jobs queued built-in jobs in one heartbeat cycle."""
    if max_jobs < 1 or max_jobs > 16:
        raise ValueError("max_jobs must be between 1 and 16")
    completed = 0
    for _ in range(max_jobs):
        if not claim_one_job(base_url, config):
            break
        completed += 1
    return completed


def run_agent(base_url: str, config_path: Path, interval: int, once: bool = False, enable_jobs: bool = False, max_jobs_per_cycle: int = 1) -> None:
    if not base_url.startswith("https://") and not base_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise RuntimeError("Use HTTPS for remote servers. HTTP is allowed only on localhost.")
    enrollment_token = os.getenv("QCLOUD_ENROLLMENT_TOKEN", "")
    if not config_path.exists() and not enrollment_token:
        raise RuntimeError("Set QCLOUD_ENROLLMENT_TOKEN for first enrollment.")
    config = load_or_enroll(base_url, config_path, enrollment_token)
    while True:
        inventory = send_capabilities(base_url, config)
        telemetry = send_telemetry(base_url, config)
        result = send_heartbeat(base_url, config)
        print(json.dumps({
            "heartbeat": "ok",
            "worker_id": config["worker_id"],
            "gpu_count": len(inventory.get("gpus", [])),
            "cpu_percent": telemetry.get("cpu_percent"),
            "ram_percent": telemetry.get("ram_percent"),
            "last_seen_at": result.get("last_seen_at"),
        }))
        if enable_jobs:
            processed = process_job_batch(base_url, config, max_jobs_per_cycle)
            print(json.dumps({"jobs_processed_this_cycle": processed}))
        if once:
            return
        time.sleep(max(15, interval))

def main() -> None:
    parser = argparse.ArgumentParser(description="QingCloud worker agent with opt-in bounded built-in job execution.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--enable-jobs", action="store_true", help="Opt in to bounded built-in jobs")
    parser.add_argument("--max-jobs-per-cycle", type=int, default=1, help="Maximum jobs to process per heartbeat cycle (1-16)")
    args = parser.parse_args()
    base_url = os.getenv("QCLOUD_URL", "http://127.0.0.1:8000")
    config_path = Path(os.getenv("QCLOUD_WORKER_CONFIG", str(Path.home() / ".qingcloud" / "worker.json")))
    try:
        run_agent(base_url, config_path, args.interval, args.once, args.enable_jobs, args.max_jobs_per_cycle)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print("QingCloud worker error: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
