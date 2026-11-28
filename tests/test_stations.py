import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from fuel_stop.app import app, price_provider
from fuel_stop.boundary import AustriaBoundary
from fuel_stop.models import Point, Trip
from fuel_stop.planner import DependencyUnavailable
from fuel_stop.providers import OSMStations
from fuel_stop.stations import SOURCE, LocalStations, from_tags
from tests.test_providers import TRIP, osrm_response

TAGS = {"amenity": "fuel", "opening_hours": "24/7", "fuel:diesel": "yes", "name": "Synthetic test station"}


def catalogue(tmp_path, stations=None):
    station = from_tags("node/42", TAGS, Point(lon=16, lat=48)).model_dump(mode="json")
    data = {"schema_version": 1, "source": SOURCE, "map_timestamp": "2026-11-01T00:00:00Z",
            "stations": stations if stations is not None else [station]}
    path = tmp_path / "stations.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("change", [
    {"access": "private"}, {"motor_vehicle": "no"}, {"opening_hours": "Mo-Fr 08:00-17:00"},
    {"disused": "yes"}, {"abandoned": "yes"}, {"fuel:diesel": "no"}, {"amenity": "parking"},
])
def test_station_rules_preserved(change):
    assert from_tags("node/1", {**TAGS, **change}, Point(lon=16, lat=48)) is None


def test_local_catalogue_filters_fuel_range_and_country(tmp_path):
    rows = []
    for ident, lon, lat, fuels in [(1, 16.3738, 48.2082, ["diesel"]),
                                   (2, 16.3738, 48.2082, ["petrol95"]),
                                   (3, 10, 47, ["diesel"]), (4, 18.1, 48.2, ["diesel"])]:
        station = from_tags(f"node/{ident}", TAGS, Point(lon=lon, lat=lat))
        rows.append(station.model_copy(update={"fuel_types": fuels}).model_dump(mode="json"))
    source = LocalStations(str(catalogue(tmp_path, rows)), AustriaBoundary(box(9, 46, 18, 50)))
    matches = source.candidates(Trip.model_validate(TRIP), None)
    assert [s.id for s in matches] == ["node/1"]
    assert source.data_timestamp.isoformat() == "2026-11-01T00:00:00+00:00"


def test_local_missing_catalogue_is_explicit(tmp_path):
    with pytest.raises(DependencyUnavailable) as error:
        LocalStations(str(tmp_path / "missing.json"), AustriaBoundary(box(9, 46, 18, 50))).load()
    assert error.value.reason == "local_catalogue_missing_or_unreadable"


def test_duplicate_catalogue_rejected(tmp_path):
    path = catalogue(tmp_path)
    data = json.loads(path.read_text())
    data["stations"] *= 2
    path.write_text(json.dumps(data))
    with pytest.raises(DependencyUnavailable) as error:
        LocalStations(str(path), AustriaBoundary(box(9, 46, 18, 50))).load()
    assert error.value.reason == "local_catalogue_invalid"


def test_empty_valid_catalogue_is_not_provider_failure(tmp_path):
    assert LocalStations(str(catalogue(tmp_path, [])), AustriaBoundary(box(9, 46, 18, 50))).candidates(
        Trip.model_validate(TRIP), None) == []


def test_list_stations_returns_catalogue_without_trip_filter(tmp_path, monkeypatch):
    rows = [
        from_tags("node/1", TAGS, Point(lon=16, lat=48)).model_dump(mode="json"),
        from_tags("node/2", {**TAGS, "fuel:diesel": "no", "fuel:octane_95": "yes"},
                  Point(lon=10, lat=47)).model_dump(mode="json"),
    ]
    monkeypatch.setenv("STATION_CATALOGUE_FILE", str(catalogue(tmp_path, rows)))
    with TestClient(app) as http:
        response = http.get("/stations")
    assert response.status_code == 200
    assert response.json() == {
        "source": SOURCE, "data_timestamp": "2026-11-01T00:00:00Z", "stations": rows,
    }


