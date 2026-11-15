from qingcloud.db import clear_all, load_state, save_job, save_worker, save_worker_token
from qingcloud.models import JobRecord, WorkerRecord


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
