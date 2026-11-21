"""Local, dated OSM station catalogue. No runtime network dependency."""
import json
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .boundary import AustriaBoundary
from .models import Point, Station, Trip
from .planner import DependencyUnavailable

SOURCE = "OpenStreetMap / local Austria PBF"
RESTRICTED_ACCESS = {"private", "no", "customers", "permit"}


def from_tags(osm_id: str, tags: dict, point: Point) -> Station | None:
    """Apply the existing conservative eligibility rules to real OSM tags."""
    if (tags.get("amenity") != "fuel" or tags.get("opening_hours") != "24/7" or
        tags.get("access") in RESTRICTED_ACCESS or tags.get("motor_vehicle") in RESTRICTED_ACCESS or
        tags.get("disused") == "yes" or tags.get("abandoned") == "yes"):
        return None
    fuels = [fuel for fuel, tag in (("diesel", "diesel"), ("petrol95", "octane_95"))
             if tags.get(f"fuel:{tag}") == "yes"]
    if not fuels:
        return None
    return Station(id=osm_id, name=tags.get("name", tags.get("brand", "Unnamed station")),
                   point=point, fuel_types=fuels, opening_hours="24/7", source=SOURCE)


def distance_km(a: Point, b: Point) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat, dlon = lat2 - lat1, math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, h)))


class LocalStations:
    source = SOURCE
    data_timestamp: datetime | None = None

    def __init__(self, path: str, boundary: AustriaBoundary):
        self.path, self.boundary = path, boundary

    def load(self) -> list[Station]:
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
            if data["schema_version"] != 1 or data["source"] != SOURCE:
                raise ValueError("Unsupported catalogue")
            self.data_timestamp = datetime.fromisoformat(data["map_timestamp"].replace("Z", "+00:00"))
            if self.data_timestamp.tzinfo is None:
                raise ValueError("Map timestamp must include a timezone")
            stations = [Station.model_validate(s) for s in data["stations"]]
            if len({s.id for s in stations}) != len(stations):
                raise ValueError("Duplicate OSM station IDs")
            for station in stations:
                if (station.opening_hours != "24/7" or not station.fuel_types or
                    not set(station.fuel_types) <= {"diesel", "petrol95"}):
                    raise ValueError("Invalid station eligibility")
            return stations
        except OSError:
            raise DependencyUnavailable("stations", "local_catalogue_missing_or_unreadable") from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise DependencyUnavailable("stations", "local_catalogue_invalid") from None

    def candidates(self, trip: Trip, direct) -> list[Station]:
        radius = float(max(Decimal(0), trip.current_fuel_l - trip.minimum_buffer_l)
                       * 100 / trip.consumption_l_per_100km) + 1
        matches = [s for s in self.load() if trip.fuel_type in s.fuel_types
                   and self.boundary.covers_point(s.point)
                   and distance_km(trip.origin, s.point) <= radius]
        return sorted(matches, key=lambda s: s.id)
