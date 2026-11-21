"""Live adapters. Provider failures never become fake routes or zero prices."""
import math
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from prometheus_client import Counter, Histogram

from .models import Point, Quote, Route, Station, Trip
from .boundary import AustriaBoundary
from .planner import BorderRestrictedRoute, DependencyUnavailable, NoRoute

LOOKUP_SECONDS = Histogram("fuel_provider_seconds", "Provider request latency", ["provider"])
LOOKUP_FAILURES = Counter("fuel_provider_failures_total", "Failed provider requests", ["provider"])
logger = logging.getLogger(__name__)


def request(client, provider, method, url, **kwargs):
    try:
        with LOOKUP_SECONDS.labels(provider).time():
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        LOOKUP_FAILURES.labels(provider).inc()
        if isinstance(exc, httpx.HTTPStatusError):
            reason = f"upstream_http_{exc.response.status_code}"
        elif isinstance(exc, httpx.TimeoutException):
            reason = "upstream_timeout"
        elif isinstance(exc, httpx.HTTPError):
            reason = "upstream_network_error"
        else:
            reason = "upstream_invalid_json"
        # Never log credentials, response bodies or complete provider URLs.
        logger.warning("%s lookup failed: %s", provider, reason)
        raise DependencyUnavailable(provider, reason) from None


class OSRMRouter:
    def __init__(self, client: httpx.Client, base_url: str, boundary: AustriaBoundary):
        self.client, self.base_url, self.boundary = client, base_url.rstrip("/"), boundary
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.hostname == "router.project-osrm.org":
            raise DependencyUnavailable("routing")

    def validate_trip(self, trip: Trip):
        self.boundary.validate_trip(trip)

    def route(self, points: list[Point]) -> Route:
        if not 2 <= len(points) <= 3:
            raise ValueError("Expected origin, optional station, and destination")
        if not all(self.boundary.covers_point(p) for p in points):
            raise NoRoute()
        coordinates = ";".join(f"{p.lon},{p.lat}" for p in points)
        params = {"overview": "full", "geometries": "geojson", "steps": "false",
                  "alternatives": "false", "radiuses": ";".join(["100"] * len(points))}
        try:
            with LOOKUP_SECONDS.labels("routing").time():
                response = self.client.get(f"{self.base_url}/route/v1/driving/{coordinates}", params=params)
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Invalid routing response")
                if response.status_code in (200, 400) and data.get("code") in ("NoRoute", "NoSegment"):
                    raise NoRoute()
                response.raise_for_status()
            if data.get("code") != "Ok":
                raise ValueError("OSRM did not return an OK route")
            route = data["routes"][0]
            legs = [Decimal(str(s["distance"])) / 1000 for s in route["legs"]]
            distance = Decimal(str(route["distance"])) / 1000
            duration = Decimal(str(route["duration"]))
            if (len(legs) != len(points) - 1 or not distance.is_finite() or
                not duration.is_finite() or duration < 0 or
                any(not x.is_finite() or x < 0 for x in legs)):
                raise ValueError("Invalid route legs")
            if abs(sum(legs) - distance) > Decimal("0.001"):
                raise ValueError("Route distance and legs disagree")
            geometry = route["geometry"]
            if geometry["type"] != "LineString" or len(geometry["coordinates"]) < 2:
                raise ValueError("Missing map geometry")
            for coordinate in geometry["coordinates"]:
                if len(coordinate) != 2:
                    raise ValueError("Expected 2D route coordinates")
                Point(lon=coordinate[0], lat=coordinate[1])
            waypoints = data["waypoints"]
            if len(waypoints) != len(points):
                raise ValueError("Invalid snapped waypoints")
            for waypoint in waypoints:
                snap_distance = float(waypoint["distance"])
                if not math.isfinite(snap_distance) or not 0 <= snap_distance <= 100:
                    raise ValueError("Invalid snapping distance")
                location = waypoint["location"]
                if not self.boundary.covers_point(Point(lon=location[0], lat=location[1])):
                    raise BorderRestrictedRoute()
            if not self.boundary.covers_route(geometry["coordinates"]):
                raise BorderRestrictedRoute()
            return Route(distance_km=distance, duration_s=duration,
                         leg_distances_km=legs, geometry=geometry,
                         attribution="OSRM | © OpenStreetMap contributors (ODbL), https://www.openstreetmap.org/copyright")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, InvalidOperation, OverflowError):
            LOOKUP_FAILURES.labels("routing").inc()
            raise DependencyUnavailable("routing") from None


