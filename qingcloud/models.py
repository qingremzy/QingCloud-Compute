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


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class GPUDevice(BaseModel):
    name: str
    vendor: str | None = None
    vram_mb: int = Field(default=0, ge=0)
    free_vram_mb: int | None = Field(default=None, ge=0)
    utilization_percent: float | None = Field(default=None, ge=0, le=100)
    driver_version: str | None = None
    compute_capability: str | None = None
    runtime: str | None = None


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


class DistributedSumCreate(BaseModel):
    start: int
    end: int
    chunk_size: int = Field(default=1000, ge=1, le=10000)


class JobCreate(BaseModel):
    kind: str = Field(default="diagnostic", min_length=1, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)
    required_gpu_vram_mb: int = Field(default=0, ge=0)
    required_labels: dict[str, str] = Field(default_factory=dict)
    max_attempts: int = Field(default=2, ge=1, le=10)


class JobRecord(JobCreate):
    job_id: str
    task_id: str | None = None
    state: JobState = JobState.queued
    assigned_worker_id: str | None = None
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
