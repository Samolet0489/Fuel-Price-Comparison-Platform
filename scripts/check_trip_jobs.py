"""Verify direct and refueling trips through the public API."""
import argparse
import json
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep

import httpx

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def check_fuel_constraints(trip, result):
    fuel = Decimal(str(trip["current_fuel_l"]))
    rate = Decimal(str(trip["consumption_l_per_100km"])) / 100
    tank = Decimal(str(trip["tank_capacity_l"]))
    buffer = Decimal(str(trip.get("minimum_buffer_l", 1)))
    stops = result["stops"]
    legs = result["route"]["leg_distances_km"]
    if len(legs) != len(stops) + 1:
        raise RuntimeError("The number of route legs does not match the stops")
    for stop, distance in zip(stops, legs):
        fuel -= Decimal(distance) * rate
        if fuel < buffer or fuel != Decimal(stop["arrival_fuel_l"]):
            raise RuntimeError("A refueling stop could not be reached with the required fuel buffer")
        fuel += Decimal(stop["litres_to_buy"])
        if fuel > tank or fuel != Decimal(stop["departure_fuel_l"]):
            raise RuntimeError("Reported departure fuel does not fit the tank")
    fuel -= Decimal(legs[-1]) * rate
    if fuel < Decimal(str(trip["arrival_reserve_l"])) or fuel != Decimal(result["arrival_fuel_l"]):
        raise RuntimeError("Destination arrival fuel does not meet the reserve")
    if trip.get("max_detour_km") is not None and Decimal(result["detour_km"]) > Decimal(str(trip["max_detour_km"])):
        raise RuntimeError("Route detour exceeds the trip limit")


def check_trip(client: httpx.Client, filename: str, expected: str, *, updates=None, require_complete=False):
    trip = json.loads((EXAMPLES / filename).read_text(encoding="utf-8"))
    trip.update(updates or {})
    start = monotonic()
    response = client.post("/trip-plans", json=trip)
    if response.status_code != 202:
        raise RuntimeError(f"Submit returned {response.status_code}: {response.text}")
    receipt = response.json()
    location = f"/trip-plans/{receipt['id']}"
    if receipt["status"] != "QUEUED" or response.headers.get("Location") != location:
        raise RuntimeError(f"Unexpected job receipt: {receipt}")

    deadline = monotonic() + 60
    last_poll_error = None
    while monotonic() < deadline:
        try:
            job = client.get(location).raise_for_status().json()
        except httpx.TransportError as exc:
            last_poll_error = exc
            sleep(0.5)
            continue
        last_poll_error = None
        if job["status"] == "FAILED":
            raise RuntimeError(f"{filename} job {job['id']} failed: {job['error']}")
        if job["status"] == "SUCCEEDED":
            result = job["result"]
            if result["status"] != expected or result["mode"] != "live" or not result["route"]:
                raise RuntimeError(f"{filename}: expected a live {expected} route, got {result['status']}")
            if require_complete:
                if not result["ranking_complete"]:
                    raise RuntimeError(f"{filename}: price comparison was incomplete")
                check_fuel_constraints(trip, result)
            print(f"{job['id']} SUCCEEDED: {result['status']} ({result['route']['distance_km']} km) "
                  f"in {monotonic() - start:.1f}s, ranking_complete={result['ranking_complete']}")
            return result
        sleep(0.5)
    raise RuntimeError(f"{filename} job {receipt['id']} did not finish in time") from last_poll_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--expect-unpriced", action="store_true", help="check the fallback in observed price mode")
    mode.add_argument("--check-ranking", action="store_true", help="require complete rankings for single- and multi-stop trips")
    args = parser.parse_args()
    with httpx.Client(base_url=args.api_url, timeout=5) as client:
        check_trip(client, "direct-trip.json", "direct")
        expected = "prices_unavailable" if args.expect_unpriced else "stop"
        result = check_trip(client, "trip.json", expected, require_complete=args.check_ranking)
        has_cost = result["estimated_cost_eur"] is not None
        if not result["stops"] or has_cost == args.expect_unpriced:
            raise RuntimeError("Trip did not return the expected stops and cost")
        for stop in result["stops"]:
            print(f"{stop['station']['name']}: buy {stop['litres_to_buy']} L")
        if has_cost:
            print(f"Estimated cost: EUR {result['estimated_cost_eur']}")
        if args.check_ranking:
            print("Salzburg–Linz price comparison")
            check_trip(client, "trip.json", "stop", require_complete=True, updates={
                "origin": {"lon": 13.0449, "lat": 47.80938},
                "destination": {"lon": 14.2858, "lat": 48.2914},
            })
            print("Vienna–Graz multi-stop price comparison")
            multi_stop = check_trip(client, "trip.json", "stop", require_complete=True, updates={
                "current_fuel_l": 8,
                "tank_capacity_l": 8.5,
                "consumption_l_per_100km": 10,
                "arrival_reserve_l": 2,
                "max_detour_km": 15,
            })
            purchases = [Decimal(stop["litres_to_buy"]) for stop in multi_stop["stops"]]
            if len(purchases) < 2 or any(purchase <= 0 for purchase in purchases):
                raise RuntimeError("Expected at least two refueling stops with positive purchases")
            if multi_stop["estimated_cost_eur"] is None or sum(purchases) != Decimal(multi_stop["litres_to_buy"]):
                raise RuntimeError("The multi-stop route must report its estimated cost and total purchase")
            print(f"{len(purchases)} refueling stops, estimated cost EUR {multi_stop['estimated_cost_eur']}")


if __name__ == "__main__":
    main()
