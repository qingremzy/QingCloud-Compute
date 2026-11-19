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

The safe runtime supports diagnostic, echo (2,000-character limit), SHA-256 (10,000-character limit), and sum_range chunks (at most 10,000 integers per chunk). The distributed-sum API has an automated two-worker identity test that verifies distinct chunk assignment and final aggregation. Distributed task chunk records are persisted in a single SQLite transaction, so a coordinator interruption cannot leave a partially-created task. This validates coordinator behavior in tests, not execution across two real physical machines. Failed attempts are requeued up to each job's max_attempts setting. A worker that stops renewing its job lease is treated as abandoned after 180 seconds, and the job is requeued or marked failed when another worker claims work. Submit `POST /v1/distributed/sum` with `start`, `end`, and optional `chunk_size` (maximum 10,000) to split an inclusive integer range into independently schedulable CPU jobs. Each job is assigned a task ID. Poll `GET /v1/distributed/sum/{task_id}` for progress and the final total. The coordinator verifies each returned chunk's boundaries, count, and arithmetic before accepting its partial sum. The whole task is limited to 1,000,000 integers and 100 chunks. Unknown job kinds are rejected. The worker never runs shell commands or evaluates submitted payloads. Job execution is disabled unless --enable-jobs is supplied. By default, the agent processes one job per heartbeat cycle. Set --max-jobs-per-cycle to a value from 1 to 16 to process a bounded batch before the next heartbeat.

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

The built-in worker agent accepts either `QCLOUD_INVITATION_TOKEN` or `QCLOUD_ENROLLMENT_TOKEN`. For a one-time invitation, set `QCLOUD_URL` and `QCLOUD_INVITATION_TOKEN`, then run:

```bash
python -m worker_agent.agent --once
```

The agent exchanges the invitation for its own worker access token and saves that token in `~/.qingcloud/worker.json`. After enrollment, remove the invitation from the environment. Subsequent runs reuse the saved worker identity and token.

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
- [x] Basic retry limits and expired-worker lease recovery\n- [x] In-process claim locking to prevent duplicate claims within one API process\n- [x] Durable SQLite queue with database-level transactional claiming across coordinator processes sharing one database file
- [x] Worker agent for hardware discovery and authenticated heartbeat (no job execution)
- [x] Opt-in worker runtime for bounded built-in diagnostic jobs
- [ ] Secure, sandboxed worker runtime for third-party workloads\n- [x] Declared per-job CPU-core and RAM requirements with telemetry-aware eligibility checks
- [x] Initial NVIDIA GPU capability probing and authenticated inventory refresh\n- [x] NVIDIA free-VRAM and utilization telemetry with free-memory-aware scheduling
- [x] GPU provider health assessment in admin dashboard API
- [x] Persistent GPU hardware inventory change history
- [x] Coordinator-recorded worker job outcome history and reliability scoring
- [x] Suitability-ranked worker assignment using GPU telemetry, reliability, and live job load
- [x] Authenticated provider registry with automatic stale-heartbeat offline detection
- [ ] Broader GPU capability probing (ROCm/Metal and richer telemetry)
- [x] Distributed CPU sum prototype: split ranges into jobs and verify/aggregate chunk results\n- [x] Multi-worker chunk assignment and aggregation covered by automated coordinator tests\n- [ ] Demonstrate real workers on separate physical machines and improve multi-worker throughput
- [ ] Shared storage backend\n- [x] Admin-authenticated network operations dashboard API
- [x] Initial YTAgent server-to-coordinator integration (authenticated distributed CPU task submission and result polling)\n- [ ] Offload YTAgent Wan video generation and narration to sandboxed QingCloud GPU workers


## Browser dashboard

Open `/dashboard` on the running coordinator to use the browser control room. Enter the value configured as `QCLOUD_ADMIN_TOKEN` when prompted. The page requests the protected `/v1/dashboard` API using the admin header, keeps the token only in page memory, does not put it in the URL or browser storage, and clears it when you disconnect or close the page. It refreshes the snapshot every 15 seconds while connected. Serve the coordinator over HTTPS whenever it is accessed remotely. This UI does not make the prototype safe for public exposure.


## GPU workload reservations

