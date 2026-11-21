"""Small real-data station check; default source is the local OSM catalogue.

Run from the repository root: uv run python -m scripts.check_live_stations
"""
import json
import argparse
import os
from pathlib import Path

import httpx

from fuel_stop.models import Trip
from fuel_stop.boundary import AustriaBoundary
from fuel_stop.providers import OSMStations
from fuel_stop.stations import LocalStations
from fuel_stop.planner import DependencyUnavailable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["local", "overpass"], default="local")
    parser.add_argument("--full-range", action="store_true")
    parser.add_argument("--limit", type=int, default=5, help="Number of station records to display; comparison is not truncated")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    root = Path(__file__).resolve().parents[1]
    trip = Trip.model_validate(json.loads((root / "examples/trip.json").read_text()))
    # Keep this smoke check small: approximately 5 km of usable range.
    if not args.full_range:
        trip = trip.model_copy(update={"current_fuel_l": trip.minimum_buffer_l + trip.consumption_l_per_100km / 20})
    boundary = AustriaBoundary.load(os.getenv("AUSTRIA_BOUNDARY_FILE", str(root / "data/osrm/austria-boundary.geojson")))
    boundary.validate_trip(trip)
    with httpx.Client(timeout=30) as http:
        source = (LocalStations(os.getenv("STATION_CATALOGUE_FILE", str(root / "data/osrm/stations.json")), boundary)
                  if args.source == "local" else OSMStations(http, os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")))
        try:
            stations = source.candidates(trip, None)
        except DependencyUnavailable as exc:
            raise SystemExit(json.dumps({"status": f"{exc.dependency}_unavailable", "reason": exc.reason})) from None
    print(json.dumps({"source": args.source, "map_timestamp": str(getattr(source, "data_timestamp", None)), "austrian_endpoints": True,
                      "stations_found": len(stations),
                      "stations_shown": min(args.limit, len(stations)),
                      "stations": [s.model_dump(mode="json") for s in stations[:args.limit]]}, indent=2))


if __name__ == "__main__":
    main()
