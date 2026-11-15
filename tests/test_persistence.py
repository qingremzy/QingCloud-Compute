from qingcloud.db import claim_job_atomic, clear_all, load_state, save_job, save_worker, save_worker_token
from qingcloud.models import JobRecord, WorkerRecord, WorkerState


def test_workers_jobs_and_token_hashes_survive_state_reload():
    clear_all()
    workers = {"w-1": WorkerRecord(worker_id="w-1", name="persist-me")}
    jobs = {"j-1": JobRecord(job_id="j-1", kind="diagnostic")}
    tokens = {"w-1": "hashed-secret-value"}

    save_worker(workers["w-1"])
    save_job(jobs["j-1"])
    save_worker_token("w-1", tokens["w-1"])

    restored_workers = {}
    restored_jobs = {}
    restored_tokens = {}
    load_state(restored_workers, restored_jobs, restored_tokens)

    assert restored_workers["w-1"].name == "persist-me"
    assert restored_jobs["j-1"].state.value == "queued"
    assert restored_tokens["w-1"] == "hashed-secret-value"
    clear_all()



def test_atomic_claim_is_persisted_and_stale_second_process_cannot_claim_same_job():
    clear_all()
    save_job(JobRecord(job_id="atomic-job", kind="diagnostic"))
    worker_a = WorkerRecord(worker_id="worker-a", name="worker-a", state=WorkerState.online)
    worker_b = WorkerRecord(worker_id="worker-b", name="worker-b", state=WorkerState.online)
    process_a_jobs = {}
    process_b_jobs = {}

    first = claim_job_atomic(worker_a, process_a_jobs)
    second = claim_job_atomic(worker_b, process_b_jobs)

    assert first is not None
    assert first.job_id == "atomic-job"
    assert first.assigned_worker_id == "worker-a"
    assert second is None

    restored_jobs = {}
    load_state({}, restored_jobs, {})
    assert restored_jobs["atomic-job"].state.value == "running"
    assert restored_jobs["atomic-job"].assigned_worker_id == "worker-a"
    clear_all()
