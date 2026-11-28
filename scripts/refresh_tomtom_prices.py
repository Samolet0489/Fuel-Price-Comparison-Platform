"""Look up TomTom fuel IDs and refresh an authorized Austrian price snapshot."""
import argparse
import json
import os
import sys
from pathlib import Path
from time import sleep
from urllib.parse import quote

import httpx

from fuel_stop.boundary import AustriaBoundary
from fuel_stop.models import Point, Quote
from fuel_stop.planner import DependencyUnavailable
from fuel_stop.providers import request
from fuel_stop.stations import LocalStations
from scripts.price_snapshot import write_snapshot


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "osrm"
BASE_URL = "https://api.tomtom.com/search/2"
SOURCE = "TomTom"
# Regular diesel and RON 95 only. Prefer sp95 when timestamps are equal.
GRADES = {"diesel": ("diesel",), "petrol95": ("sp95_e10", "sp95")}


def find_matches(client, api_key, station):
    query = quote(station.name, safe="")
    data = request(client, "tomtom_search", "GET", f"{BASE_URL}/search/{query}.json", params={
        "key": api_key, "countrySet": "AT", "idxSet": "POI", "fuelSet": "Diesel,Petrol",
        "lat": station.point.lat, "lon": station.point.lon, "radius": 150, "limit": 10,
    })
    matches = []
    for row in data["results"]:
        fuel_id = row.get("dataSources", {}).get("fuelPrice", {}).get("id")
        if fuel_id:
            point = Point.model_validate(row["position"])
            matches.append({"fuel_price_id": fuel_id, "name": row["poi"]["name"],
                            "address": row.get("address", {}).get("freeformAddress"),
                            "distance_m": round(station.point.distance_km(point) * 1000)})
    return sorted(matches, key=lambda row: row["distance_m"])


def normalize_prices(data, station_id, fuel_price_id):
    fuels = data["fuels"]
    if not isinstance(fuels, list) or (fuels and data["fuelPrice"] != fuel_price_id):
        raise ValueError("TomTom response does not match the requested fuel price ID")
    quotes = []
    for fuel_type, grades in GRADES.items():
        choices = []
        for row in fuels:
            if row["type"] not in grades:
                continue
            values = {str(price["value"]) for price in row["price"]
                      if price["currency"] == "EUR" and price["volumeUnit"] == "liter"}
            if len(values) > 1:
                raise ValueError("TomTom returned conflicting prices for one fuel grade")
            if values:
                normalized = Quote(station_id=station_id, fuel_type=fuel_type, eur_per_l=values.pop(),
                                   observed_at=row["updatedAt"])
                choices.append((normalized.observed_at, grades.index(row["type"]), normalized))
        if choices:
            latest = max((at, grade) for at, grade, _ in choices)
            matching = [q for at, grade, q in choices if (at, grade) == latest]
            if len({q.eur_per_l for q in matching}) > 1:
                raise ValueError("TomTom returned conflicting quotes for one station and timestamp")
            quotes.append(matching[0])
    return quotes


def refresh(client, api_key, mapping, output):
    quotes = []
    for station_id, fuel_price_id in sorted(mapping.items()):
        try:
            data = request(client, "tomtom_prices", "GET", f"{BASE_URL}/fuelPrice.json",
                           params={"key": api_key, "fuelPrice": fuel_price_id},
                           headers={"TomTom-Api-Version": "1"})
        except DependencyUnavailable as exc:
            if exc.reason == "upstream_http_404":
                continue
            raise
        quotes.extend(normalize_prices(data, station_id, fuel_price_id))
    write_snapshot(output, SOURCE, quotes)
    return len(quotes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--find", metavar="OSM_ID", help="list nearby TomTom matches for one local station")
    parser.add_argument("--mapping", type=Path, default=DATA / "tomtom-station-map.json")
    parser.add_argument("--output", type=Path, default=DATA / "tomtom-prices.json")
    parser.add_argument("--watch-seconds", type=int, help="repeat at this interval after each refresh")
    args = parser.parse_args()
    if args.watch_seconds is not None and (args.watch_seconds <= 0 or args.find):
        parser.error("--watch-seconds requires a positive interval and refresh mode")
    api_key = os.getenv("TOMTOM_API_KEY", "").strip()
    if not api_key or not os.getenv("PRICE_PERMISSION_REFERENCE", "").strip():
        parser.error("Set TOMTOM_API_KEY and PRICE_PERMISSION_REFERENCE in this terminal")

    try:
        boundary = AustriaBoundary.load(DATA / "austria-boundary.geojson")
        stations = {station.id: station for station in LocalStations(DATA / "stations.json", boundary).load()}
        if args.find and args.find not in stations:
            parser.error("The OSM station ID is absent from the local catalogue")
        if not args.find:
            mapping = json.loads(args.mapping.read_text(encoding="utf-8-sig"))
            if (not isinstance(mapping, dict) or not mapping
                    or any(k not in stations or not isinstance(v, str) or not v.strip() for k, v in mapping.items())
                    or len(set(mapping.values())) != len(mapping)):
                parser.error("The mapping must pair known OSM station IDs with distinct TomTom fuel price IDs")

        with httpx.Client(timeout=10, follow_redirects=False) as client:
            if args.find:
                station = stations[args.find]
                print(json.dumps({"osm_station": station.model_dump(mode="json"),
                                  "matches": find_matches(client, api_key, station)}, indent=2))
                return
            while True:
                try:
                    count = refresh(client, api_key, mapping, args.output)
                    print(f"Saved {count} TomTom quotes across {len(mapping)} mapped stations.", flush=True)
                except DependencyUnavailable as exc:
                    if args.watch_seconds is None:
                        raise
                    print(f"Refresh failed ({exc.reason or exc.dependency}). Previous snapshot retained.",
                          file=sys.stderr, flush=True)
                    if exc.reason in {"upstream_http_401", "upstream_http_403"}:
                        raise SystemExit(1) from None
                if args.watch_seconds is None:
                    return
                sleep(args.watch_seconds)
    except DependencyUnavailable as exc:
        sys.exit(f"TomTom price setup failed ({exc.reason or exc.dependency}).")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        sys.exit("Could not read the local inputs, validate TomTom's reply, or write the snapshot.")


if __name__ == "__main__":
    main()
