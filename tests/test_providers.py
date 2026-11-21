import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from fuel_stop.app import app
from fuel_stop.boundary import AustriaBoundary
from fuel_stop.models import Point, Trip
from fuel_stop.planner import DependencyUnavailable, NoRoute, OutsideAustria
from fuel_stop.providers import AuthorisedPrices, OSRMRouter, OSMStations
from shapely.geometry import box

TRIP = json.loads((Path(__file__).parent / "fixtures/trips.json").read_text())["trip"]


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def osrm_response(points, legs=None):
    legs = legs or [12345] * (len(points) - 1)
    return {"code": "Ok", "routes": [{"distance": sum(legs), "duration": 600,
        "legs": [{"distance": d, "duration": 600 / len(legs)} for d in legs],
        "geometry": {"type": "LineString", "coordinates": points}}],
        "waypoints": [{"location": p, "distance": 0} for p in points]}


def router(http):
    return OSRMRouter(http, "http://osrm.test:5000", AustriaBoundary(box(9, 46, 18, 50)))


def test_osrm_contract_and_units():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/route/v1/driving/16.3,48.2;15.4,47.0"
        assert request.url.params["overview"] == "full"
        assert request.url.params["geometries"] == "geojson"
        assert request.url.params["radiuses"] == "100;100"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=osrm_response([[16.3, 48.2], [15.4, 47.0]]))
    with client(handler) as http:
        route = router(http).route([Point(lon=16.3, lat=48.2), Point(lon=15.4, lat=47)])
    assert str(route.distance_km) == "12.345"
    assert route.duration_s == 600


@pytest.mark.parametrize("status,data,error", [
    (429, {}, DependencyUnavailable), (503, {}, DependencyUnavailable),
    (200, {"routes": []}, DependencyUnavailable), (200, [], DependencyUnavailable),
    (400, {"code": "NoRoute"}, NoRoute), (400, {"code": "NoSegment"}, NoRoute),
    (400, {"code": "InvalidQuery"}, DependencyUnavailable),
])
def test_osrm_failures(status, data, error):
    with client(lambda r: httpx.Response(status, json=data)) as http:
        with pytest.raises(error):
            router(http).route([Point(lon=16, lat=48), Point(lon=15, lat=47)])


def test_country_validation_rejects_outside():
    with client(lambda r: pytest.fail("Country checks must be local")) as http:
        with pytest.raises(OutsideAustria):
            router(http).validate_trip(Trip.model_validate({**TRIP, "origin": {"lon": 2.35, "lat": 48.85}}))


def test_osm_mapping_and_partial_query_rejected():
    element = {"type": "way", "id": 42, "center": {"lat": 48, "lon": 16}, "tags": {"name": "Test"}}
    with client(lambda r: httpx.Response(200, json={"elements": [element]})) as http:
        stations = OSMStations(http, "https://overpass.test").candidates(Trip.model_validate(TRIP), None)
        assert stations[0].id == "way/42"
        assert stations[0].fuel_types == ["diesel"]
    with client(lambda r: httpx.Response(200, json={"elements": [element], "remark": "timeout"})) as http:
        with pytest.raises(DependencyUnavailable):
            OSMStations(http, "https://overpass.test").candidates(Trip.model_validate(TRIP), None)


def test_price_permission_required_before_network():
    with client(lambda r: pytest.fail("Unauthorised feed must not be contacted")) as http:
        with pytest.raises(DependencyUnavailable):
            AuthorisedPrices(http, "https://prices.test", "", "Test").quotes("diesel")


@pytest.mark.parametrize("quotes", [
    [{"station_id": "node/1", "fuel_type": "diesel", "eur_per_l": "1.6", "observed_at": "2026-11-21T12:00:00"}],
    [{"station_id": "node/1", "fuel_type": "diesel", "eur_per_l": value, "observed_at": "2026-11-21T12:00:00Z"}
     for value in ["1.6", "1.7"]],
])
def test_bad_price_feed(quotes):
    with client(lambda r: httpx.Response(200, json={"quotes": quotes})) as http:
        with pytest.raises(DependencyUnavailable):
            AuthorisedPrices(http, "https://prices.test", "contract-123", "Test").quotes("diesel")


def test_api_health_metrics_and_validation():
    with TestClient(app) as http:
        assert http.get("/health/live").status_code == 200
        assert "worker_id" in http.get("/health/ready").json()
        assert "fuel_plan_seconds" in http.get("/metrics").text
        assert http.post("/plan", json={}).status_code == 422


def test_api_routing_unavailable(monkeypatch):
    with TestClient(app) as http, client(lambda r: httpx.Response(503, json={})) as upstream:
        app.state.client = upstream
        response = http.post("/plan", json=TRIP)
        assert response.status_code == 503
        assert response.json()["detail"]["status"] == "routing_unavailable"


