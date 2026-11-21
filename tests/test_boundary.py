import json

import httpx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box, shape

from fuel_stop.app import app
from fuel_stop.boundary import AustriaBoundary
from fuel_stop.models import Point
from fuel_stop.planner import BorderRestrictedRoute, DependencyUnavailable
from fuel_stop.providers import OSRMRouter
from scripts.prepare_osrm import country_feature
from tests.test_providers import TRIP, osrm_response


def test_entire_line_checked_including_between_vertices():
    # Synthetic country with a foreign enclave. Both vertices are inside, but
    # the connecting segment crosses foreign territory. Vertex-only checks fail.
    boundary = AustriaBoundary(box(9, 46, 18, 50).difference(box(13, 47, 14, 49)))
    points = [Point(lon=12, lat=48), Point(lon=16, lat=48)]
    assert all(boundary.covers_point(p) for p in points)
    assert not boundary.covers_route([p.coordinates() for p in points])
    payload = osrm_response([p.coordinates() for p in points])
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(BorderRestrictedRoute):
            OSRMRouter(client, "http://osrm.test", boundary).route(points)


def test_border_exit_and_reentry_reported_by_api():
    payload = osrm_response([[16.3738, 48.2082], [19, 48], [15.4395, 47.0707]], [100000, 100000])
    # Two requested points, one OSRM leg, with an out-of-country interior vertex.
    payload["routes"][0]["legs"] = [{"distance": 200000, "duration": 600}]
    payload["waypoints"] = [payload["waypoints"][0], payload["waypoints"][-1]]
    with TestClient(app) as http, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as upstream:
        app.state.client = upstream
        result = http.post("/plan", json=TRIP)
        assert result.status_code == 422
        assert result.json()["detail"]["status"] == "border_restricted_route"


def test_snapped_waypoint_outside_country_rejected():
    payload = osrm_response([[16, 48], [15, 47]])
    payload["waypoints"][0]["location"] = [19, 48]
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(BorderRestrictedRoute):
            OSRMRouter(client, "http://osrm.test", AustriaBoundary(box(9, 46, 18, 50))).route(
                [Point(lon=16, lat=48), Point(lon=15, lat=47)])


@pytest.mark.parametrize("data", [{}, {"type": "Feature", "properties": {"osm_relation_id": 123}, "geometry": {}},
                                  {"type": "Feature", "properties": {"osm_relation_id": 16239}, "geometry": {"type": "Point", "coordinates": [16, 48]}}])
def test_bad_boundary_fails_closed(tmp_path, data):
    path = tmp_path / "bad.geojson"
    path.write_text(json.dumps(data))
    with pytest.raises(DependencyUnavailable, match="boundary"):
        AustriaBoundary.load(str(path))


def test_country_relation_assembled_from_connected_ways_and_hole():
    xml = b'''<osm>
    <node id="1" lon="0" lat="0"/><node id="2" lon="4" lat="0"/>
    <node id="3" lon="4" lat="4"/><node id="4" lon="0" lat="4"/>
    <node id="5" lon="1" lat="1"/><node id="6" lon="2" lat="1"/>
    <node id="7" lon="2" lat="2"/><node id="8" lon="1" lat="2"/>
    <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/></way>
    <way id="11"><nd ref="3"/><nd ref="4"/><nd ref="1"/></way>
    <way id="12"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="5"/></way>
    <relation id="16239" version="42">
      <member type="way" ref="10" role="outer"/><member type="way" ref="11" role="outer"/>
      <member type="way" ref="12" role="inner"/>
      <tag k="ISO3166-1" v="AT"/><tag k="admin_level" v="2"/>
    </relation></osm>'''
    result = country_feature(xml)
    assert result["properties"]["relation_version"] == "42"
    assert shape(result["geometry"]).area == 15
    with pytest.raises(ValueError, match="unclosed"):
        country_feature(xml.replace(b'<member type="way" ref="11" role="outer"/>', b''))


def test_country_relation_identity_required():
    with pytest.raises(ValueError):
        country_feature(b'<osm><relation id="16239"><tag k="ISO3166-1" v="DE"/></relation></osm>')
