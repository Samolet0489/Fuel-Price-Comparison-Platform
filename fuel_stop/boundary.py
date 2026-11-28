"""Local, fail-closed country checks; never use an extract's padded extent."""
import json
from pathlib import Path

from shapely import from_geojson, linestrings
from shapely.geometry import Point as GeoPoint
from shapely.prepared import prep
from shapely.errors import ShapelyError

from .models import Point, Trip
from .planner import DependencyUnavailable, OutsideAustria


class AustriaBoundary:
    def __init__(self, geometry):
        if geometry.geom_type not in ("Polygon", "MultiPolygon") or geometry.is_empty or not geometry.is_valid:
            raise ValueError("A valid country Polygon/MultiPolygon is required")
        self.geometry = geometry
        self.prepared = prep(geometry)

    @classmethod
    def load(cls, path: str):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if data["type"] != "Feature" or data["properties"]["osm_relation_id"] != 16239:
                raise ValueError("Expected Austrian OSM country boundary")
            return cls(from_geojson(json.dumps(data["geometry"])))
        except (OSError, ValueError, KeyError, TypeError, ShapelyError):
            raise DependencyUnavailable("boundary") from None

    def covers_point(self, point: Point) -> bool:
        return self.prepared.covers(GeoPoint(point.lon, point.lat))

    def validate_trip(self, trip: Trip):
        if not all(self.covers_point(p) for p in (trip.origin, trip.destination)):
            raise OutsideAustria()

    def covers_route(self, coordinates: list) -> bool:
        # Covers the WHOLE line, not just its vertices. No outward buffer:
        # even a small border excursion must not be silently accepted.
        return self.prepared.covers(linestrings(coordinates))
