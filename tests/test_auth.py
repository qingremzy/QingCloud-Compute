import pytest
from fastapi.testclient import TestClient

from qingcloud.main import app, workers, jobs, worker_token_hashes

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    workers.clear()
    jobs.clear()
    worker_token_hashes.clear()
    monkeypatch.setenv("QCLOUD_ENROLLMENT_TOKEN", "test-enrollment-secret")
    yield
    workers.clear()
    jobs.clear()
    worker_token_hashes.clear()


def enroll(name="test-worker"):
    response = client.post(
        "/v1/workers/register",
        headers={"X-QingCloud-Enrollment-Token": "test-enrollment-secret"},
        json={"name": name, "cpu_cores": 2, "ram_mb": 4096, "gpus": []},
    )
    assert response.status_code == 200
    body = response.json()
    return body["worker"]["worker_id"], body["access_token"]


def test_registration_requires_enrollment_secret():
    response = client.post("/v1/workers/register", json={"name": "intruder"})
    assert response.status_code == 401


def test_registration_fails_closed_without_server_secret(monkeypatch):
    monkeypatch.delenv("QCLOUD_ENROLLMENT_TOKEN")
    response = client.post(
        "/v1/workers/register",
        headers={"X-QingCloud-Enrollment-Token": "anything"},
        json={"name": "worker"},
    )
    assert response.status_code == 503


def test_worker_heartbeat_requires_valid_token():
    worker_id, token = enroll()
    assert client.post(f"/v1/workers/{worker_id}/heartbeat").status_code == 401
    response = client.post(
        f"/v1/workers/{worker_id}/heartbeat",
        headers={"X-QingCloud-Worker-Token": token},
    )
    assert response.status_code == 200


def test_only_assigned_worker_can_update_job():
    worker_a, token_a = enroll("worker-a")
    worker_b, token_b = enroll("worker-b")
    job = client.post("/v1/jobs", json={"kind": "diagnostic"}).json()
    claim = client.post(
        f"/v1/workers/{worker_a}/claim",
        headers={"X-QingCloud-Worker-Token": token_a},
    )
    assert claim.status_code == 200
    job_id = job["job_id"]

    denied = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={worker_b}",
        headers={"X-QingCloud-Worker-Token": token_b},
        json={"success": True, "result": {"ok": True}},
    )
    assert denied.status_code == 403

    allowed = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={worker_a}",
        headers={"X-QingCloud-Worker-Token": token_a},
        json={"success": True, "result": {"ok": True}},
    )
    assert allowed.status_code == 200
    assert allowed.json()["state"] == "succeeded"