class OSMStations:
    def __init__(self, client: httpx.Client, url: str, max_candidates=200):
        self.client, self.url, self.max_candidates = client, url, max_candidates

    def query(self, query):
        data = request(self.client, "stations", "GET", self.url, params={"data": query},
                       headers={"User-Agent": "FuelStopPlanner/0.1 (university project)"})
        if not isinstance(data, dict) or "remark" in data or not isinstance(data.get("elements"), list):
            raise DependencyUnavailable("stations", "upstream_incomplete_response")
        return data["elements"]

    def candidates(self, trip: Trip, direct: Route) -> list[Station]:
        # Any feasible first stop must lie within the car's straight-line range.
        # This safe superset avoids an arbitrary narrow route corridor.
        reachable_km = max(Decimal(0), trip.current_fuel_l - trip.minimum_buffer_l) * 100 / trip.consumption_l_per_100km
        radius_m = math.ceil(float(reachable_km) * 1000) + 1000  # account for road snapping
        query = (f'[out:json][timeout:25];area["ISO3166-1"="AT"]["admin_level"="2"]->.at;'
                 f'nwr(area.at)(around:{radius_m},{trip.origin.lat},{trip.origin.lon})'
                 '["amenity"="fuel"]["opening_hours"="24/7"]'
                 '["access"!~"^(private|no|customers|permit)$"]'
                 '["motor_vehicle"!~"^(private|no|customers|permit)$"]["disused"!="yes"]["abandoned"!="yes"]'
                 f'["fuel:{"octane_95" if trip.fuel_type == "petrol95" else "diesel"}"="yes"];out center tags;')
        elements = self.query(query)
        if len(elements) > self.max_candidates:
            raise DependencyUnavailable("station_capacity")
        try:
            stations = []
            for element in elements:
                tags = element["tags"]
                center = element.get("center", element)
                stations.append(Station(id=f'{element["type"]}/{element["id"]}',
                                        name=tags.get("name", tags.get("brand", "Unnamed station")),
                                        point=Point(lon=center["lon"], lat=center["lat"]),
                                        fuel_types=[trip.fuel_type], opening_hours="24/7"))
            return stations
        except (KeyError, TypeError, ValueError):
            raise DependencyUnavailable("stations") from None


class AuthorisedPrices:
    """Contract for a licensed feed, NOT an adapter for E-Control's private API.

    The URL must serve {"quotes": [Quote, ...]}. IDs must match OSM IDs.
    An upstream adapter is needed if the eventual licensed provider differs.
    """
    def __init__(self, client, url: str, permission_reference: str, source: str, token: str = ""):
        self.client, self.url, self.permission_reference = client, url, permission_reference
        self.source, self.token, self.retrieved_at = source, token, None

    def quotes(self, fuel_type: str) -> list[Quote]:
        if not self.url or not self.permission_reference or not self.source:
            LOOKUP_FAILURES.labels("prices").inc()
            raise DependencyUnavailable("prices")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        data = request(self.client, "prices", "GET", self.url, headers=headers,
                       params={"fuel_type": fuel_type})
        try:
            quotes = [Quote.model_validate(q) for q in data["quotes"]]
            # Reject ambiguous conflicting duplicates rather than choose an arbitrary price.
            seen = {}
            for q in quotes:
                key = (q.station_id, q.fuel_type, q.observed_at)
                if key in seen and seen[key] != q.eur_per_l:
                    raise ValueError("Conflicting prices")
                seen[key] = q.eur_per_l
            self.retrieved_at = datetime.now(timezone.utc)
            return quotes
        except (KeyError, TypeError, ValueError, ValidationError):
            LOOKUP_FAILURES.labels("prices").inc()
            raise DependencyUnavailable("prices") from None
