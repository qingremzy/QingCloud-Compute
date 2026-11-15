import pytest
from fastapi import HTTPException

from qingcloud.security import verify_admin_token


def test_admin_auth_fails_closed_when_token_is_not_configured(monkeypatch):
    monkeypatch.delenv("QCLOUD_ADMIN_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        verify_admin_token("anything")
    assert exc.value.status_code == 503


def test_admin_auth_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setenv("QCLOUD_ADMIN_TOKEN", "a-long-test-secret")
    for token in (None, "wrong-secret"):
        with pytest.raises(HTTPException) as exc:
            verify_admin_token(token)
        assert exc.value.status_code == 401


def test_admin_auth_accepts_matching_token(monkeypatch):
    monkeypatch.setenv("QCLOUD_ADMIN_TOKEN", "a-long-test-secret")
    verify_admin_token("a-long-test-secret")
