import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from qingcloud.main import app, workers, jobs, worker_token_hashes
from qingcloud.db import clear_all, invitation_token_hash_exists, create_worker_invitation
from qingcloud.security import hash_worker_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    clear_all()
    workers.clear()
    jobs.clear()
    worker_token_hashes.clear()
    monkeypatch.setenv("QCLOUD_ENROLLMENT_TOKEN", "test-enrollment-secret")
    monkeypatch.setenv("QCLOUD_ADMIN_TOKEN", "test-admin-secret")
    yield
    workers.clear()
    jobs.clear()
    worker_token_hashes.clear()
    clear_all()


def enroll(name="test-worker"):
    response = client.post(
        "/v1/workers/register",
        headers={"X-QingCloud-Enrollment-Token": "test-enrollment-secret"},
        json={"name": name, "cpu_cores": 2, "ram_mb": 4096, "gpus": []},
    )
    assert response.status_code == 200
    body = response.json()
    worker_id = body["worker"]["worker_id"]
    approved = client.put(
        f"/v1/workers/{worker_id}/approval",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
        json={"status": "approved", "note": "Approved by test fixture"},
    )
    assert approved.status_code == 200
    return worker_id, body["access_token"]


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
    job = client.post("/v1/jobs", headers={"X-QingCloud-Admin-Token": "test-admin-secret"}, json={"kind": "diagnostic"}).json()

    claim = None
    assigned_worker = None
    assigned_token = None
    for worker_id, token in ((worker_a, token_a), (worker_b, token_b)):
        response = client.post(
            f"/v1/workers/{worker_id}/claim",
            headers={"X-QingCloud-Worker-Token": token},
        )
        assert response.status_code == 200
        if response.json() is not None:
            claim = response
            assigned_worker = worker_id
            assigned_token = token
            break
    assert claim is not None

    other_worker, other_token = (
        (worker_b, token_b) if assigned_worker == worker_a else (worker_a, token_a)
    )
    job_id = job["job_id"]
    denied = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={other_worker}",
        headers={"X-QingCloud-Worker-Token": other_token},
        json={"success": True, "result": {"ok": True}},
    )
    assert denied.status_code == 403

    allowed = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={assigned_worker}",
        headers={"X-QingCloud-Worker-Token": assigned_token},
        json={"success": True, "result": {"ok": True}},
    )
    assert allowed.status_code == 200

def test_worker_can_refresh_own_capabilities_only():
    worker_id, token = enroll("worker-original")
    response = client.put(
        f"/v1/workers/{worker_id}/capabilities",
        headers={"X-QingCloud-Worker-Token": token},
        json={"name": "worker-refreshed", "cpu_cores": 8, "ram_mb": 16384,
              "gpus": [{"name": "NVIDIA T4", "vendor": "NVIDIA", "vram_mb": 15360}],
              "labels": {"agent": "qingcloud-worker-agent"}},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "worker-refreshed"
    assert response.json()["cpu_cores"] == 8
    assert response.json()["gpus"][0]["vram_mb"] == 15360

def test_capability_refresh_requires_worker_token():
    worker_id, token = enroll()
    denied = client.put(
        f"/v1/workers/{worker_id}/capabilities",
        json={"name": "spoofed-worker"},
    )
    assert denied.status_code == 401


def test_failed_job_is_requeued_until_attempt_limit():
    worker_id, token = enroll("retry-worker")
    job = client.post("/v1/jobs", headers={"X-QingCloud-Admin-Token": "test-admin-secret"}, json={"kind": "echo", "max_attempts": 2}).json()
    headers = {"X-QingCloud-Worker-Token": token}
    first_claim = client.post(f"/v1/workers/{worker_id}/claim", headers=headers)
    assert first_claim.status_code == 200
    job_id = job["job_id"]
    failed = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={worker_id}",
        headers=headers,
        json={"success": False, "error": "temporary failure"},
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "queued"
    assert failed.json()["assigned_worker_id"] is None

    second_claim = client.post(f"/v1/workers/{worker_id}/claim", headers=headers)
    assert second_claim.status_code == 200
    assert second_claim.json()["attempts"] == 2
    final = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={worker_id}",
        headers=headers,
        json={"success": False, "error": "still failing"},
    )
    assert final.status_code == 200
    assert final.json()["state"] == "failed"


