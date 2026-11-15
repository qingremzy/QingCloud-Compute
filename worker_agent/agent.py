from __future__ import annotations
import argparse, json, os, platform, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
from typing import Any

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
    devices = []
    output = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 1)]
        if len(parts) == 2:
            try:
                devices.append({"name": parts[0], "vendor": "NVIDIA", "vram_mb": max(0, int(float(parts[1]))), "runtime": "CUDA"})
            except ValueError:
                continue
    if devices:
        return devices
    # ROCm product-name output is detected best-effort. VRAM remains unknown (0)
    # until a reliable cross-version parser is available.
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
                    devices.append({"name": value, "vendor": "AMD", "vram_mb": 0, "runtime": "ROCm"})
            walk(data)
        except (json.JSONDecodeError, TypeError):
            pass
    return devices

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
    request_json(base_url, "/v1/jobs/" + job_id + "/heartbeat?worker_id=" + worker_id,
        "POST", payload={"progress": 10, "message": "Validating approved built-in task"}, headers=headers)
    success, result, error = execute_safe_job(job.get("kind", ""), job.get("payload") or {})
    request_json(base_url, "/v1/jobs/" + job_id + "/complete?worker_id=" + worker_id,
        "POST", payload={"success": success, "result": result, "error": error}, headers=headers)
    print(json.dumps({"job_id": job_id, "job_kind": job.get("kind"), "success": success, "error": error}))
    return True

def run_agent(base_url: str, config_path: Path, interval: int, once: bool = False, enable_jobs: bool = False) -> None:
    if not base_url.startswith("https://") and not base_url.startswith(("http://127.0.0.1", "http://localhost")):
        raise RuntimeError("Use HTTPS for remote servers. HTTP is allowed only on localhost.")
    enrollment_token = os.getenv("QCLOUD_ENROLLMENT_TOKEN", "")
    if not config_path.exists() and not enrollment_token:
        raise RuntimeError("Set QCLOUD_ENROLLMENT_TOKEN for first enrollment.")
    config = load_or_enroll(base_url, config_path, enrollment_token)
    while True:
        inventory = send_capabilities(base_url, config)
        result = send_heartbeat(base_url, config)
        print(json.dumps({
            "heartbeat": "ok",
            "worker_id": config["worker_id"],
            "gpu_count": len(inventory.get("gpus", [])),
            "last_seen_at": result.get("last_seen_at"),
        }))
        if enable_jobs:
            claim_one_job(base_url, config)
        if once:
            return
        time.sleep(max(15, interval))

def main() -> None:
    parser = argparse.ArgumentParser(description="QingCloud discovery-only worker agent. It does not execute jobs.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--enable-jobs", action="store_true", help="Opt in to bounded built-in diagnostic, echo, and SHA-256 jobs")
    args = parser.parse_args()
    base_url = os.getenv("QCLOUD_URL", "http://127.0.0.1:8000")
    config_path = Path(os.getenv("QCLOUD_WORKER_CONFIG", str(Path.home() / ".qingcloud" / "worker.json")))
    try:
        run_agent(base_url, config_path, args.interval, args.once, args.enable_jobs)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print("QingCloud worker error: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
