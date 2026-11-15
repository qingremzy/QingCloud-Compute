from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import Header, HTTPException


def issue_worker_token() -> str:
    return secrets.token_urlsafe(32)


def hash_worker_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_enrollment_token(provided: str | None) -> None:
    expected = os.getenv("QCLOUD_ENROLLMENT_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Worker enrollment is disabled. Configure QCLOUD_ENROLLMENT_TOKEN.",
        )
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid enrollment token")


def require_worker_token(
    worker_id: str,
    token: str | None = Header(default=None, alias="X-QingCloud-Worker-Token"),
    token_hashes: dict[str, str] | None = None,
) -> None:
    stored_hash = (token_hashes or {}).get(worker_id)
    if not token or not stored_hash:
        raise HTTPException(status_code=401, detail="Worker authentication required")
    if not hmac.compare_digest(hash_worker_token(token), stored_hash):
        raise HTTPException(status_code=401, detail="Invalid worker token")