def test_distributed_sum_splits_jobs_and_aggregates_verified_results():
    worker_id, token = enroll("sum-worker")
    headers = {"X-QingCloud-Worker-Token": token}
    created = client.post("/v1/distributed/sum", headers={"X-QingCloud-Admin-Token": "test-admin-secret"}, json={"start": 1, "end": 10, "chunk_size": 4})
    assert created.status_code == 200
    task = created.json()
    assert task["chunk_count"] == 3
    assert task["integer_count"] == 10

    for _ in range(3):
        claimed = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
        assert claimed["kind"] == "sum_range"
        start = claimed["payload"]["start"]
        end = claimed["payload"]["end"]
        result = {"start": start, "end": end, "count": end - start + 1,
                  "sum": (start + end) * (end - start + 1) // 2}
        completed = client.post(
            f"/v1/jobs/{claimed['job_id']}/complete?worker_id={worker_id}",
            headers=headers,
            json={"success": True, "result": result},
        )
        assert completed.status_code == 200

    status = client.get(f"/v1/distributed/sum/{task['task_id']}", headers={"X-QingCloud-Admin-Token": "test-admin-secret"})
    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert status.json()["total"] == 55
    assert status.json()["succeeded_chunks"] == 3


def test_distributed_sum_rejects_invalid_or_excessive_ranges():
    assert client.post("/v1/distributed/sum", headers={"X-QingCloud-Admin-Token": "test-admin-secret"}, json={"start": 10, "end": 1}).status_code == 422
    assert client.post("/v1/distributed/sum", headers={"X-QingCloud-Admin-Token": "test-admin-secret"}, json={"start": 1, "end": 1000001}).status_code == 422


def test_distributed_sum_rejects_worker_result_mismatch():
    worker_id, token = enroll("unreliable-worker")
    headers = {"X-QingCloud-Worker-Token": token}
    task = client.post("/v1/distributed/sum", headers={"X-QingCloud-Admin-Token": "test-admin-secret"}, json={"start": 1, "end": 5, "chunk_size": 5}).json()
    claimed = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    client.post(
        f"/v1/jobs/{claimed['job_id']}/complete?worker_id={worker_id}",
        headers=headers,
        json={"success": True, "result": {"start": 1, "end": 5, "count": 5, "sum": 999}},
    )
    status = client.get(f"/v1/distributed/sum/{task['task_id']}", headers={"X-QingCloud-Admin-Token": "test-admin-secret"})
    assert status.json()["status"] == "failed"
    assert status.json()["total"] is None
    assert status.json()["invalid_chunks"] == [claimed["job_id"]]


def test_two_workers_claim_distinct_chunks_and_complete_distributed_sum():
    worker_a, token_a = enroll("worker-a")
    worker_b, token_b = enroll("worker-b")
    task = client.post(
        "/v1/distributed/sum",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
        json={"start": 1, "end": 12, "chunk_size": 3},
    ).json()

    # Intelligent scheduling may correctly return no job to a worker that is
    # not currently the highest-ranked candidate. Both workers poll until all
    # chunks are claimed instead of assuming strict A/B alternation.
    claims = []
    worker_credentials = ((worker_a, token_a), (worker_b, token_b))
    for _ in range(8):
        if len(claims) == 4:
            break
        for worker_id, token in worker_credentials:
            if len(claims) == 4:
                break
            response = client.post(
                f"/v1/workers/{worker_id}/claim",
                headers={"X-QingCloud-Worker-Token": token},
            )
            assert response.status_code == 200
            claimed = response.json()
            if claimed is not None:
                claims.append((worker_id, token, claimed))

    assert len(claims) == 4
    assert len({item[2]["job_id"] for item in claims}) == 4
    assert {item[0] for item in claims} == {worker_a, worker_b}

    for worker_id, token, job in claims:
        start = job["payload"]["start"]
        end = job["payload"]["end"]
        result = {
            "start": start,
            "end": end,
            "count": end - start + 1,
            "sum": (start + end) * (end - start + 1) // 2,
        }
        completed = client.post(
            f"/v1/jobs/{job['job_id']}/complete?worker_id={worker_id}",
            headers={"X-QingCloud-Worker-Token": token},
            json={"success": True, "result": result},
        )
        assert completed.status_code == 200

    status = client.get(
        f"/v1/distributed/sum/{task['task_id']}",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
    )
    assert status.json()["status"] == "succeeded"
    assert status.json()["total"] == 78
    assert status.json()["succeeded_chunks"] == 4


def test_overview_reports_online_hardware_inventory_without_pooling_vram():
    enroll("inventory-worker")
    overview = client.get("/v1/overview", headers={"X-QingCloud-Admin-Token": "test-admin-secret"}).json()
    assert overview["online_workers"] == 1
    assert overview["reported_cpu_cores_online"] == 2
    assert overview["reported_ram_mb_online"] == 4096
    assert overview["reported_gpu_count_online"] == 0
    assert "not one unified GPU memory pool" in overview["note"]


def test_dashboard_requires_admin_token_and_reports_network_inventory():
    worker_id, token = enroll("dashboard-worker")
    assert client.get("/v1/dashboard").status_code == 401
    response = client.get("/v1/dashboard", headers={"X-QingCloud-Admin-Token": "test-admin-secret"})
    assert response.status_code == 200
    body = response.json()
    assert body["overview"]["online_workers"] == 1
    assert body["workers"][0]["worker_id"] == worker_id
    assert body["workers"][0]["name"] == "dashboard-worker"
    assert body["workers"][0]["running_jobs"] == 0
    assert body["jobs"] == []
    assert "not a unified memory pool" in body["note"]
    assert body["workers"][0]["gpu_health"]["status"] == "no_gpu"
    assert "capabilities_updated_at" in body["workers"][0]


def test_job_submission_and_inspection_require_admin_token():
    assert client.post("/v1/jobs", json={"kind": "diagnostic"}).status_code == 401
    assert client.get("/v1/jobs").status_code == 401
    assert client.get("/v1/workers").status_code == 401


def test_browser_dashboard_page_is_served_without_embedded_admin_secret():
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "QINGCLOUD" in response.text
    assert "X-QingCloud-Admin-Token" in response.text
    assert "localStorage" not in response.text
    assert "sessionStorage" not in response.text
    assert "test-admin-secret" not in response.text



def test_gpu_history_records_hardware_changes_and_requires_admin_token():
    worker_id, token = enroll("gpu-history-worker")
    endpoint = f"/v1/workers/{worker_id}/gpu-history"
    assert client.get(endpoint).status_code == 401
    headers = {"X-QingCloud-Worker-Token": token}
    first = client.put(
        f"/v1/workers/{worker_id}/capabilities",
        headers=headers,
        json={"name": "gpu-history-worker", "gpus": [
            {"name": "NVIDIA T4", "vendor": "NVIDIA", "vram_mb": 16000,
             "free_vram_mb": 12000, "utilization_percent": 10}
        ]},
    )
    assert first.status_code == 200
    # Volatile telemetry changes must not create a hardware-change event.
    same_gpu = client.put(
        f"/v1/workers/{worker_id}/capabilities",
        headers=headers,
        json={"name": "gpu-history-worker", "gpus": [
            {"name": "NVIDIA T4", "vendor": "NVIDIA", "vram_mb": 16000,
             "free_vram_mb": 1000, "utilization_percent": 95}
        ]},
    )
    assert same_gpu.status_code == 200
    added_gpu = client.put(
        f"/v1/workers/{worker_id}/capabilities",
        headers=headers,
        json={"name": "gpu-history-worker", "gpus": [
            {"name": "NVIDIA T4", "vendor": "NVIDIA", "vram_mb": 16000},
            {"name": "NVIDIA A10", "vendor": "NVIDIA", "vram_mb": 23000}
        ]},
    )
    assert added_gpu.status_code == 200
    history = client.get(endpoint, headers={"X-QingCloud-Admin-Token": "test-admin-secret"})
    assert history.status_code == 200
    events = history.json()["events"]
    assert [event["event_type"] for event in events] == ["gpu_added", "initial_gpu_inventory"]
    assert "volatile live telemetry" in history.json()["note"]


def test_gpu_history_returns_not_found_for_unknown_worker():
    response = client.get(
        "/v1/workers/missing/gpu-history",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
    )
    assert response.status_code == 404


def test_worker_reliability_counts_attempt_outcomes_without_inflating_claims():
    worker_id, token = enroll("reliability-worker")
    endpoint = f"/v1/workers/{worker_id}/reliability"
    assert client.get(endpoint).status_code == 401
    headers = {"X-QingCloud-Worker-Token": token}
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    initial = client.get(endpoint, headers=admin).json()
    assert initial["status"] == "unrated"
    assert initial["score"] is None
    assert initial["sample_size"] == 0

    job = client.post("/v1/jobs", headers=admin, json={"kind": "echo", "max_attempts": 2}).json()
    claimed = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    assert claimed["job_id"] == job["job_id"]
    # A claim alone is not a success or failure.
    after_claim = client.get(endpoint, headers=admin).json()
    assert after_claim["sample_size"] == 0

    failed = client.post(
        f"/v1/jobs/{job['job_id']}/complete?worker_id={worker_id}",
        headers=headers, json={"success": False, "error": "temporary"},
    )
    assert failed.status_code == 200
    second = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    assert second["attempts"] == 2
    succeeded = client.post(
        f"/v1/jobs/{job['job_id']}/complete?worker_id={worker_id}",
        headers=headers, json={"success": True, "result": {"ok": True}},
    )
    assert succeeded.status_code == 200

    result = client.get(endpoint, headers=admin)
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "limited_data"
    assert body["sample_size"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["observed_success_rate"] == 50.0
    assert body["score"] == 50.0


def test_worker_reliability_returns_not_found_for_unknown_worker():
    response = client.get(
        "/v1/workers/missing/reliability",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
    )
    assert response.status_code == 404


def test_dashboard_includes_unrated_worker_reliability():
    worker_id, _ = enroll("unrated-worker")
    response = client.get("/v1/dashboard", headers={"X-QingCloud-Admin-Token": "test-admin-secret"})
    assert response.status_code == 200
    assert response.json()["workers"][0]["reliability"]["status"] == "unrated"


def test_provider_registry_marks_stale_workers_offline_and_persists_state():
    from datetime import timedelta
    from qingcloud.main import now
    worker_id, _ = enroll("presence-worker")
    workers[worker_id].last_seen_at = now() - timedelta(seconds=181)
    from qingcloud.db import save_worker
    save_worker(workers[worker_id])
    denied = client.get("/v1/providers")
    assert denied.status_code == 401
    response = client.get("/v1/providers", headers={"X-QingCloud-Admin-Token": "test-admin-secret"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["offline"] == 1
    assert body["providers"][0]["state"] == "offline"
    assert body["providers"][0]["heartbeat_age_seconds"] >= 181
    assert workers[worker_id].state.value == "offline"


def test_fresh_heartbeat_returns_offline_provider_to_online_registry():
    worker_id, token = enroll("reconnecting-worker")
    from datetime import timedelta
    from qingcloud.main import now
    workers[worker_id].last_seen_at = now() - timedelta(seconds=181)
    from qingcloud.db import save_worker
    save_worker(workers[worker_id])
    headers = {"X-QingCloud-Worker-Token": token}
    heartbeat = client.post(f"/v1/workers/{worker_id}/heartbeat", headers=headers)
    assert heartbeat.status_code == 200
    registry = client.get("/v1/providers", headers={"X-QingCloud-Admin-Token": "test-admin-secret"})
    assert registry.json()["online"] == 1

 

def test_invitation_creation_requires_admin_token():
    assert client.post("/v1/invitations", json={"expires_in_hours": 24}).status_code == 401
    response = client.post(
        "/v1/invitations",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
        json={"expires_in_hours": 24},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["invitation_token"]
    assert body["expires_in_hours"] == 24
    assert invitation_token_hash_exists(hash_worker_token(body["invitation_token"]))


def test_invitation_enrolls_one_worker_and_cannot_be_reused():
    created = client.post(
        "/v1/invitations",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
        json={"expires_in_hours": 24},
    )
    invitation = created.json()["invitation_token"]
    registration = client.post(
        "/v1/workers/register",
        headers={"X-QingCloud-Invitation-Token": invitation},
        json={"name": "invited-worker", "cpu_cores": 2, "ram_mb": 4096},
    )
    assert registration.status_code == 200
    assert registration.json()["worker"]["name"] == "invited-worker"
    assert registration.json()["access_token"]
    reused = client.post(
        "/v1/workers/register",
        headers={"X-QingCloud-Invitation-Token": invitation},
        json={"name": "second-worker"},
    )
    assert reused.status_code == 401


def test_expired_invitation_is_rejected():
    expired = "expired-invitation-for-test"
    create_worker_invitation(
        hash_worker_token(expired),
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    response = client.post(
        "/v1/workers/register",
        headers={"X-QingCloud-Invitation-Token": expired},
        json={"name": "too-late-worker"},
    )
    assert response.status_code == 401


def test_invitation_expiry_is_bounded():
    headers = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    assert client.post("/v1/invitations", headers=headers, json={"expires_in_hours": 0}).status_code == 422
    assert client.post("/v1/invitations", headers=headers, json={"expires_in_hours": 169}).status_code == 422

 

def test_new_provider_is_pending_and_cannot_claim_until_approved():
    registration = client.post(
        "/v1/workers/register",
        headers={"X-QingCloud-Enrollment-Token": "test-enrollment-secret"},
        json={"name": "pending-provider", "cpu_cores": 4, "ram_mb": 8192},
    )
    assert registration.status_code == 200
    worker_id = registration.json()["worker"]["worker_id"]
    token = registration.json()["access_token"]
    assert registration.json()["worker"]["approval_status"] == "pending"
    job = client.post(
        "/v1/jobs",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
        json={"kind": "diagnostic"},
    ).json()
    blocked = client.post(
        f"/v1/workers/{worker_id}/claim",
        headers={"X-QingCloud-Worker-Token": token},
    )
    assert blocked.status_code == 200
    assert blocked.json() is None

    approved = client.put(
        f"/v1/workers/{worker_id}/approval",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
        json={"status": "approved", "note": "Hardware checked"},
    )
    assert approved.status_code == 200
    assert approved.json()["approval_status"] == "approved"
    claimed = client.post(
        f"/v1/workers/{worker_id}/claim",
        headers={"X-QingCloud-Worker-Token": token},
    )
    assert claimed.json()["job_id"] == job["job_id"]


def test_rejected_provider_cannot_claim_and_approval_changes_are_audited():
    worker_id, token = enroll("reviewed-provider")
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    rejected = client.put(
        f"/v1/workers/{worker_id}/approval",
        headers=admin,
        json={"status": "rejected", "note": "Unverified host"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["approval_status"] == "rejected"
    job = client.post("/v1/jobs", headers=admin, json={"kind": "diagnostic"}).json()
    blocked = client.post(
        f"/v1/workers/{worker_id}/claim",
        headers={"X-QingCloud-Worker-Token": token},
    )
    assert blocked.status_code == 200
    assert blocked.json() is None
    history = client.get(f"/v1/workers/{worker_id}/approval-history", headers=admin)
    assert history.status_code == 200
    statuses = [(event["previous_status"], event["new_status"]) for event in history.json()["events"]]
    assert ("pending", "approved") in statuses
    assert ("approved", "rejected") in statuses


def test_provider_approval_endpoints_require_admin_and_handle_missing_workers():
    assert client.put("/v1/workers/missing/approval", json={"status": "approved"}).status_code == 401
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    assert client.put("/v1/workers/missing/approval", headers=admin, json={"status": "approved"}).status_code == 404
    assert client.get("/v1/workers/missing/approval-history", headers=admin).status_code == 404
