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
export QCLOUD_ADMIN_TOKEN="another-long-random-secret"
uvicorn qingcloud.main:app --reload
```

On Windows PowerShell, set both secrets with `$env:QCLOUD_ENROLLMENT_TOKEN="replace-with-a-long-random-secret"` and `$env:QCLOUD_ADMIN_TOKEN="another-long-random-secret"`. Keep both private and do not commit them.

The SQLite database defaults to `qingcloud.sqlite3` in the working directory. Set `QCLOUD_DB_PATH` to change its location. Back up this file to preserve workers, jobs, and token hashes.

Open the interactive API docs at `http://127.0.0.1:8000/docs`.

## Coordinator administration\n\nJob submission and job/task inspection require the `X-QingCloud-Admin-Token` header matching `QCLOUD_ADMIN_TOKEN`. The API fails closed with HTTP 503 if the variable is not configured and rejects missing or incorrect tokens. Keep this token only in the coordinator environment and trusted admin clients. The read-only `GET /v1/dashboard` endpoint returns worker hardware inventory, worker status and last-seen timestamps, running-job counts, safe job status summaries, distributed-task chunk counts, and the overview. It deliberately omits job payloads and result contents. Worker enrollment and worker operations continue using their separate enrollment and per-worker tokens.\n\n## Worker enrollment

1. Send `POST /v1/workers/register` with the `X-QingCloud-Enrollment-Token` header.
2. Save the returned `access_token` securely. It is shown only at enrollment.
3. Send the token as `X-QingCloud-Worker-Token` for worker heartbeat, claim, and job-result calls.

The server stores only a SHA-256 hash of each worker token. If a worker loses its token, it must be re-enrolled with a new identity. The enrollment secret must remain private and must never be committed to Git.

## Worker agent (discovery + opt-in safe jobs)

The worker_agent package detects CPU/RAM and available GPU inventory, enrolls a machine, saves its worker token locally, refreshes its hardware inventory through an authenticated endpoint, and sends authenticated heartbeats. Job execution is opt-in with --enable-jobs and is limited to bounded built-in tasks. It can process a configurable batch of up to 16 jobs per heartbeat cycle. For a remote server, use HTTPS.

```bash
export QCLOUD_URL="https://your-qingcloud-host"
export QCLOUD_ENROLLMENT_TOKEN="your-enrollment-secret"
python -m worker_agent.agent --once
# Opt in to built-in jobs:
python -m worker_agent.agent --enable-jobs --max-jobs-per-cycle 4
```

On Windows PowerShell, set QCLOUD_URL and QCLOUD_ENROLLMENT_TOKEN using the $env: syntax. The default token file is ~/.qingcloud/worker.json and is created with owner-only permissions where supported. Keep it private. Subsequent runs reuse the saved token. Set QCLOUD_WORKER_CONFIG to choose another config path.

The safe runtime supports diagnostic, echo (2,000-character limit), SHA-256 (10,000-character limit), and sum_range chunks (at most 10,000 integers per chunk). The distributed-sum API has an automated two-worker identity test that verifies distinct chunk assignment and final aggregation. This validates coordinator behavior in tests, not execution across two real physical machines. Failed attempts are requeued up to each job's max_attempts setting. A worker that stops renewing its job lease is treated as abandoned after 180 seconds, and the job is requeued or marked failed when another worker claims work. Submit `POST /v1/distributed/sum` with `start`, `end`, and optional `chunk_size` (maximum 10,000) to split an inclusive integer range into independently schedulable CPU jobs. Each job is assigned a task ID. Poll `GET /v1/distributed/sum/{task_id}` for progress and the final total. The coordinator verifies each returned chunk's boundaries, count, and arithmetic before accepting its partial sum. The whole task is limited to 1,000,000 integers and 100 chunks. Unknown job kinds are rejected. The worker never runs shell commands or evaluates submitted payloads. Job execution is disabled unless --enable-jobs is supplied. By default, the agent processes one job per heartbeat cycle. Set --max-jobs-per-cycle to a value from 1 to 16 to process a bounded batch before the next heartbeat.

NVIDIA inventory is read through nvidia-smi. AMD ROCm product-name detection is best-effort and reports VRAM as unknown (0 MB) until reliable platform-specific parsing is added.

## Safety and limitations

This remains a development prototype. Worker registration and worker operations use their dedicated tokens. Job submission and job/task inspection require the coordinator admin token. The worker listing, overview, and dashboard endpoints also require the admin token. Job claims use SQLite BEGIN IMMEDIATE transactions, preventing duplicate claims across API processes that share the same SQLite database file on one host. A multi-host coordinator still needs a networked transactional database and durable queue. Do not expose it publicly. Do not execute arbitrary user-supplied code on worker machines. Workers need a sandbox, resource limits, transport security, and stronger API authorization before production use.

SQLite persistence is local to the machine running the control plane. It does not provide 100 GB of shared storage or GPU capacity. Actual GPU resources must come from real enrolled machines.

## Roadmap

- [x] Initial API and in-memory scheduler prototype
- [x] Worker enrollment secret and per-worker API tokens
- [x] SQLite persistence for workers, token hashes, and jobs
- [x] Admin-token authentication for job submission and job/task inspection
- [x] Basic retry limits and expired-worker lease recovery\n- [x] In-process claim locking to prevent duplicate claims within one API process\n- [ ] Durable queue with database-level transactional claiming across multiple API processes
- [x] Worker agent for hardware discovery and authenticated heartbeat (no job execution)
- [x] Opt-in worker runtime for bounded built-in diagnostic jobs
- [ ] Secure, sandboxed worker runtime for third-party workloads
- [x] Initial NVIDIA GPU capability probing and authenticated inventory refresh
- [ ] Broader GPU capability probing (ROCm/Metal and richer telemetry)
- [x] Distributed CPU sum prototype: split ranges into jobs and verify/aggregate chunk results\n- [x] Multi-worker chunk assignment and aggregation covered by automated coordinator tests\n- [ ] Demonstrate real workers on separate physical machines and improve multi-worker throughput
- [ ] Shared storage backend\n- [x] Admin-authenticated network operations dashboard API
- [ ] YTAgent integration


## Browser dashboard

Open `/dashboard` on the running coordinator to use the browser control room. Enter the value configured as `QCLOUD_ADMIN_TOKEN` when prompted. The page requests the protected `/v1/dashboard` API using the admin header, keeps the token only in page memory, does not put it in the URL or browser storage, and clears it when you disconnect or close the page. It refreshes the snapshot every 15 seconds while connected. Serve the coordinator over HTTPS whenever it is accessed remotely. This UI does not make the prototype safe for public exposure.