GPU jobs request a minimum `required_gpu_vram_mb`. At claim time, the coordinator selects one physical GPU device on that worker and records `assigned_gpu_index` plus `reserved_gpu_vram_mb` on the job. While the job is running, later claims subtract reservations for that same worker/device from its reported free-VRAM snapshot. A reservation is released automatically when the job succeeds, fails/requeues, or its lease is recovered, because only currently running jobs count. NVIDIA workers report stable `nvidia-smi` device indexes. For older or non-NVIDIA reports without an index, the coordinator uses the device position in the latest inventory.

Reservations are conservative scheduler bookkeeping, not hardware-enforced isolation. They do not stop another local process from consuming GPU memory, and fresh telemetry may already reflect memory used by a running workload, so reservation accounting can temporarily understate headroom. The worker runtime now includes one bounded NVIDIA CUDA proof workload, `gpu_matrix_multiply`, which requires a CUDA-enabled PyTorch installation on the provider and an explicit per-device assignment. It is a smoke test only. No real GPU provider has been connected or hardware-tested, and no model inference or video-generation workload is supported yet.


## Live GPU reservation dashboard

The admin dashboard now shows active GPU reservations by physical device, each device's reported free VRAM less the coordinator's active reservation estimate, and the GPU index plus reserved amount on each assigned job. Reservation totals are derived from jobs currently in the running state. Completion, retry after failure, and expired-lease recovery clear the active reservation amount. These figures are scheduler estimates, not hardware-enforced memory isolation or a live guarantee of available VRAM.


### Coordinator restart recovery

On startup, the coordinator reloads persisted jobs and transactionally checks running-job leases against the configured lease timeout. Expired jobs are requeued when attempts remain or failed when their attempt limit is exhausted. Recovery clears the assigned GPU device and VRAM reservation and persists the updated job record before the coordinator serves new work. Jobs with a still-valid lease are left running until their lease expires. The same persisted recovery check runs before worker claims. This prevents expired reservations from surviving a restart, but it does not prove a worker process has stopped or forcibly reclaim physical GPU memory.

### Worker attempt fencing

Every successful job claim increments a monotonically increasing attempt number. The worker agent must send that exact attempt value on job heartbeat and completion requests. The coordinator checks the worker identity, running state, and attempt number, then compares and persists the update inside a SQLite write transaction. A heartbeat or completion from an expired or superseded attempt receives HTTP 409 and cannot overwrite the current attempt's persisted job state. This is coordinator-side stale-request protection, not process termination or physical GPU isolation.


### Live provider resource telemetry

The worker agent samples host CPU utilization and RAM total, used, available, and percentage through `psutil`, plus available per-GPU utilization and VRAM readings from the existing NVIDIA probe. It sends these snapshots over the authenticated `POST /v1/workers/{worker_id}/telemetry` endpoint. The coordinator stores timestamped samples in SQLite. Administrators can inspect recent samples with `GET /v1/workers/{worker_id}/telemetry?limit=50`, and the dashboard API includes each worker's latest sample. History is capped per request (1–200 samples) but the prototype does not yet prune old telemetry rows automatically. Values are self-reported host telemetry, not hardware attestation or resource isolation. No real provider has been connected or benchmarked.


### Resource-aware scheduling

When a worker has a resource telemetry sample no older than 120 seconds, scheduling adds live CPU utilization and available RAM to its suitability score. CPU pressure and RAM headroom influence both CPU and GPU job ranking, alongside existing reliability, concurrency, GPU free-memory, utilization, and reservation checks. Missing, malformed, or stale samples receive a neutral resource score rather than being treated as current. These scores are heuristics based on self-reported telemetry and do not guarantee workload fit or enforce CPU/RAM limits.

### Declared CPU and RAM workload requirements

Job submissions can declare `required_cpu_cores` (0–256) and `required_ram_mb` (0–1,048,576). Zero preserves the backward-compatible unspecified requirement. Workers must report enough total CPU cores and RAM to meet the request. When a worker has a resource telemetry sample no older than 120 seconds, the scheduler also requires its reported currently available RAM to meet the job's RAM requirement. Stale or missing RAM samples fall back to reported total RAM. These are admission checks based on self-reported values, not reservations or OS-enforced isolation. Concurrent jobs can still compete for CPU and RAM, and the worker runtime remains limited to its allowlisted built-in tasks.


### CPU and RAM reservation accounting

