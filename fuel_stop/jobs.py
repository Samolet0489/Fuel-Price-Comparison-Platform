"""PostgreSQL storage shared by the public API and the job controller."""
import os
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .models import Model, Plan, Trip

JobStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]


class JobError(Model):
    status: str
    upstream_http_status: int | None = None


class JobReceipt(Model):
    id: UUID
    status: Literal["QUEUED"] = "QUEUED"


class JobView(Model):
    id: UUID
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: Plan | None
    error: JobError | None


class JobStore:
    def __init__(self):
        self.dsn = os.getenv("DATABASE_URL", "")

    def connect(self):
        return psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row)

    def initialize(self):
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS trip_jobs (
                    id uuid PRIMARY KEY,
                    status text NOT NULL DEFAULT 'QUEUED'
                        CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
                    trip jsonb NOT NULL,
                    result jsonb,
                    error jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    started_at timestamptz,
                    attempt_deadline timestamptz,
                    finished_at timestamptz
                )
                """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS trip_jobs_pending
                ON trip_jobs (created_at, id) WHERE status = 'QUEUED'
                """)

    def submit(self, trip: Trip) -> JobReceipt:
        receipt = JobReceipt(id=uuid4())
        with self.connect() as db:
            db.execute("INSERT INTO trip_jobs (id, trip) VALUES (%s, %s)",
                       (receipt.id, Jsonb(trip.model_dump(mode="json"))))
        return receipt

    def get(self, job_id: UUID) -> JobView | None:
        with self.connect() as db:
            row = db.execute("""
                SELECT id, status, created_at, started_at, finished_at, result, error
                FROM trip_jobs WHERE id = %s
                """, (job_id,)).fetchone()
        return JobView.model_validate(row) if row is not None else None

    def claim(self, attempt_seconds: int):
        with self.connect() as db:
            # A stopped controller leaves its claim behind. Expire it visibly
            # instead of leaving it RUNNING forever or dispatching it twice.
            db.execute("""
                UPDATE trip_jobs SET status = 'FAILED', finished_at = now(), error = %s
                WHERE status = 'RUNNING' AND attempt_deadline <= now()
                """, (Jsonb({"status": "attempt_expired"}),))
            return db.execute("""
                WITH next_job AS (
                    SELECT id FROM trip_jobs WHERE status = 'QUEUED'
                    ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
                )
                UPDATE trip_jobs AS job SET status = 'RUNNING', started_at = now(),
                    attempt_deadline = now() + %s * interval '1 second'
                FROM next_job WHERE job.id = next_job.id
                RETURNING job.id, job.trip
                """, (attempt_seconds,)).fetchone()

    def finish(self, job_id: UUID, outcome: Plan | JobError) -> bool:
        payload = Jsonb(outcome.model_dump(mode="json"))
        with self.connect() as db:
            completed = db.execute("""
                UPDATE trip_jobs SET status = %s, result = %s, error = %s, finished_at = now()
                WHERE id = %s AND status = 'RUNNING' AND attempt_deadline > now()
                """, ("SUCCEEDED", payload, None, job_id) if isinstance(outcome, Plan)
                else ("FAILED", None, payload, job_id))
        return completed.rowcount == 1