def test_generated_quotes_use_the_current_revision(tmp_path, monkeypatch):
    station = from_tags("node/42", {**TAGS, "fuel:octane_95": "yes"}, Point(lon=16, lat=48))
    other = station.model_copy(update={"id": "way/43", "fuel_types": ["diesel"]})
    rows = [item.model_dump(mode="json") for item in (station, other)]
    monkeypatch.setenv("STATION_CATALOGUE_FILE", str(catalogue(tmp_path, rows)))
    monkeypatch.delenv("PRICE_MODE", raising=False)
    monkeypatch.setenv("PRICE_REVISION_URL", "http://api.test/fuel-prices/revision")
    revision = 0
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"revision": revision}))) as client:
        provider = price_provider(client)
        before = {q.station_id: q.eur_per_l for q in provider.quotes("diesel")}
        assert before.keys() == {"node/42", "way/43"}
        assert [q.station_id for q in provider.quotes("petrol95")] == ["node/42"]
        revision = 1
        after = {q.station_id: q.eur_per_l for q in provider.quotes("diesel")}
        assert all(before[station_id] != after[station_id] for station_id in before)


@pytest.mark.parametrize(("station_file", "osrm_ok", "dependency"), [
    ("missing", True, "stations"),
    ("invalid", True, "stations"),
    ("valid", False, "routing"),
    ("valid", True, None),
])
def test_readiness_checks_station_catalogue_and_osrm(tmp_path, monkeypatch, station_file, osrm_ok, dependency):
    path = tmp_path / "stations.json"
    if station_file == "valid":
        catalogue(tmp_path)
    elif station_file == "invalid":
        path.write_text("{}")
    monkeypatch.setenv("STATION_SOURCE", "local")
    monkeypatch.setenv("STATION_CATALOGUE_FILE", str(path))

    def handler(request):
        assert "/nearest/v1/driving/" in request.url.path
        if not osrm_ok:
            return httpx.Response(503)
        return httpx.Response(200, json={"code": "Ok", "waypoints": [{"location": [16.369, 48.202]}]})

    with TestClient(app) as http, httpx.Client(transport=httpx.MockTransport(handler)) as upstream:
        app.state.client = upstream
        response = http.get("/health/ready")
        assert http.get("/health/live").status_code == 200
    ready = response.json()
    assert response.status_code == (200 if dependency is None else 503)
    assert ready["boundary_ready"]
    assert ready["stations_ready"] == (station_file == "valid")
    assert ready["routing_ready"] == (dependency is None)
    assert ready.get("dependency") == dependency


@pytest.mark.parametrize("station_count", [1, 201])
def test_default_local_station_plan_compares_all_candidates(tmp_path, monkeypatch, station_count):
    stations = [
        from_tags(f"node/{ident}", TAGS, Point(lon=16, lat=48)).model_dump(mode="json")
        for ident in range(1, station_count + 1)
    ]
    monkeypatch.setenv("STATION_SOURCE", "local")
    monkeypatch.setenv("PRICE_MODE", "observed")
    monkeypatch.setenv("STATION_CATALOGUE_FILE", str(catalogue(tmp_path, stations)))
    for name in ("PRICE_FEED_URL", "PRICE_PERMISSION_REFERENCE", "PRICE_SOURCE"):
        monkeypatch.delenv(name, raising=False)
    def handler(request):
        assert request.url.host == "osrm.test", "Local station lookup must not use Overpass"
        points = [[float(x) for x in p.split(",")] for p in request.url.path.rsplit("/", 1)[1].split(";")]
        return httpx.Response(200, json=osrm_response(points, [200000] if len(points) == 2 else [80000, 120000]))
    with TestClient(app) as http, httpx.Client(transport=httpx.MockTransport(handler)) as upstream:
        app.state.client = upstream
        result = http.post("/plan", json=TRIP)
        assert result.status_code == 200
        payload = result.json()
        assert payload["status"] == "prices_unavailable"
        assert payload["station"] is not None
        assert payload["route"]["distance_km"] == "200"
        assert payload["litres_to_buy"] == "7.000"
        assert payload["estimated_cost_eur"] is None
        assert payload["stations_compared"] == payload["feasible_stations"] == station_count
        assert payload["station_data_timestamp"] == "2026-11-01T00:00:00Z"
        assert payload["station_source"] == SOURCE


