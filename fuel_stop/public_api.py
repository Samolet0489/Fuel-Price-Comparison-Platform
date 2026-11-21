"""Public, asynchronous trip-job API."""
import logging
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse

from .jobs import JobReceipt, JobStore, JobView
from .models import Trip

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.jobs = JobStore()
    app.state.jobs.initialize()
    yield


app = FastAPI(title="Austria fuel-stop trip API", lifespan=lifespan)


@app.exception_handler(psycopg.Error)
async def database_unavailable(request, exc):
    logger.error("Trip database operation failed (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": {"status": "database_unavailable"}})


@app.get("/health/ready")
def ready():
    with app.state.jobs.connect() as db:
        db.execute("SELECT id FROM trip_jobs LIMIT 1")
    return {"status": "ok"}


@app.post("/trip-plans", response_model=JobReceipt, status_code=202)
def submit_trip(trip: Trip, response: Response):
    receipt = app.state.jobs.submit(trip)
    response.headers["Location"] = f"/trip-plans/{receipt.id}"
    return receipt


@app.get("/trip-plans/{job_id}", response_model=JobView)
def get_trip(job_id: UUID):
    job = app.state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, detail={"status": "job_not_found"})
    return job
