# QingCloud Compute

**A $0-first distributed compute control plane.**

QingCloud Compute coordinates machines that voluntarily contribute compute resources. It does not manufacture GPU hardware or merge separate GPUs into one physical device. It discovers workers, records their capabilities, schedules compatible jobs, and tracks results.

## Current prototype

- Register workers and report CPU, RAM, and GPU inventory.
- Submit compute jobs and let compatible workers claim them.
- Authenticate workers with an enrollment secret and individual worker tokens.
- Persist workers, hashed worker tokens, and jobs in SQLite.
- Keep GPU memory accounting per worker. Aggregate VRAM is a network inventory figure, not one unified memory pool.

## Run locally

Python 3.11+

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export QCLOUD_ENROLLMENT_TOKEN="replace-with-a-long-random-secret"
uvicorn qingcloud.main:app --reload
```

On Windows PowerShell, set the secret with `$env:QCLOUD_ENROLLMENT_TOKEN="replace-with-a-long-random-secret"`.

The SQLite database defaults to `qingcloud.sqlite3` in the working directory. Set `QCLOUD_DB_PATH` to change its location. Back up this file to preserve workers, jobs, and token hashes.

Open the interactive API docs at `http://127.0.0.1:8000/docs`.

## Worker enrollment

1. Send `POST /v1/workers/register` with the `X-QingCloud-Enrollment-Token` header.
2. Save the returned `access_token` securely. It is shown only at enrollment.
3. Send the token as `X-QingCloud-Worker-Token` for worker heartbeat, claim, and job-result calls.

The server stores only a SHA-256 hash of each worker token. If a worker loses its token, it must be re-enrolled with a new identity. The enrollment secret must remain private and must never be committed to Git.

## Worker agent (discovery-only v0.1)

The worker_agent package detects CPU/RAM and available GPU inventory, enrolls a machine, saves its worker token locally, and sends authenticated heartbeats. It deliberately does not claim or execute jobs. For a remote server, use HTTPS.

```bash
export QCLOUD_URL="https://your-qingcloud-host"
export QCLOUD_ENROLLMENT_TOKEN="your-enrollment-secret"
python -m worker_agent.agent --once
```

On Windows PowerShell, set QCLOUD_URL and QCLOUD_ENROLLMENT_TOKEN using the $env: syntax. The default token file is ~/.qingcloud/worker.json and is created with owner-only permissions where supported. Keep it private. Subsequent runs reuse the saved token. Set QCLOUD_WORKER_CONFIG to choose another config path.

NVIDIA inventory is read through nvidia-smi. AMD ROCm product-name detection is best-effort and reports VRAM as unknown (0 MB) until reliable platform-specific parsing is added.

## Safety and limitations

This remains a development prototype. Job submission and read-only endpoints are not yet authenticated, and the in-process scheduler is not safe for multiple concurrent server processes. Do not expose it publicly. Do not execute arbitrary user-supplied code on worker machines. Workers need a sandbox, resource limits, transport security, and stronger API authorization before production use.

SQLite persistence is local to the machine running the control plane. It does not provide 100 GB of shared storage or GPU capacity. Actual GPU resources must come from real enrolled machines.

## Roadmap

- [x] Initial API and in-memory scheduler prototype
- [x] Worker enrollment secret and per-worker API tokens
- [x] SQLite persistence for workers, token hashes, and jobs
- [ ] Authentication for job submission and read endpoints
- [ ] Durable queue with concurrency-safe claiming and retry recovery
- [x] Worker agent for hardware discovery and authenticated heartbeat (no job execution)
- [ ] Secure, sandboxed worker runtime
- [ ] GPU capability probing (CUDA/ROCm/Metal where available)
- [ ] Multi-worker scheduling and retry policy
- [ ] Shared storage backend and dashboard
- [ ] YTAgent integration
