"""Public, asynchronous trip-job API."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .generated_prices import generated_price, generated_prices_enabled
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


@app.get("/stations", response_model=StationCatalogue, response_model_exclude_none=True)
def list_stations():
    try:
        response = app.state.worker.get("/stations")
        response.raise_for_status()
        catalogue = StationCatalogue.model_validate(response.json())
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, detail={"status": "station_catalogue_unavailable"}) from None
    if generated_prices_enabled():
        revision = app.state.jobs.price_revision()
        catalogue.generated_prices = {
            station.id: {fuel: generated_price(station.id, fuel, revision) for fuel in station.fuel_types}
            for station in catalogue.stations
        }
    return catalogue


@app.get("/fuel-prices/revision")
def price_revision() -> dict[str, int]:
    return {"revision": app.state.jobs.price_revision()}


@app.post("/fuel-prices/regenerate")
def regenerate_prices(request: Request) -> dict[str, int]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, detail={"status": "json_required"})
    if not generated_prices_enabled():
        raise HTTPException(409, detail={"status": "generated_prices_disabled"})
    return {"revision": app.state.jobs.regenerate_prices()}


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
