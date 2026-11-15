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

The initial prototype uses in-memory state. Restarting the server clears registered workers and jobs. Persistent storage, authentication, secure worker execution, quotas, and production deployment are later milestones.

## Safety

Do not expose this prototype publicly. Do not execute arbitrary user-supplied code on worker machines. Workers must be explicitly enrolled and run jobs in an isolated, resource-limited environment before production use.

## Roadmap

- [x] Initial API and in-memory scheduler prototype
- [ ] Worker authentication and signed enrollment
- [ ] Persistent database and durable queue
- [ ] Secure, sandboxed worker runtime
- [ ] GPU capability probing (CUDA/ROCm/Metal where available)
- [ ] Multi-worker scheduling and retry policy
- [ ] Storage backend and dashboard
- [ ] YTAgent integration
