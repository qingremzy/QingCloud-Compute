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

## Coordinator administration\n\nJob submission and job/task inspection require the `X-QingCloud-Admin-Token` header matching `QCLOUD_ADMIN_TOKEN`. The API fails closed with HTTP 503 if the variable is not configured and rejects missing or incorrect tokens. Keep this token only in the coordinator environment and trusted admin clients. The read-only `GET /v1/dashboard` endpoint returns worker hardware inventory, worker status and last-seen timestamps, capability-refresh timestamps, GPU health assessments, running-job counts, safe job status summaries, distributed-task chunk counts, and the overview. It deliberately omits job payloads and result contents. Worker enrollment and worker operations continue using their separate enrollment and per-worker tokens.\n\n## Worker enrollment

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

NVIDIA inventory is read through nvidia-smi and includes total/free VRAM, utilization, driver version, and compute capability when the driver reports them. GPU jobs use reported free VRAM when available, falling back to total VRAM only when free-memory telemetry is unknown. AMD ROCm product-name detection remains best-effort and reports VRAM/utilization as unknown until reliable platform-specific parsing is added.

## GPU provider health monitoring

The admin-only `GET /v1/workers/{worker_id}/gpu-history` endpoint returns persisted GPU hardware inventory changes, including initial inventory, additions, removals, and other hardware/driver changes. The history intentionally excludes fluctuating free VRAM and utilization readings. The dashboard API reports each worker GPU inventory with capability-report age and status (`healthy`, `stale`, `offline`, or `no_gpu`). It flags free VRAM at or below 10% of reported total and utilization at or above 90%. These are operational warning heuristics, not proof of hardware failure. Missing AMD telemetry is shown as unknown rather than guessed. Capability freshness is based on the last authenticated hardware refresh, not a live out-of-band GPU probe.

## Intelligent worker scheduling

When an enrolled worker requests a job, the coordinator ranks currently online, recently seen compatible workers before assigning it. GPU jobs consider per-device free VRAM (or total VRAM only when free-memory telemetry is unknown), reported GPU utilization, reliability, and active job load. CPU jobs consider reliability, active job load, CPU cores, and RAM. Required labels and minimum GPU VRAM remain hard eligibility filters. Workers with no outcome history receive a neutral reliability prior rather than a zero score. The ranking is a transparent heuristic, not a benchmark or guarantee. A worker that is not currently top-ranked waits for another claim cycle. Only workers reporting online and recently seen are included in the ranking.

## Provider presence and registry

The admin-only `GET /v1/providers` endpoint lists enrolled providers with heartbeat age, hardware summary, labels, and online/offline counts. The coordinator automatically marks a provider offline after 120 seconds without an authenticated heartbeat, persists that state, and marks it online again when the provider sends a fresh heartbeat. This is heartbeat-based presence detection, not internet-wide device discovery or automatic enrollment of unknown machines. A volunteer must run the QingCloud worker agent and possess the private enrollment secret to join.

## Worker reliability scoring

The admin-only `GET /v1/workers/{worker_id}/reliability` endpoint reports a coordinator-derived reliability estimate. Each authenticated job completion records an outcome against the worker that actually completed that attempt, including failed attempts that are later retried. Claims and unfinished/abandoned attempts do not count as outcomes. Workers with no completed outcomes are `unrated`, not penalized. The score uses a small Laplace prior to avoid treating one success as perfect reliability: (successes + 1) / (completed outcomes + 2). Samples below 10 outcomes are marked `limited_data`, and 10 or more are `established`. The dashboard shows the score and sample size. This is an operational signal, not a guarantee of future performance or a public reputation ranking.


## Controlled provider invitations

Administrators can issue a one-time provider invitation with the admin-authenticated `POST /v1/invitations` endpoint. The request accepts `expires_in_hours` from 1 to 168 (default 24). The response contains a random invitation token and expiry timestamp. The plaintext token is returned once. QingCloud stores only its SHA-256 hash.

