import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg", exc_type=ImportError)
from psycopg import sql
from psycopg.conninfo import make_conninfo

from fuel_stop.controller import dispatch, probe
from fuel_stop.jobs import JobError, JobStore
from fuel_stop.models import Plan, Trip
from fuel_stop.public_api import app
from tests.test_providers import TRIP


@pytest.fixture
def store(monkeypatch):
    dsn = os.getenv("FUEL_STOP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set FUEL_STOP_TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_jobs_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            monkeypatch.setenv("DATABASE_URL", make_conninfo(dsn, options=f"-c search_path={schema}"))
            store = JobStore()
            store.initialize()
            yield store
        finally:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_retry_selects_another_ready_worker_and_ignores_late_reply(store):
    plan = Plan(status="direct", mode="live", message="Route found")
    receipt = store.submit(Trip.model_validate(TRIP))
    dispatched = []

    def worker(request):
        worker_id = request.url.host
        if request.url.path == "/health/ready":
            ready = worker_id != "worker-c"
            return httpx.Response(200 if ready else 503, json={
                "status": "ok" if ready else "not_ready", "worker_id": worker_id, "node": f"node-{worker_id}",
            })
        dispatched.append(worker_id)
        if worker_id == "worker-a":
            return httpx.Response(503, json={"detail": {"status": "routing_unavailable"}})
        return httpx.Response(200, json=plan.model_dump(mode="json"))

    async def verify():
        async with httpx.AsyncClient(transport=httpx.MockTransport(worker)) as client:
            urls = ["http://worker-a", "http://worker-b", "http://worker-c"]
            observations = await asyncio.gather(*(probe(client, url) for url in urls))
            workers = {worker_id: (node, url) for worker_id, node, url in filter(None, observations)}
            ready_nodes = {worker_id: node for worker_id, (node, _) in workers.items()}
            assert list(ready_nodes) == ["worker-a", "worker-b"]
            first = store.claim(ready_nodes)
            assert first["id"] == receipt.id
            failure = await dispatch(client, workers[first["worker_id"]][1], first["trip"], first["attempt_seconds"])
            assert store.finish(receipt.id, first["attempt_id"], failure) == "QUEUED"
            assert store.claim({"worker-a": "node-a"}) is None

            second = store.claim(ready_nodes)
            assert second["id"] == receipt.id
            assert second["attempt_id"] != first["attempt_id"]
            assert store.finish(receipt.id, first["attempt_id"], plan) is None
            success = await dispatch(client, workers[second["worker_id"]][1], second["trip"], second["attempt_seconds"])
            assert store.finish(receipt.id, second["attempt_id"], success) == "SUCCEEDED"
            assert store.finish(receipt.id, first["attempt_id"], failure) is None

    asyncio.run(verify())
    job = store.get(receipt.id)
    assert dispatched == ["worker-a", "worker-b"]
    assert job.status == "SUCCEEDED" and job.result.status == "direct"
    assert (job.attempts, job.worker_id, job.worker_node) == (2, "worker-b", "node-worker-b")


def test_expired_attempt_is_requeued_and_attempt_count_is_bounded(store):
    receipt = store.submit(Trip.model_validate(TRIP))
    workers = {"first": "a", "second": "b"}
    first = store.claim(workers)
    with store.connect() as db:
        db.execute("UPDATE trip_jobs SET attempt_deadline = now() WHERE id = %s", (receipt.id,))
    assert store.get(receipt.id).status == "QUEUED"
    second = store.claim(workers)
    assert second["worker_id"] == "second" and second["attempt_id"] != first["attempt_id"]
    assert store.finish(receipt.id, second["attempt_id"], JobError(status="worker_unavailable")) == "FAILED"
    assert store.claim({"third": "c"}) is None


def test_overall_deadline_applies_without_a_worker(store):
    receipt = store.submit(Trip.model_validate(TRIP))
    with store.connect() as db:
        db.execute("UPDATE trip_jobs SET deadline_at = now() WHERE id = %s", (receipt.id,))
    assert store.claim({"worker": "node"}) is None
    job = store.get(receipt.id)
    assert job.attempts == 0
    assert job.status == "FAILED"
    assert job.error.status == "job_deadline_exceeded"


def test_concurrent_claim_and_non_retryable_worker_error(store):
    receipt = store.submit(Trip.model_validate(TRIP))
    with ThreadPoolExecutor(max_workers=2) as controllers:
        claims = list(controllers.map(store.claim, [{"one": "a"}, {"two": "b"}]))
    claimed, = [job for job in claims if job]
    error = JobError(status="outside_austria", upstream_http_status=422)
    assert store.finish(receipt.id, claimed["attempt_id"], error) == "FAILED"
    assert store.get(receipt.id).error == error
    assert store.claim({"other": "another-node"}) is None


def test_regeneration_updates_all_prices_and_persists_across_api_restart(store, monkeypatch):
    monkeypatch.delenv("PRICE_MODE", raising=False)
    catalogue = {
        "source": "test", "data_timestamp": "2026-11-01T00:00:00Z",
        "stations": [{"id": "node/42", "name": "Test", "point": {"lon": 16, "lat": 48},
                      "fuel_types": ["diesel", "petrol95"], "opening_hours": "24/7"}],
    }
    with httpx.Client(base_url="http://worker.test", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=catalogue))) as worker:
        with TestClient(app) as client:
            app.state.worker = worker
            before = client.get("/stations").raise_for_status().json()["generated_prices"]["node/42"]
            assert client.post("/fuel-prices/regenerate").status_code == 415
            assert client.get("/fuel-prices/revision").json() == {"revision": 0}
            assert client.post("/fuel-prices/regenerate", json={}).raise_for_status().json() == {"revision": 1}
            after = client.get("/stations").raise_for_status().json()["generated_prices"]["node/42"]
            assert all(before[fuel] != after[fuel] for fuel in before)
        with TestClient(app) as restarted:
            app.state.worker = worker
            assert restarted.get("/stations").raise_for_status().json()["generated_prices"]["node/42"] == after
    with ThreadPoolExecutor(max_workers=2) as users:
        revisions = list(users.map(lambda _: store.regenerate_prices(), range(2)))
    assert sorted(revisions) == [2, 3]
