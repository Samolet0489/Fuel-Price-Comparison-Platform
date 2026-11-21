"""Small, explicit real-data smoke check; not part of offline CI.

Run from the repository root: uv run python -m scripts.check_live_stations
"""
import json
from pathlib import Path

import httpx

from fuel_stop.models import Trip
from fuel_stop.boundary import AustriaBoundary
from fuel_stop.providers import OSMStations


def main():
    trip = Trip.model_validate(json.loads(Path("examples/trip.json").read_text()))
    # Keep this smoke check small: approximately 5 km of usable range.
    trip = trip.model_copy(update={"current_fuel_l": trip.minimum_buffer_l + trip.consumption_l_per_100km / 20})
    with httpx.Client(timeout=30) as http:
        source = OSMStations(http, "https://overpass-api.de/api/interpreter")
        AustriaBoundary.load("data/osrm/austria-boundary.geojson").validate_trip(trip)
        stations = source.candidates(trip, None)
    print(json.dumps({"source": "OpenStreetMap via Overpass", "austrian_endpoints": True,
                      "stations_found": len(stations),
                      "stations": [s.model_dump(mode="json") for s in stations]}, indent=2))


if __name__ == "__main__":
    main()