When a job is claimed, its declared CPU-core and RAM requirements are recorded as scheduler reservations against that worker. Later claims subtract active reservations from reported CPU-core capacity and RAM capacity. Fresh RAM telemetry is also reduced by active RAM reservations before checking a new job. Reservations are released on job completion, retry/failure completion, or expired-lease recovery. The dashboard API reports active per-worker totals and per-job reservations. These are coordinator-side accounting estimates only: they do not pin CPU cores, allocate memory, prevent unrelated processes from using resources, or provide OS/container isolation. Providers and their telemetry are self-reported.


### Worker-side resource preflight

Immediately after claiming a job and before running its allowlisted handler, the worker agent takes a fresh local CPU/RAM/GPU snapshot and checks the job's declared CPU-core, available-RAM, and GPU free-VRAM requirements again. If the local snapshot no longer meets a requirement, the agent does not execute the task, reports a failed attempt with the reason, and stops processing further jobs in that cycle so another provider can claim a retry. This is a last-moment admission check, not an OS-enforced resource limit. CPU/RAM can still change after the check, GPU telemetry can be unavailable or stale, and the built-in runtime remains limited to its existing CPU-safe task handlers.


### Worker job decline and retry accounting

A worker that claims a job but discovers locally that it cannot safely run it can call the worker-authenticated `POST /v1/jobs/{job_id}/decline?worker_id=...&attempt=...` endpoint with a short reason. The coordinator verifies the worker identity and monotonic attempt fencing token, releases all CPU/RAM/GPU reservations, and returns the job to the queue without recording a failed execution outcome. The claim counter remains monotonic so stale workers cannot reuse an old fencing token. A separate declined-attempt counter means resource-based declines do not consume the configured failed-execution retry budget. Declines are not counted as successful or failed execution outcomes for reliability scoring.

## YTAgent first-customer integration

The YTAgent application can act as an authenticated QingCloud client. On the YTAgent server, configure `QINGCLOUD_URL` with this coordinator's HTTPS base URL and `QINGCLOUD_ADMIN_TOKEN` with the coordinator admin token. Never put the admin token in browser JavaScript, a mobile app, a notebook, or a worker configuration.

YTAgent's server-side client uses:
- `GET /health` and admin-authenticated `GET /v1/overview` for connection/provider status.
- `POST /v1/distributed/sum` to submit a bounded CPU task.
- `GET /v1/distributed/sum/{task_id}` to retrieve verified progress and results.

The YTAgent API wraps these as authenticated `GET /api/compute/status`, `POST /api/compute/test`, and `GET /api/compute/tasks/{task_id}` routes. The test task defaults to summing 1–1,000 in chunks of 100. It is a real control-plane and distributed CPU integration test: it will remain queued until at least one online, approved worker runs the built-in worker agent with `--enable-jobs`.

**Scope boundary:** this integration does not yet run YTAgent's Wan video generation, Kokoro narration, FFmpeg compiler, or GPU inference on QingCloud. The existing YTAgent Kaggle GPU worker remains separate. A dedicated GPU workload adapter, media transfer protocol, sandbox, and real-hardware validation are still required before video-generation offload can be claimed.


### Job source attribution

Jobs carry a short `source` label (default `api`) so an integrated application can identify its own workloads in the admin dashboard. Distributed tasks preserve that source label on every chunk and in task status. This field is descriptive metadata, not an authorization boundary. YTAgent submits its first distributed CPU verification task with `source="ytagent"`.


## Container deployment and first CUDA smoke test

A Dockerfile and Docker Compose configuration are provided for a persistent, non-root coordinator deployment. The compose service binds only to localhost and stores SQLite data in a named volume. See [the self-hosting deployment guide](docs/SELF_HOSTED_DEPLOYMENT.md) for HTTPS reverse-proxy, backup, YTAgent connection, and provider-enrollment steps. The deployment files do not provision a free public host or GPU hardware.

The worker agent advertises the `gpu_execution=cuda_torch` label only when PyTorch reports CUDA available. The bounded `gpu_matrix_multiply` task runs a matrix multiplication on the coordinator-assigned NVIDIA device and returns device name, elapsed time, and checksum. It accepts matrix sizes from 32 through 2048 and requires a non-zero `required_gpu_vram_mb` plus the `gpu_execution=cuda_torch` required label. A worker must have a compatible PyTorch/CUDA environment installed by its owner. This proves only that a CUDA operation can execute on a real provider. It does not run Wan, Kokoro, FFmpeg, arbitrary Python, or YTAgent video generation.
