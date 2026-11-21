import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from fuel_stop.app import app
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


def test_local_capacity_not_silently_truncated(tmp_path):
    path = catalogue(tmp_path)
    source = LocalStations(str(path), AustriaBoundary(box(9, 46, 18, 50)), max_candidates=0)
    with pytest.raises(DependencyUnavailable, match="station_capacity"):
        source.candidates(Trip.model_validate(TRIP), None)


def test_default_local_station_plan_reaches_price_dependency(tmp_path, monkeypatch):
    monkeypatch.setenv("STATION_SOURCE", "local")
    monkeypatch.setenv("STATION_CATALOGUE_FILE", str(catalogue(tmp_path)))
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
        assert payload["stations_compared"] == payload["feasible_stations"] == 1
        assert payload["station_data_timestamp"] == "2026-11-01T00:00:00Z"
        assert payload["station_source"] == SOURCE


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
