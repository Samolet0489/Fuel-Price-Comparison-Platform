"""Local, dated OSM station catalogue. No runtime network dependency."""
import json
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
        return [s for s in self.route_candidates(trip, direct) if trip.origin.distance_km(s.point) <= radius]

    def route_candidates(self, trip: Trip, direct) -> list[Station]:
        # Later stops may be beyond the range of the fuel already in the tank.
        return sorted((s for s in self.load() if trip.fuel_type in s.fuel_types
                       and self.boundary.covers_point(s.point)), key=lambda s: s.id)
