"""Dispatch queued trip jobs to the planner worker."""
import logging
import os
from time import sleep

import httpx
import psycopg

from .jobs import JobError, JobStore
from .models import Plan

logger = logging.getLogger(__name__)


def dispatch(client: httpx.Client, worker_url: str, trip: dict) -> Plan | JobError:
    try:
        response = client.post(f"{worker_url}/plan", json=trip)
    except httpx.TimeoutException:
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


def main():
    logging.basicConfig(level=logging.INFO)
    store = JobStore()
    worker_url = os.environ["WORKER_URL"].rstrip("/")
    # Allow time to persist the outcome after the worker's HTTP timeout.
    with httpx.Client(timeout=httpx.Timeout(30, connect=5)) as client:
        while True:
            try:
                job = store.claim(attempt_seconds=45)
                if job is None:
                    sleep(0.5)
                    continue
                outcome = dispatch(client, worker_url, job["trip"])
                if store.finish(job["id"], outcome):
                    logger.info("Finished trip %s with %s", job["id"], outcome.status)
                else:
                    logger.warning("Trip %s exceeded its attempt deadline", job["id"])
            except psycopg.Error as exc:
                logger.error("Trip database operation failed (%s)", type(exc).__name__)
                sleep(2)


if __name__ == "__main__":
    main()