def test_multi_stop_plan_includes_timestamp_from_local_catalogue(tmp_path, monkeypatch):
    origin, first, second, destination = [(lon, 48.0) for lon in (14.0, 14.5, 15.5, 16.0)]
    stations = [
        from_tags(f"node/{ident}", TAGS, Point(lon=lon, lat=lat)).model_dump(mode="json")
        for ident, (lon, lat) in enumerate([first, second], 1)
    ]
    monkeypatch.setenv("STATION_SOURCE", "local")
    monkeypatch.setenv("STATION_CATALOGUE_FILE", str(catalogue(tmp_path, stations)))
    monkeypatch.setenv("PRICE_MODE", "observed")
    monkeypatch.delenv("PRICE_FEED_URL", raising=False)
    monkeypatch.delenv("PRICE_SNAPSHOT_FILE", raising=False)
    roads = {(origin, destination): 200000, (origin, first): 40000,
             (first, second): 80000, (second, destination): 80000}

    def handler(request):
        path = request.url.path.rsplit("/", 1)[1]
        points = [tuple(map(float, coordinate.split(","))) for coordinate in path.split(";")]
        distance = roads.get(tuple(points))
        if distance is None:
            return httpx.Response(200, json={"code": "NoRoute"})
        return httpx.Response(200, json=osrm_response(points, [distance]))

    trip = {
        **TRIP, "origin": dict(zip(("lon", "lat"), origin)), "destination": dict(zip(("lon", "lat"), destination)),
        "current_fuel_l": 5, "tank_capacity_l": 10, "consumption_l_per_100km": 10, "arrival_reserve_l": 2,
    }
    with TestClient(app) as http, httpx.Client(transport=httpx.MockTransport(handler)) as upstream:
        app.state.client = upstream
        response = http.post("/plan", json=trip)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "prices_unavailable"
    assert [stop["station"]["id"] for stop in result["stops"]] == ["node/1", "node/2"]
    assert result["station_source"] == SOURCE
    assert result["station_data_timestamp"] == "2026-11-01T00:00:00Z"


def test_overpass_failure_has_safe_reason(caplog):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(502, text="Provider queue full"))) as http:
        with pytest.raises(DependencyUnavailable) as error:
            OSMStations(http, "https://overpass.test").query("sensitive query text")
    assert error.value.reason == "upstream_http_502"
    assert "upstream_http_502" in caplog.text
    assert "sensitive query text" not in caplog.text


def test_extract_real_pbf_format_nodes_ways_relations(tmp_path, local_test_boundary):
    osmium = pytest.importorskip("osmium", reason="Native extraction runs in Linux data container; use --group data on supported hosts", exc_type=ImportError)
    from scripts.extract_stations import extract
    # This is a synthetic OSM PBF fixture, not production station data.
    pbf = tmp_path / "synthetic.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", "2026-11-01T00:00:00Z")
    with osmium.SimpleWriter(str(pbf), header=header) as writer:
        for ident, location in [(1, (16, 48)), (2, (16.01, 48)), (3, (16.01, 48.01)), (4, (16, 48.01))]:
            writer.add_node(osmium.osm.mutable.Node(id=ident, location=location, tags=TAGS if ident == 1 else {}))
        writer.add_way(osmium.osm.mutable.Way(id=10, nodes=[1, 2, 3, 4, 1], tags=TAGS))
        writer.add_relation(osmium.osm.mutable.Relation(id=20, members=[('w', 10, 'outer')],
            tags={**TAGS, "type": "multipolygon"}))
    path = tmp_path / "stations.json"
    result = extract(pbf, local_test_boundary, path)
    assert result["counts"]["eligible_stations"] == 3
    assert {s.id for s in LocalStations(str(path), AustriaBoundary(box(9, 46, 18, 50))).load()} == {
        "node/1", "way/10", "relation/20"}
    assert result["map_timestamp"] == "2026-11-01T00:00:00Z"
    assert len(result["pbf_sha256"]) == 64