def test_api_complete_plan_with_saved_http_contracts(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setenv("STATION_SOURCE", "overpass")
    monkeypatch.setenv("OVERPASS_URL", "https://overpass.test")
    monkeypatch.setenv("PRICE_FEED_URL", "https://prices.test")
    monkeypatch.setenv("PRICE_SOURCE", "Synthetic contract feed")
    monkeypatch.setenv("PRICE_PERMISSION_REFERENCE", "test-only")

    def handler(request):
        if request.url.host == "overpass.test":
            return httpx.Response(200, json={"elements": [{"type": "node", "id": 42,
                "lat": 48, "lon": 16, "tags": {"name": "Synthetic station"}}]})
        if request.url.host == "osrm.test":
            points = [[float(x) for x in p.split(",")] for p in request.url.path.rsplit("/", 1)[1].split(";")]
            legs = [200000] if len(points) == 2 else [80000, 120000]
            return httpx.Response(200, json=osrm_response(points, legs))
        assert request.url.host == "prices.test"
        return httpx.Response(200, json={"quotes": [{"station_id": "node/42", "fuel_type": "diesel",
            "eur_per_l": "1.60", "observed_at": datetime.now(timezone.utc).isoformat()}]})

    with TestClient(app) as http, client(handler) as upstream:
        app.state.client = upstream
        response = http.post("/plan", json=TRIP)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "stop"
        assert result["estimated_cost_eur"] == "11.20"
        assert result["litres_to_buy"] == "7.000"
        assert result["station"]["id"] == "node/42"
        assert result["stations_compared"] == result["priced_stations"] == 1
        assert result["ranking_complete"] is True


def test_malformed_station_response():
    with client(lambda r: httpx.Response(200, json=[])) as http:
        with pytest.raises(DependencyUnavailable):
            OSMStations(http, "https://overpass.test").query("test")


def test_direct_api_needs_no_station_or_price_lookup():
    def handler(request):
        assert request.url.host == "osrm.test", "Direct trips must not need station or price providers"
        return httpx.Response(200, json=osrm_response([[16.3738, 48.2082], [15.4395, 47.0707]], [200000]))
    with TestClient(app) as http, client(handler) as upstream:
        app.state.client = upstream
        response = http.post("/plan", json={**TRIP, "current_fuel_l": 35})
        assert response.status_code == 200
        assert response.json()["status"] == "direct"
        assert response.json()["stations_compared"] == 0


def test_boundary_missing_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("AUSTRIA_BOUNDARY_FILE", str(tmp_path / "missing.geojson"))
    with TestClient(app) as http:
        assert http.get("/health/live").status_code == 200
        assert http.get("/health/ready").status_code == 503
        assert http.get("/health/ready").json()["status"] == "not_ready"
        response = http.post("/plan", json=TRIP)
        assert response.status_code == 503
        assert response.json()["detail"]["status"] == "boundary_unavailable"


@pytest.mark.parametrize("mutation", ["missing_leg", "negative", "mismatched_sum", "nan", "not_a_number", "empty_geometry", "snap_too_far"])
def test_osrm_invalid_payload(mutation):
    payload = osrm_response([[16, 48], [15, 47]])
    route = payload["routes"][0]
    if mutation == "missing_leg":
        route["legs"] = []
    elif mutation == "negative":
        route["duration"] = -1
    elif mutation == "mismatched_sum":
        route["distance"] = 20
    elif mutation == "nan":
        route["distance"] = "NaN"
    elif mutation == "not_a_number":
        route["distance"] = "broken"
    elif mutation == "empty_geometry":
        route["geometry"]["coordinates"] = []
    elif mutation == "snap_too_far":
        payload["waypoints"][0]["distance"] = 500
    with client(lambda r: httpx.Response(200, json=payload)) as http:
        with pytest.raises(DependencyUnavailable):
            router(http).route([Point(lon=16, lat=48), Point(lon=15, lat=47)])


def test_osrm_timeout():
    def timeout(request):
        raise httpx.ReadTimeout("OSRM timed out", request=request)
    with client(timeout) as http:
        with pytest.raises(DependencyUnavailable):
            router(http).route([Point(lon=16, lat=48), Point(lon=15, lat=47)])


def test_public_demo_cannot_be_configured():
    with client(lambda r: pytest.fail("Do not call the public demo")) as http:
        with pytest.raises(DependencyUnavailable):
            OSRMRouter(http, "https://router.project-osrm.org", AustriaBoundary(box(9, 46, 18, 50)))


def test_saved_real_vienna_response():
    saved = json.loads((Path(__file__).parent / "fixtures/osrm-vienna.json").read_text(encoding="utf-8"))
    points = [Point(lon=16.3692842, lat=48.2024484), Point(lon=16.3682888, lat=48.202671),
              Point(lon=16.3658369, lat=48.2031961)]
    with client(lambda r: httpx.Response(200, json=saved["response"])) as http:
        route = router(http).route(points)
    assert route.distance_km == sum(route.leg_distances_km)
    assert [str(d) for d in route.leg_distances_km] == ["0.078", "0.1914"]
    assert str(route.duration_s) == "26.3"
    assert len(route.geometry["coordinates"]) == 17
