"""Dispatch queued trip jobs to the planner worker."""
import asyncio
import logging
import os
import socket

import httpx
import psycopg

from .jobs import JobError, JobStore
from .models import Plan

logger = logging.getLogger(__name__)


async def dispatch(client: httpx.AsyncClient, worker_url: str, trip: dict, seconds: float) -> Plan | JobError:
    try:
        response = await asyncio.wait_for(client.post(f"{worker_url}/plan", json=trip), timeout=seconds)
    except (TimeoutError, httpx.TimeoutException):
        return JobError(status="worker_timeout")
    except httpx.RequestError:
        return JobError(status="worker_unavailable")

    try:
        payload = response.json()
        if response.status_code == 200:
            return Plan.model_validate(payload)
        if isinstance(payload, dict) and isinstance(payload.get("detail"), dict):
            status = payload["detail"].get("status")
            if isinstance(status, str) and status.isidentifier() and len(status) <= 80:
                return JobError(status=status, upstream_http_status=response.status_code)
    except ValueError:
        pass
    return JobError(status="invalid_worker_response", upstream_http_status=response.status_code)


async def worker_urls() -> list[str]:
    host = os.getenv("WORKER_DISCOVERY_HOST")
    if not host:
        urls = os.getenv("WORKER_URLS") or os.environ["WORKER_URL"]
        return [url.strip().rstrip("/") for url in urls.split(",") if url.strip()]
    # A headless Service returns the addresses of individual ready pods.
    # Sending both tries to the load-balanced Service could hit the same pod.
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, 8000, type=socket.SOCK_STREAM), timeout=2
        )
    except (socket.gaierror, TimeoutError):
        return []
    hosts = {address[4][0] for address in addresses}
    return [f"http://[{host}]:8000" if ":" in host else f"http://{host}:8000" for host in sorted(hosts)]


async def probe(client: httpx.AsyncClient, url: str) -> tuple[str, str, str] | None:
    try:
        response = await asyncio.wait_for(client.get(f"{url}/health/ready"), timeout=2)
        response.raise_for_status()
        data = response.json()
    except (TimeoutError, httpx.HTTPError, ValueError):
        return None
    if isinstance(data, dict) and data.get("status") == "ok":
        worker_id, node = data.get("worker_id"), data.get("node")
        if isinstance(worker_id, str) and worker_id and isinstance(node, str):
            return worker_id, node, url
    return None


async def run():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    store = JobStore()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5), trust_env=False) as client:
        while True:
            try:
                if not store.has_queued_jobs():
                    await asyncio.sleep(1)
                    continue
                observations = await asyncio.gather(*(probe(client, url) for url in await worker_urls()))
                workers = {worker_id: (node, url) for worker_id, node, url in filter(None, observations)}
                job = store.claim({worker_id: node for worker_id, (node, _) in workers.items()})
                if job is None:
                    await asyncio.sleep(1)
                    continue
                _, worker_url = workers[job["worker_id"]]
                outcome = await dispatch(client, worker_url, job["trip"], job["attempt_seconds"])
                status = store.finish(job["id"], job["attempt_id"], outcome)
                if status:
                    logger.info("Trip %s attempt %s on worker %s: %s (%s)",
                                job["id"], job["attempt_id"], job["worker_id"], status, outcome.status)
                else:
                    logger.warning("Discarded expired attempt %s for trip %s", job["attempt_id"], job["id"])
            except psycopg.Error as exc:
                logger.error("Trip database operation failed (%s)", type(exc).__name__)
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run())
