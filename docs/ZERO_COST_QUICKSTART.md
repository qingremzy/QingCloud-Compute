# QingCloud Compute — $0 Quickstart

This launch kit runs the coordinator on a Windows computer you control. It uses the existing FastAPI coordinator, SQLite persistence, dashboard, scheduler, and worker agent. No paid hosting, GPU rental, domain, or cloud account is created.

## 1. Start your control centre (Windows 10/11)

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/).
2. Download this repository from GitHub and extract it.
3. Open PowerShell in the extracted folder.
4. Run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\start-qingcloud-windows.ps1
```

The script creates a virtual environment, installs dependencies, generates separate random admin/enrollment secrets, saves them in the ignored local file `.qingcloud-local-secrets.json`, and starts the coordinator. Keep the terminal open.

Open **http://127.0.0.1:8000/dashboard** and enter the admin token printed by the script. API documentation is at **http://127.0.0.1:8000/docs**. The SQLite database is stored in the repository folder as `qingcloud.sqlite3`.

## 2. Connect a worker

A worker must be a separate computer with Python 3.11+. For a remote computer, the coordinator must first be reachable through an HTTPS endpoint. The local script deliberately binds to localhost and is not exposed to the internet.

On the worker computer, download/extract the same repository, open PowerShell in its folder, then run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\join-qingcloud-worker-windows.ps1
```

Enter the HTTPS coordinator URL and a one-time invitation token or enrollment token. The script performs enrollment and a first hardware heartbeat. It asks separately before enabling bounded built-in job execution. Worker credentials are stored under the user's home directory in `~/.qingcloud/worker.json`.

## 3. What works now

- Coordinator health endpoint, authenticated admin dashboard and API.
- Worker enrollment, per-worker credentials, heartbeat and hardware inventory.
- CPU/RAM and NVIDIA GPU discovery through `nvidia-smi`.
- Worker online/offline presence, provider approval, GPU health history and reliability records.
- Capability-aware scheduling and bounded allowlisted jobs.
- Durable SQLite job state and atomic distributed-task creation.
- Distributed CPU sum tasks and a bounded CUDA/PyTorch matrix-multiply smoke test on a compatible, approved NVIDIA worker.

## 4. $0 boundaries (important)

- The coordinator runs only while the host computer is powered on and the process is running. This quickstart does not create an always-on public cloud service.
- For remote providers, you still need a secure HTTPS route to the coordinator. A free tunnel may be used if its provider's current terms and limits fit your needs, but you must configure it yourself. Never send enrollment secrets over public HTTP.
- No GPUs are supplied by QingCloud. Providers must voluntarily connect their own compatible machines and keep them online.
- GPU inventory is not a single pooled GPU. A job runs on one compatible worker/device.
- Execution is limited to built-in allowlisted tasks. This is not arbitrary code execution, a public GPU rental marketplace, a model-serving platform, or a production multi-tenant security boundary.
- The YTAgent integration requires a reachable HTTPS coordinator and server-side credentials in YTAgent. Do not place the admin token in browser/mobile code.
- No paid service is activated by these instructions.

## Stop and restart

Press Ctrl+C in the coordinator terminal to stop. Run the start script again to resume from the same SQLite database and local secrets. Back up `qingcloud.sqlite3` and `.qingcloud-local-secrets.json` privately. Never commit or share either file publicly.
