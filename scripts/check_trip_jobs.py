"""Verify the direct and no-price fallback trips through the public API."""
import argparse
import json
from pathlib import Path
from time import monotonic, sleep

import httpx

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def check_trip(client: httpx.Client, filename: str, expected: str):
    trip = json.loads((EXAMPLES / filename).read_text(encoding="utf-8"))
    response = client.post("/trip-plans", json=trip)
    if response.status_code != 202:
        raise RuntimeError(f"Submit returned {response.status_code}: {response.text}")
    receipt = response.json()
    location = f"/trip-plans/{receipt['id']}"
    if receipt["status"] != "QUEUED" or response.headers.get("Location") != location:
        raise RuntimeError(f"Unexpected job receipt: {receipt}")

    deadline = monotonic() + 60
    while monotonic() < deadline:
        job = client.get(location).raise_for_status().json()
        if job["status"] == "FAILED":
            raise RuntimeError(f"{filename} job {job['id']} failed: {job['error']}")
        if job["status"] == "SUCCEEDED":
            result = job["result"]
            if result["status"] != expected or result["mode"] != "live" or not result["route"]:
                raise RuntimeError(f"{filename}: expected a live {expected} route, got {result['status']}")
            print(f"{job['id']} SUCCEEDED: {result['status']} ({result['route']['distance_km']} km)")
            return result
        sleep(0.5)
    raise RuntimeError(f"{filename} job {receipt['id']} did not finish in time")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    with httpx.Client(base_url=args.api_url, timeout=5) as client:
        check_trip(client, "direct-trip.json", "direct")
        fallback = check_trip(client, "trip.json", "prices_unavailable")
        if not fallback["station"] or fallback["estimated_cost_eur"] is not None:
            raise RuntimeError("The fallback must include a station and have no estimated cost")
        print(f"Fallback: {fallback['station']['name']}, buy {fallback['litres_to_buy']} L")


if __name__ == "__main__":
    main()
