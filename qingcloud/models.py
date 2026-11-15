from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkerState(str, Enum):
    online = "online"
    offline = "offline"


class WorkerApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class GPUDevice(BaseModel):
    device_index: int | None = Field(default=None, ge=0)
    name: str
    vendor: str | None = None
    vram_mb: int = Field(default=0, ge=0)
    free_vram_mb: int | None = Field(default=None, ge=0)
    utilization_percent: float | None = Field(default=None, ge=0, le=100)
    driver_version: str | None = None
    compute_capability: str | None = None
    runtime: str | None = None


class GPUResourceTelemetry(BaseModel):
    device_index: int | None = Field(default=None, ge=0)
    name: str
    utilization_percent: float | None = Field(default=None, ge=0, le=100)
    vram_mb: int = Field(default=0, ge=0)
    free_vram_mb: int | None = Field(default=None, ge=0)


class WorkerTelemetry(BaseModel):
    cpu_percent: float = Field(ge=0, le=100)
    ram_total_mb: int = Field(ge=0)
    ram_used_mb: int = Field(ge=0)
    ram_available_mb: int = Field(ge=0)
    ram_percent: float = Field(ge=0, le=100)
    gpus: list[GPUResourceTelemetry] = Field(default_factory=list)


class WorkerRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    hostname: str | None = None
    os: str | None = None
    cpu_model: str | None = None
    cpu_cores: int = Field(default=1, ge=1)
    ram_mb: int = Field(default=0, ge=0)
    gpus: list[GPUDevice] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class WorkerRecord(WorkerRegistration):
    worker_id: str
    state: WorkerState = WorkerState.online
    registered_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    capabilities_updated_at: datetime = Field(default_factory=utcnow)
    approval_status: WorkerApprovalStatus = WorkerApprovalStatus.pending
    approval_updated_at: datetime | None = None
    approval_note: str | None = Field(default=None, max_length=500)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=16)


class WorkerApprovalUpdate(BaseModel):
    status: WorkerApprovalStatus
    note: str | None = Field(default=None, max_length=500)


class WorkerCapacityUpdate(BaseModel):
    max_concurrent_jobs: int = Field(ge=1, le=16)


class DistributedSumCreate(BaseModel):
    start: int
    end: int
    chunk_size: int = Field(default=1000, ge=1, le=10000)


class JobCreate(BaseModel):
    kind: str = Field(default="diagnostic", min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    required_gpu_vram_mb: int = Field(default=0, ge=0)
    required_cpu_cores: int = Field(default=0, ge=0, le=256)
    required_ram_mb: int = Field(default=0, ge=0, le=1048576)
    required_labels: dict[str, str] = Field(default_factory=dict)
    max_attempts: int = Field(default=2, ge=1, le=10)


class JobRecord(JobCreate):
    job_id: str
    task_id: str | None = None
    state: JobState = JobState.queued
    assigned_worker_id: str | None = None
    assigned_gpu_index: int | None = Field(default=None, ge=0)
    reserved_gpu_vram_mb: int = Field(default=0, ge=0)
    reserved_cpu_cores: int = Field(default=0, ge=0, le=256)
    reserved_ram_mb: int = Field(default=0, ge=0, le=1048576)
    attempts: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    progress: float = Field(default=0, ge=0, le=100)
    result: dict[str, Any] | None = None
    error: str | None = None


class JobHeartbeat(BaseModel):
    progress: float = Field(ge=0, le=100)
    message: str | None = None


class JobCompletion(BaseModel):
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class WorkerEnrollmentResponse(BaseModel):
    worker: WorkerRecord
    access_token: str
    token_type: str = "Bearer"


class WorkerInvitationCreate(BaseModel):
    expires_in_hours: int = Field(default=24, ge=1, le=168)
