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

## Safety and limitations

This remains a development prototype. Job submission and read-only endpoints are not yet authenticated, and the in-process scheduler is not safe for multiple concurrent server processes. Do not expose it publicly. Do not execute arbitrary user-supplied code on worker machines. Workers need a sandbox, resource limits, transport security, and stronger API authorization before production use.

SQLite persistence is local to the machine running the control plane. It does not provide 100 GB of shared storage or GPU capacity. Actual GPU resources must come from real enrolled machines.

## Roadmap

- [x] Initial API and in-memory scheduler prototype
- [x] Worker enrollment secret and per-worker API tokens
- [x] SQLite persistence for workers, token hashes, and jobs
- [ ] Authentication for job submission and read endpoints
- [ ] Durable queue with concurrency-safe claiming and retry recovery
- [ ] Secure, sandboxed worker runtime
- [ ] GPU capability probing (CUDA/ROCm/Metal where available)
- [ ] Multi-worker scheduling and retry policy
- [ ] Shared storage backend and dashboard
- [ ] YTAgent integration
