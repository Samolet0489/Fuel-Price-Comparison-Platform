"""Public, asynchronous trip-job API."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .jobs import JobReceipt, JobStore, JobView
from .models import StationCatalogue, Trip

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.jobs = JobStore()
    app.state.jobs.initialize()
    worker = os.getenv("WORKER_URL", "http://127.0.0.1:8000")
    with httpx.Client(base_url=worker, timeout=5) as client:
        app.state.worker = client
        yield


app = FastAPI(title="Austria fuel-stop trip API", lifespan=lifespan)
web = Path(__file__).resolve().parents[1] / "web"
app.mount("/assets", StaticFiles(directory=web), name="assets")


@app.get("/", include_in_schema=False)
def trip_map():
    return FileResponse(web / "index.html")


@app.exception_handler(psycopg.Error)
async def database_unavailable(request, exc):
    logger.error("Trip database operation failed (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": {"status": "database_unavailable"}})


@app.get("/health/ready")
def ready():
    with app.state.jobs.connect() as db:
        db.execute("SELECT id FROM trip_jobs LIMIT 1")
    return {"status": "ok"}


@app.get("/stations", response_model=StationCatalogue)
def list_stations():
    try:
        response = app.state.worker.get("/stations")
        response.raise_for_status()
        return StationCatalogue.model_validate(response.json())
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, detail={"status": "station_catalogue_unavailable"}) from None


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
