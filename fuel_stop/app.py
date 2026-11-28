import os
import socket
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .models import Plan, StationCatalogue, Trip
from .boundary import AustriaBoundary
from .generated_prices import generated_prices_enabled
from .planner import BorderRestrictedRoute, DependencyUnavailable, NoRoute, OutsideAustria, plan
from .providers import (LOCAL_PRICE_FILENAME, LOCAL_PRICE_SOURCE, AuthorisedPrices, GeneratedPrices,
                        OSRMRouter, OSMStations)
from .stations import LocalStations

PLANS = Counter("fuel_plans_total", "Planning outcomes", ["status"])
LATENCY = Histogram("fuel_plan_seconds", "Complete planning latency")
DEFAULT_STATION_CATALOGUE = "data/osrm/stations.json"


@asynccontextmanager
async def lifespan(app):
    # Per-request timeout plus sequential bounded station evaluation. A future
    # dispatcher should apply its own whole-job deadline; this is synchronous v1.
    with httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False) as client:
        app.state.client = client
        yield


app = FastAPI(title="Austrian fuel-stop worker", version="0.2.0", lifespan=lifespan)


@lru_cache(maxsize=2)
def load_boundary(path):
    return AustriaBoundary.load(path)


def boundary():
    return load_boundary(os.getenv("AUSTRIA_BOUNDARY_FILE", "data/osrm/austria-boundary.geojson"))


def local_stations():
    return LocalStations(os.getenv("STATION_CATALOGUE_FILE", DEFAULT_STATION_CATALOGUE), boundary())


def station_provider(client):
    source = os.getenv("STATION_SOURCE", "local")
    if source == "local":
        return local_stations()
    if source == "overpass":
        return OSMStations(client, os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"))
    raise DependencyUnavailable("stations", "invalid_station_source")


def price_provider(client):
    if generated_prices_enabled():
        return GeneratedPrices(lambda: local_stations().load(), client, os.getenv("PRICE_REVISION_URL", ""))
    url = os.getenv("PRICE_FEED_URL", "")
    snapshot = os.getenv("PRICE_SNAPSHOT_FILE", "")
    if not url and not snapshot:
        local_file = Path(os.getenv("STATION_CATALOGUE_FILE", DEFAULT_STATION_CATALOGUE)).with_name(LOCAL_PRICE_FILENAME)
        if local_file.is_file():
            return AuthorisedPrices(client, "", "local-observations", LOCAL_PRICE_SOURCE, snapshot_file=str(local_file))
    return AuthorisedPrices(client, url, os.getenv("PRICE_PERMISSION_REFERENCE", ""),
                            os.getenv("PRICE_SOURCE", ""), os.getenv("PRICE_FEED_TOKEN", ""), snapshot)


@app.get("/health/live")
def live():
    return {"status": "ok", "worker_id": socket.gethostname(), "node": os.getenv("NODE_NAME", "local")}


@app.get("/health/ready")
def ready(response: Response):
    checks = {"boundary_ready": False, "stations_ready": False, "routing_ready": False}
    routing_url = os.getenv("OSRM_BASE_URL", "http://127.0.0.1:5000")
    client = app.state.client
    try:
        country = boundary()
        checks["boundary_ready"] = True
        source = station_provider(client)
        if isinstance(source, LocalStations):
            source.load()
        else:
            source.check_ready()
        checks["stations_ready"] = True
        OSRMRouter(client, routing_url, country).check_ready()
        checks["routing_ready"] = True
    except DependencyUnavailable as exc:
        response.status_code = 503
        checks["dependency"] = exc.dependency
    return {**live(), "status": "not_ready" if response.status_code == 503 else "ok",
            "routing_provider": "osrm", **checks,
            "station_source": os.getenv("STATION_SOURCE", "local"),
            "routing_url": routing_url,
            "prices_configured": price_provider(client).configured}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})


@app.get("/stations", response_model=StationCatalogue, response_model_exclude_none=True)
def list_stations():
    try:
        catalogue = local_stations()
        stations = catalogue.load()
    except DependencyUnavailable as exc:
        raise HTTPException(503, detail={"status": f"{exc.dependency}_unavailable", "reason": exc.reason}) from None
    return StationCatalogue(source=catalogue.source, data_timestamp=catalogue.data_timestamp, stations=stations)


@app.post("/plan", response_model=Plan)
def create_plan(trip: Trip):
    client = app.state.client
    prices = price_provider(client)
    with LATENCY.time():
        try:
            router = OSRMRouter(client, os.getenv("OSRM_BASE_URL", "http://127.0.0.1:5000"), boundary())
            stations = station_provider(client)
            result = plan(trip, router, stations, prices)
            PLANS.labels(result.status).inc()
            return result
        except OutsideAustria:
            PLANS.labels("outside_austria").inc()
            raise HTTPException(422, detail={"status": "outside_austria"}) from None
        except BorderRestrictedRoute:
            PLANS.labels("border_restricted_route").inc()
            raise HTTPException(422, detail={"status": "border_restricted_route",
                                "message": "OSRM route leaves Austria; a different domestic route may exist"}) from None
        except NoRoute:
            PLANS.labels("no_route").inc()
            raise HTTPException(422, detail={"status": "no_route"}) from None
        except DependencyUnavailable as exc:
            status = f"{exc.dependency}_unavailable"
            PLANS.labels(status).inc()
            detail = {"status": status}
            if exc.reason:
                detail["reason"] = exc.reason
            raise HTTPException(503, detail=detail) from None
