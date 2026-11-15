import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from qingcloud.main import app, workers, jobs, worker_token_hashes
from qingcloud.db import clear_all, invitation_token_hash_exists, create_worker_invitation, save_job, load_state, recover_expired_jobs_persisted
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
        f"/v1/jobs/{job_id}/complete?worker_id={other_worker}&attempt={claim.json()['attempts']}",
        headers={"X-QingCloud-Worker-Token": other_token},
        json={"success": True, "result": {"ok": True}},
    )
    assert denied.status_code == 409

    allowed = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={assigned_worker}&attempt={claim.json()['attempts']}",
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
        f"/v1/jobs/{job_id}/complete?worker_id={worker_id}&attempt={first_claim.json()['attempts']}",
        headers=headers,
        json={"success": False, "error": "temporary failure"},
    )
    assert failed.status_code == 200
    assert failed.json()["state"] == "queued"
    assert failed.json()["assigned_worker_id"] is None

    second_claim = client.post(f"/v1/workers/{worker_id}/claim", headers=headers)
    assert second_claim.status_code == 200
    assert second_claim.json()['attempts'] == 2
    final = client.post(
        f"/v1/jobs/{job_id}/complete?worker_id={worker_id}&attempt={second_claim.json()['attempts']}",
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
            f"/v1/jobs/{claimed['job_id']}/complete?worker_id={worker_id}&attempt={claimed['attempts']}",
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
        f"/v1/jobs/{claimed['job_id']}/complete?worker_id={worker_id}&attempt={claimed['attempts']}",
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
    admin_headers = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    for worker_id in (worker_a, worker_b):
        capacity = client.put(
            f"/v1/workers/{worker_id}/capacity",
            headers=admin_headers,
            json={"max_concurrent_jobs": 2},
        )
        assert capacity.status_code == 200

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
            f"/v1/jobs/{job['job_id']}/complete?worker_id={worker_id}&attempt={claimed['attempts']}",
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
        f"/v1/jobs/{job['job_id']}/complete?worker_id={worker_id}&attempt={claimed['attempts']}",
        headers=headers, json={"success": False, "error": "temporary"},
    )
    assert failed.status_code == 200
    second = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    assert second["attempts"] == 2
    succeeded = client.post(
        f"/v1/jobs/{job['job_id']}/complete?worker_id={worker_id}&attempt={second['attempts']}",
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

 

def test_provider_concurrency_limit_blocks_extra_claims_until_admin_increases_capacity():
    worker_id, token = enroll("capacity-provider")
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    worker_headers = {"X-QingCloud-Worker-Token": token}
    first_job = client.post("/v1/jobs", headers=admin, json={"kind": "diagnostic"}).json()
    second_job = client.post("/v1/jobs", headers=admin, json={"kind": "echo"}).json()

    first = client.post(f"/v1/workers/{worker_id}/claim", headers=worker_headers)
    assert first.json()["job_id"] == first_job["job_id"]
    blocked = client.post(f"/v1/workers/{worker_id}/claim", headers=worker_headers)
    assert blocked.status_code == 200
    assert blocked.json() is None

    denied = client.put(
        f"/v1/workers/{worker_id}/capacity",
        json={"max_concurrent_jobs": 2},
    )
    assert denied.status_code == 401
    invalid = client.put(
        f"/v1/workers/{worker_id}/capacity",
        headers=admin,
        json={"max_concurrent_jobs": 17},
    )
    assert invalid.status_code == 422

    updated = client.put(
        f"/v1/workers/{worker_id}/capacity",
        headers=admin,
        json={"max_concurrent_jobs": 2},
    )
    assert updated.status_code == 200
    assert updated.json()["running_jobs"] == 1
    assert updated.json()["available_slots"] == 1
    second = client.post(f"/v1/workers/{worker_id}/claim", headers=worker_headers)
    assert second.json()["job_id"] == second_job["job_id"]


def test_provider_registry_reports_approval_and_capacity():
    worker_id, _ = enroll("registry-capacity")
    response = client.get(
        "/v1/providers",
        headers={"X-QingCloud-Admin-Token": "test-admin-secret"},
    )
    assert response.status_code == 200
    provider = next(item for item in response.json()["providers"] if item["worker_id"] == worker_id)
    assert provider["approval_status"] == "approved"
    assert provider["max_concurrent_jobs"] == 1
    assert provider["running_jobs"] == 0
    assert provider["available_slots"] == 1


def test_gpu_vram_is_reserved_per_device_until_job_finishes():
    worker_id, token = enroll("gpu-reservation-worker")
    headers = {"X-QingCloud-Worker-Token": token}
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    capacity = client.put(
        f"/v1/workers/{worker_id}/capacity", headers=admin,
        json={"max_concurrent_jobs": 3},
    )
    assert capacity.status_code == 200
    refreshed = client.put(
        f"/v1/workers/{worker_id}/capabilities",
        headers=headers,
        json={"name": "gpu-reservation-worker", "cpu_cores": 8, "ram_mb": 8192,
              "gpus": [
                  {"device_index": 0, "name": "GPU-A", "vram_mb": 12000, "free_vram_mb": 10000},
                  {"device_index": 1, "name": "GPU-B", "vram_mb": 16000, "free_vram_mb": 14000},
              ]},
    )
    assert refreshed.status_code == 200
    first = client.post("/v1/jobs", headers=admin, json={"kind": "diagnostic", "required_gpu_vram_mb": 8000}).json()
    second = client.post("/v1/jobs", headers=admin, json={"kind": "diagnostic", "required_gpu_vram_mb": 8000}).json()
    claim1 = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    claim2 = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    assert claim1["job_id"] == first["job_id"]
    assert claim2["job_id"] == second["job_id"]
    assert claim1["assigned_gpu_index"] == 1
    assert claim2["assigned_gpu_index"] == 0
    third = client.post("/v1/jobs", headers=admin, json={"kind": "diagnostic", "required_gpu_vram_mb": 8000}).json()
    assert client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json() is None
    completed = client.post(
        f"/v1/jobs/{claim1['job_id']}/complete?worker_id={worker_id}&attempt={claim1['attempts']}",
        headers=headers, json={"success": True, "result": {"ok": True}},
    )
    assert completed.status_code == 200
    retry_claim = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    assert retry_claim["job_id"] == third["job_id"]
    assert retry_claim["assigned_gpu_index"] == 1


def test_dashboard_exposes_live_gpu_reservations_and_per_job_assignment():
    worker_id, token = enroll("dashboard-gpu-worker")
    worker_headers = {"X-QingCloud-Worker-Token": token}
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    client.put(f"/v1/workers/{worker_id}/capacity", headers=admin, json={"max_concurrent_jobs": 2})
    refreshed = client.put(
        f"/v1/workers/{worker_id}/capabilities", headers=worker_headers,
        json={"name": "dashboard-gpu-worker", "cpu_cores": 4, "ram_mb": 8192,
              "gpus": [{"device_index": 0, "name": "GPU-D", "vram_mb": 12000, "free_vram_mb": 10000}]},
    )
    assert refreshed.status_code == 200
    created = client.post("/v1/jobs", headers=admin, json={"kind": "diagnostic", "required_gpu_vram_mb": 4000}).json()
    claimed = client.post(f"/v1/workers/{worker_id}/claim", headers=worker_headers).json()
    assert claimed["job_id"] == created["job_id"]
    data = client.get("/v1/dashboard", headers=admin).json()
    worker = next(item for item in data["workers"] if item["worker_id"] == worker_id)
    assert worker["reserved_gpu_vram_mb_total"] == 4000
    assert worker["gpus"][0]["reserved_vram_mb"] == 4000
    assert worker["gpus"][0]["scheduler_headroom_vram_mb"] == 6000
    job = next(item for item in data["jobs"] if item["job_id"] == created["job_id"])
    assert job["assigned_gpu_index"] == 0
    assert job["reserved_gpu_vram_mb"] == 4000


def test_gpu_provider_ranking_uses_headroom_after_existing_reservations():
    worker_a, token_a = enroll("reserved-provider")
    headers_a = {"X-QingCloud-Worker-Token": token_a}
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    capacity = client.put(
        f"/v1/workers/{worker_a}/capacity", headers=admin,
        json={"max_concurrent_jobs": 2},
    )
    assert capacity.status_code == 200
    refreshed_a = client.put(
        f"/v1/workers/{worker_a}/capabilities",
        headers=headers_a,
        json={"name": "reserved-provider", "cpu_cores": 8, "ram_mb": 8192,
              "gpus": [{"device_index": 0, "name": "GPU-A", "vram_mb": 12000, "free_vram_mb": 10000}]},
    )
    assert refreshed_a.status_code == 200

    first_job = client.post(
        "/v1/jobs", headers=admin,
        json={"kind": "diagnostic", "required_gpu_vram_mb": 8000},
    ).json()
    first_claim = client.post(f"/v1/workers/{worker_a}/claim", headers=headers_a).json()
    assert first_claim["job_id"] == first_job["job_id"]
    assert first_claim["reserved_gpu_vram_mb"] == 8000

    worker_b, token_b = enroll("available-provider")
    headers_b = {"X-QingCloud-Worker-Token": token_b}
    refreshed_b = client.put(
        f"/v1/workers/{worker_b}/capabilities",
        headers=headers_b,
        json={"name": "available-provider", "cpu_cores": 8, "ram_mb": 8192,
              "gpus": [{"device_index": 0, "name": "GPU-B", "vram_mb": 12000, "free_vram_mb": 9000}]},
    )
    assert refreshed_b.status_code == 200

    next_job = client.post(
        "/v1/jobs", headers=admin,
        json={"kind": "diagnostic", "required_gpu_vram_mb": 8000},
    ).json()

    # Provider A still reports 10 GB free, but QingCloud has reserved 8 GB.
    # Its effective 2 GB headroom must not beat provider B's 9 GB.
    blocked_a = client.post(f"/v1/workers/{worker_a}/claim", headers=headers_a)
    assert blocked_a.status_code == 200
    assert blocked_a.json() is None

    claim_b = client.post(f"/v1/workers/{worker_b}/claim", headers=headers_b)
    assert claim_b.status_code == 200
    assert claim_b.json()["job_id"] == next_job["job_id"]
    assert claim_b.json()["assigned_worker_id"] == worker_b


def test_expired_gpu_lease_is_recovered_and_reservation_persisted_as_released():
    worker_id, token = enroll("restart-recovery-provider")
    worker_headers = {"X-QingCloud-Worker-Token": token}
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    updated = client.put(
        f"/v1/workers/{worker_id}/capabilities",
        headers=worker_headers,
        json={"name": "restart-recovery-provider", "cpu_cores": 4, "ram_mb": 8192,
              "gpus": [{"device_index": 0, "name": "GPU-R", "vram_mb": 12000, "free_vram_mb": 10000}]},
    )
    assert updated.status_code == 200
    created = client.post(
        "/v1/jobs", headers=admin,
        json={"kind": "diagnostic", "required_gpu_vram_mb": 8000, "max_attempts": 2},
    ).json()
    claimed = client.post(f"/v1/workers/{worker_id}/claim", headers=worker_headers).json()
    assert claimed["job_id"] == created["job_id"]
    assert claimed["reserved_gpu_vram_mb"] == 8000

    # Simulate a coordinator restart after the worker lease has expired.
    from qingcloud.main import jobs
    jobs[created["job_id"]].updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    save_job(jobs[created["job_id"]])

    recovered = recover_expired_jobs_persisted(jobs, datetime.now(timezone.utc))
    assert len(recovered) == 1
    assert recovered[0].state.value == "queued"
    assert recovered[0].assigned_worker_id is None
    assert recovered[0].assigned_gpu_index is None
    assert recovered[0].reserved_gpu_vram_mb == 0

    persisted_jobs = {}
    load_state({}, persisted_jobs, {})
    persisted = persisted_jobs[created["job_id"]]
    assert persisted.state.value == "queued"
    assert persisted.reserved_gpu_vram_mb == 0
    assert persisted.assigned_gpu_index is None


def test_stale_worker_attempt_cannot_heartbeat_or_complete_reassigned_job():
    worker_id, token = enroll("fencing-worker")
    headers = {"X-QingCloud-Worker-Token": token}
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    created = client.post(
        "/v1/jobs", headers=admin,
        json={"kind": "echo", "payload": {"text": "fencing"}, "max_attempts": 3},
    ).json()
    first = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    assert first["attempts"] == 1

    # Expire attempt 1, then let the same worker reclaim it as attempt 2.
    from qingcloud.main import jobs
    jobs[created["job_id"]].updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    save_job(jobs[created["job_id"]])
    second = client.post(f"/v1/workers/{worker_id}/claim", headers=headers).json()
    assert second["job_id"] == created["job_id"]
    assert second["attempts"] == 2

    stale_heartbeat = client.post(
        f"/v1/jobs/{created['job_id']}/heartbeat?worker_id={worker_id}&attempt=1",
        headers=headers,
        json={"progress": 50, "message": "late attempt"},
    )
    assert stale_heartbeat.status_code == 409

    stale_completion = client.post(
        f"/v1/jobs/{created['job_id']}/complete?worker_id={worker_id}&attempt=1",
        headers=headers,
        json={"success": True, "result": {"from": "stale attempt"}},
    )
    assert stale_completion.status_code == 409
    assert jobs[created["job_id"]].state.value == "running"
    assert jobs[created["job_id"]].attempts == 2

    current_completion = client.post(
        f"/v1/jobs/{created['job_id']}/complete?worker_id={worker_id}&attempt=2",
        headers=headers,
        json={"success": True, "result": {"from": "current attempt"}},
    )
    assert current_completion.status_code == 200
    assert current_completion.json()["state"] == "succeeded"
    assert current_completion.json()["result"]["from"] == "current attempt"


def test_worker_resource_telemetry_is_authenticated_persisted_and_admin_readable():
    worker_id, token = enroll("telemetry-worker")
    snapshot = {
        "cpu_percent": 42.5,
        "ram_total_mb": 8192,
        "ram_used_mb": 4096,
        "ram_available_mb": 4096,
        "ram_percent": 50.0,
        "gpus": [{"device_index": 0, "name": "NVIDIA T4", "utilization_percent": 65.0,
                  "vram_mb": 15360, "free_vram_mb": 9000}],
    }
    denied = client.post(f"/v1/workers/{worker_id}/telemetry", json=snapshot)
    assert denied.status_code == 401
    accepted = client.post(
        f"/v1/workers/{worker_id}/telemetry",
        headers={"X-QingCloud-Worker-Token": token},
        json=snapshot,
    )
    assert accepted.status_code == 200
    assert accepted.json()["cpu_percent"] == 42.5
    admin = {"X-QingCloud-Admin-Token": "test-admin-secret"}
    history = client.get(f"/v1/workers/{worker_id}/telemetry?limit=10", headers=admin)
    assert history.status_code == 200
    assert history.json()["total"] == 1
    assert history.json()["samples"][0]["gpus"][0]["utilization_percent"] == 65.0
    dashboard = client.get("/v1/dashboard", headers=admin)
    row = next(item for item in dashboard.json()["workers"] if item["worker_id"] == worker_id)
    assert row["resource_telemetry"]["ram_percent"] == 50.0


def test_worker_resource_telemetry_rejects_impossible_ram_snapshot():
    worker_id, token = enroll("bad-telemetry-worker")
    response = client.post(
        f"/v1/workers/{worker_id}/telemetry",
        headers={"X-QingCloud-Worker-Token": token},
        json={"cpu_percent": 20, "ram_total_mb": 1000, "ram_used_mb": 900,
              "ram_available_mb": 900, "ram_percent": 90, "gpus": []},
    )
    assert response.status_code == 422


def test_resource_telemetry_history_requires_admin_token():
    worker_id, _ = enroll("private-telemetry-worker")
    assert client.get(f"/v1/workers/{worker_id}/telemetry").status_code in (401, 503)
