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
JOB_SECONDS = 90
ATTEMPT_SECONDS = 30
MAX_ATTEMPTS = 2


class JobError(Model):
    status: str
    upstream_http_status: int | None = None

    @property
    def retryable(self) -> bool:
        code = self.upstream_http_status
        return (self.status in {"worker_timeout", "worker_unavailable"}
                or code in (200, 429) or (code is not None and 500 <= code < 600))


class JobReceipt(Model):
    id: UUID
    status: Literal["QUEUED"] = "QUEUED"


class JobView(Model):
    id: UUID
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline_at: datetime
    attempts: int
    attempt_id: UUID | None
    worker_id: str | None
    worker_node: str | None
    result: Plan | None
    error: JobError | None


class JobStore:
    def __init__(self):
        self.dsn = os.getenv("DATABASE_URL", "")

    def connect(self):
        return psycopg.connect(self.dsn, connect_timeout=5, row_factory=dict_row)

    def initialize(self):
        with self.connect() as db:
            # Serialize startup migrations when two API instances overlap.
            db.execute("SELECT pg_advisory_xact_lock(hashtext('fuel_stop_trip_jobs_schema'))")
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
            db.execute("""
                ALTER TABLE trip_jobs
                    ADD COLUMN IF NOT EXISTS deadline_at timestamptz,
                    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS attempt_id uuid,
                    ADD COLUMN IF NOT EXISTS worker_id text,
                    ADD COLUMN IF NOT EXISTS worker_node text
                """)
            db.execute("""
                UPDATE trip_jobs SET deadline_at = created_at + %s * interval '1 second',
                    attempts = CASE WHEN started_at IS NULL THEN 0 ELSE 1 END
                WHERE deadline_at IS NULL
                """, (JOB_SECONDS,))
            db.execute("ALTER TABLE trip_jobs ALTER COLUMN deadline_at SET NOT NULL")
            db.execute("""
                CREATE INDEX IF NOT EXISTS trip_jobs_active_deadline ON trip_jobs (deadline_at)
                WHERE status IN ('QUEUED', 'RUNNING')
                """)

    def submit(self, trip: Trip) -> JobReceipt:
        receipt = JobReceipt(id=uuid4())
        with self.connect() as db:
            db.execute("""
                INSERT INTO trip_jobs (id, trip, deadline_at)
                VALUES (%s, %s, now() + %s * interval '1 second')
                """, (receipt.id, Jsonb(trip.model_dump(mode="json")), JOB_SECONDS))
        return receipt

    def get(self, job_id: UUID) -> JobView | None:
        with self.connect() as db:
            self.expire(db, job_id)
            row = db.execute("""
                SELECT id, status, created_at, started_at, finished_at, deadline_at,
                    attempts, attempt_id, worker_id, worker_node, result, error
                FROM trip_jobs WHERE id = %s
                """, (job_id,)).fetchone()
        return JobView.model_validate(row) if row is not None else None

    def expire(self, db, job_id: UUID | None = None):
        db.execute("""
            WITH expired AS (
                SELECT id, CASE WHEN deadline_at <= now() THEN 'job_deadline_exceeded'
                               ELSE 'attempt_expired' END AS reason,
                    deadline_at > now() AND attempt_id IS NOT NULL AND attempts < %s AS can_retry
                FROM trip_jobs
                WHERE (%s::uuid IS NULL OR id = %s)
                    AND status IN ('QUEUED', 'RUNNING')
                    AND (deadline_at <= now() OR (status = 'RUNNING' AND attempt_deadline <= now()))
                FOR UPDATE SKIP LOCKED
            )
            UPDATE trip_jobs AS job
            SET status = CASE WHEN expired.can_retry THEN 'QUEUED' ELSE 'FAILED' END,
                finished_at = CASE WHEN expired.can_retry THEN NULL ELSE now() END,
                error = CASE WHEN expired.can_retry THEN NULL ELSE jsonb_build_object('status', expired.reason) END
            FROM expired WHERE job.id = expired.id
            """, (MAX_ATTEMPTS, job_id, job_id))

    def has_queued_jobs(self) -> bool:
        with self.connect() as db:
            self.expire(db)
            return db.execute("""
                SELECT EXISTS (SELECT 1 FROM trip_jobs WHERE status = 'QUEUED') AS queued
                """).fetchone()["queued"]

    def claim(self, workers: dict[str, str]):
        with self.connect() as db:
            self.expire(db)
            if not workers:
                return None
            return db.execute("""
                WITH next_job AS (
                    SELECT queued.id, picked.worker_id, picked.node
                    FROM trip_jobs AS queued
                    CROSS JOIN LATERAL (
                        SELECT worker_id, node FROM unnest(%s::text[], %s::text[]) AS ready(worker_id, node)
                        WHERE ready.worker_id IS DISTINCT FROM queued.worker_id
                        LIMIT 1
                    ) AS picked
                    WHERE queued.status = 'QUEUED' AND queued.attempts < %s
                        AND queued.deadline_at > now()
                    ORDER BY queued.created_at, queued.id LIMIT 1 FOR UPDATE OF queued SKIP LOCKED
                )
                UPDATE trip_jobs AS job SET status = 'RUNNING', started_at = COALESCE(job.started_at, now()),
                    attempts = job.attempts + 1, attempt_id = %s,
                    attempt_deadline = least(job.deadline_at, now() + %s * interval '1 second'),
                    worker_id = next_job.worker_id, worker_node = next_job.node
                FROM next_job WHERE job.id = next_job.id
                RETURNING job.id, job.trip, job.attempt_id, job.worker_id,
                    extract(epoch FROM job.attempt_deadline - clock_timestamp())::float AS attempt_seconds
                """, (list(workers), list(workers.values()), MAX_ATTEMPTS, uuid4(), ATTEMPT_SECONDS)).fetchone()

    def finish(self, job_id: UUID, attempt_id: UUID, outcome: Plan | JobError) -> JobStatus | None:
        payload = Jsonb(outcome.model_dump(mode="json"))
        with self.connect() as db:
            success = isinstance(outcome, Plan)
            retryable = isinstance(outcome, JobError) and outcome.retryable
            completed = db.execute("""
                WITH active AS (
                    SELECT id, %s AND attempts < %s AS can_retry FROM trip_jobs
                    WHERE id = %s AND attempt_id = %s AND status = 'RUNNING'
                        AND attempt_deadline > clock_timestamp()
                    FOR UPDATE
                )
                UPDATE trip_jobs AS job SET
                    status = CASE WHEN active.can_retry THEN 'QUEUED' ELSE %s END,
                    result = %s::jsonb, error = CASE WHEN active.can_retry THEN NULL ELSE %s::jsonb END,
                    finished_at = CASE WHEN active.can_retry THEN NULL ELSE now() END
                FROM active WHERE job.id = active.id AND job.attempt_deadline > clock_timestamp()
                RETURNING job.status
                """, (retryable, MAX_ATTEMPTS, job_id, attempt_id, "SUCCEEDED" if success else "FAILED",
                      payload if success else None, None if success else payload)).fetchone()
        return completed["status"] if completed else None