A provider enrolls with `POST /v1/workers/register` using the `X-QingCloud-Invitation-Token` header instead of the shared enrollment header. Each invitation can enroll exactly one worker and is rejected after expiry or redemption. Existing `X-QingCloud-Enrollment-Token` enrollment remains available for deployments that configure `QCLOUD_ENROLLMENT_TOKEN`.

Keep invitation tokens private and transmit them only over HTTPS when connecting remotely. An invitation authorizes enrollment only. It does not grant admin access or make arbitrary workload execution safe.


## Provider approval and trust controls

Newly enrolled workers start with `approval_status=pending`. They can authenticate and send heartbeats, but the scheduler will not assign them jobs until an administrator explicitly approves them. Administrators can approve, reject, or return a provider to pending review with `PUT /v1/workers/{worker_id}/approval`, using the admin token. An optional note (maximum 500 characters) is retained with the current decision.

Approval changes are recorded in SQLite and can be inspected through the admin-only `GET /v1/workers/{worker_id}/approval-history` endpoint. The provider registry and dashboard expose each provider's approval status. Rejected and pending providers remain visible for administration but are ineligible for scheduling. Approval is an administrative trust gate, not a hardware attestation or security sandbox.


## Provider concurrency limits

Each provider starts with a coordinator-controlled limit of one simultaneous job. Administrators can change the limit from 1 to 16 with the admin-authenticated `PUT /v1/workers/{worker_id}/capacity` endpoint and body `{"max_concurrent_jobs": 2}`. Atomic job claiming excludes providers that have reached their configured limit, so a saturated high-ranked worker does not block other eligible providers. The provider registry and dashboard expose the limit, running count, and available slots. This is a job-count cap, not a CPU/RAM isolation mechanism or a guarantee that a workload fits the machine.

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
- [x] Initial NVIDIA GPU capability probing and authenticated inventory refresh\n- [x] NVIDIA free-VRAM and utilization telemetry with free-memory-aware scheduling
- [x] GPU provider health assessment in admin dashboard API
- [x] Persistent GPU hardware inventory change history
- [x] Coordinator-recorded worker job outcome history and reliability scoring
- [x] Suitability-ranked worker assignment using GPU telemetry, reliability, and live job load
- [x] Authenticated provider registry with automatic stale-heartbeat offline detection
- [ ] Broader GPU capability probing (ROCm/Metal and richer telemetry)
- [x] Distributed CPU sum prototype: split ranges into jobs and verify/aggregate chunk results\n- [x] Multi-worker chunk assignment and aggregation covered by automated coordinator tests\n- [ ] Demonstrate real workers on separate physical machines and improve multi-worker throughput
- [ ] Shared storage backend\n- [x] Admin-authenticated network operations dashboard API
- [ ] YTAgent integration


## Browser dashboard

Open `/dashboard` on the running coordinator to use the browser control room. Enter the value configured as `QCLOUD_ADMIN_TOKEN` when prompted. The page requests the protected `/v1/dashboard` API using the admin header, keeps the token only in page memory, does not put it in the URL or browser storage, and clears it when you disconnect or close the page. It refreshes the snapshot every 15 seconds while connected. Serve the coordinator over HTTPS whenever it is accessed remotely. This UI does not make the prototype safe for public exposure.


## GPU workload reservations

GPU jobs request a minimum `required_gpu_vram_mb`. At claim time, the coordinator selects one physical GPU device on that worker and records `assigned_gpu_index` plus `reserved_gpu_vram_mb` on the job. While the job is running, later claims subtract reservations for that same worker/device from its reported free-VRAM snapshot. A reservation is released automatically when the job succeeds, fails/requeues, or its lease is recovered, because only currently running jobs count. NVIDIA workers report stable `nvidia-smi` device indexes. For older or non-NVIDIA reports without an index, the coordinator uses the device position in the latest inventory.

Reservations are conservative scheduler bookkeeping, not hardware-enforced isolation. They do not stop another local process from consuming GPU memory, and fresh telemetry may already reflect memory used by a running workload, so reservation accounting can temporarily understate headroom. GPU workloads are not yet executable through the built-in worker runtime, which currently supports bounded CPU-safe task types only. No real GPU provider has been connected or hardware-tested.
