# QingCloud Compute

**A $0-first distributed compute control plane.**

QingCloud Compute coordinates machines that voluntarily contribute compute resources. It does not manufacture GPU hardware or merge separate GPUs into one physical device. It discovers workers, records their capabilities, schedules compatible jobs, and tracks results.

## Phase 1: control-plane prototype

- Register workers and report CPU, RAM, and GPU inventory.
- Submit compute jobs.
- Let workers claim compatible jobs.
- Report job progress and results.
- Keep GPU memory accounting per worker. Aggregate VRAM is a network inventory figure, not one unified memory pool.

## Run locally

Python 3.11+

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn qingcloud.main:app --reload
```

Open the interactive API docs at `http://127.0.0.1:8000/docs`.

## First API workflow

1. `POST /v1/workers/register` — register a worker.
2. `POST /v1/jobs` — submit a job.
3. `POST /v1/workers/{worker_id}/claim` — worker claims a compatible job.
4. `POST /v1/jobs/{job_id}/heartbeat` — report progress.
5. `POST /v1/jobs/{job_id}/complete` — report completion or failure.

The prototype uses in-memory state. Restarting the server clears registered workers, token hashes, and jobs. Set a strong `QCLOUD_ENROLLMENT_TOKEN` environment variable before enrolling workers. Registration requires that secret in the `X-QingCloud-Enrollment-Token` header. Each enrolled worker receives a one-time `access_token`. Store it securely and send it as `X-QingCloud-Worker-Token` for worker heartbeat, claim, and job-result calls. The server stores only a SHA-256 hash of the worker token. If the server restarts, workers must be enrolled again. Job submission and read-only endpoints are still unauthenticated, so this remains a development prototype and must not be exposed publicly.

## Safety

Do not expose this prototype publicly. Do not execute arbitrary user-supplied code on worker machines. Workers must be explicitly enrolled and run jobs in an isolated, resource-limited environment before production use.

## Roadmap

- [x] Initial API and in-memory scheduler prototype
- [x] Basic worker enrollment secret and per-worker API tokens\n- [ ] Persistent token and worker storage
- [ ] Persistent database and durable queue
- [ ] Secure, sandboxed worker runtime
- [ ] GPU capability probing (CUDA/ROCm/Metal where available)
- [ ] Multi-worker scheduling and retry policy
- [ ] Storage backend and dashboard
- [ ] YTAgent integration
