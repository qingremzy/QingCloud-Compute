# Self-hosting QingCloud Compute

This is a Docker deployment recipe, not a hosted cloud service. It runs the coordinator on a machine you control and persists its SQLite database in a Docker named volume.

## Requirements

- A Linux host or VM with Docker Engine and the Docker Compose plugin.
- A persistent local disk for the Docker volume.
- A domain name and HTTPS reverse proxy for remote access.
- A host that you are authorized to administer.

The coordinator itself does not need a GPU. GPU compute comes from separately enrolled worker machines. Do not expose port 8000 directly to the public internet.

## Start the coordinator

1. Clone this repository on the host.
2. Copy `.env.example` to `.env`.
3. Replace both values with different, high-entropy secrets. Keep `.env` private.
4. Run `docker compose up -d --build`.
5. Check `docker compose ps` and `docker compose logs --tail=100 qingcloud`.
6. Confirm `curl http://127.0.0.1:8000/health` returns a healthy response.
7. Configure a TLS reverse proxy to forward HTTPS traffic to `127.0.0.1:8000`. Preserve request headers and request bodies. Then set `QINGCLOUD_URL` in YTAgent to the HTTPS URL.

The compose file binds the service to loopback only. It uses a named volume for `/data`, runs as a non-root user, drops Linux capabilities, enables a read-only container filesystem, and keeps secrets in an untracked `.env` file. These controls reduce exposure but do not replace host hardening, backups, rate limiting, or a proper security review.

## Backups and upgrades

- Back up the Docker volume `qingcloud-data` regularly while the service is stopped or using a SQLite-consistent backup procedure.
- Keep the admin token only in the coordinator environment and trusted server-side clients.
- Rotate secrets if they are exposed. Existing worker tokens are separate and are stored as hashes in SQLite.
- Review changes before pulling and rebuilding. Keep a copy of the database before upgrades.

## Connect YTAgent

Set these server-side environment variables in YTAgent:

- `QINGCLOUD_URL=https://your-domain.example`
- `QINGCLOUD_ADMIN_TOKEN=<same value as QCLOUD_ADMIN_TOKEN>`
- `QINGCLOUD_OWNER_CHANNEL_ID=<the approved YouTube channel ID>`

Restart YTAgent after changing environment variables. Do not put the admin token in browser code or worker hosts.

## Enroll a real GPU provider

A provider machine must have a supported GPU driver and a compatible Python environment. For NVIDIA GPU execution, install a PyTorch build compatible with that machine's installed NVIDIA driver and CUDA runtime. The worker agent itself does not install drivers or PyTorch.

From the QingCloud repository on the provider machine:

```bash
python -m venv .venv
# Activate the environment, then install the project's requirements.
pip install -r requirements.txt
export QCLOUD_URL="https://your-domain.example"
export QCLOUD_INVITATION_TOKEN="one-time-token-from-YTAgent"
python -m worker_agent.agent --once
unset QCLOUD_INVITATION_TOKEN
python -m worker_agent.agent --enable-jobs --interval 30
```

Before approval, inspect the provider identity, GPU model, driver, and reported VRAM. Approve only machines whose owners you trust. Keep the worker config at `~/.qingcloud/worker.json` private.

## First real GPU smoke test

The worker runtime includes a bounded `gpu_matrix_multiply` proof task. It requires PyTorch with CUDA available and accepts matrix sizes 32–2048. Submit it only to a trusted, approved NVIDIA worker with a CUDA-enabled PyTorch environment. The job should request a non-zero `required_gpu_vram_mb` and the `gpu_execution=cuda_torch` label. The worker selects the coordinator-assigned physical GPU index and returns the device name, elapsed time, and checksum.

This smoke test proves that a real CUDA operation ran on that provider. It is not a video-generation pipeline, a general model-serving runtime, a sandbox for third-party code, or a production GPU tenancy boundary. Do not submit arbitrary scripts, model files, or user workloads. The coordinator currently supports an allowlist of built-in tasks only.

## $0 constraint

This recipe does not provision hosting, domains, TLS certificates, GPUs, storage, or bandwidth. A machine that stays online and is reachable over HTTPS is still required. Use only infrastructure you already control or have explicit permission to use at no charge. Do not enable a paid plan, trial, billing account, or paid resource to follow this guide.
