"""Real route check independent of Overpass and price access.

python -m scripts.check_local_route [--url http://127.0.0.1:8000] [--wait-seconds 120]
"""
import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--wait-seconds", type=float, default=0)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    trip = json.loads((root / "examples/direct-trip.json").read_text())
    deadline = time.monotonic() + args.wait_seconds
    with httpx.Client(timeout=35) as client:
        while True:
            try:
                response = client.post(args.url.rstrip("/") + "/plan", json=trip)
                response.raise_for_status()
                result = response.json()
                assert result["status"] == "direct", result
                assert result["mode"] == "live" and result["stations_compared"] == 0
                assert result["route"]["geometry"]["type"] == "LineString"
                print(json.dumps({"status": result["status"], "mode": result["mode"],
                    "distance_km": result["route"]["distance_km"],
                    "duration_s": result["route"]["duration_s"],
                    "geometry_points": len(result["route"]["geometry"]["coordinates"]),
                    "arrival_fuel_l": result["arrival_fuel_l"], "stations_compared": 0}, indent=2))
                return
            except (httpx.HTTPError, ValueError, AssertionError) as exc:
                if time.monotonic() >= deadline:
                    if isinstance(exc, httpx.HTTPStatusError):
                        print(exc.response.text)
                    raise SystemExit(f"Local route check failed: {exc}") from None
                time.sleep(2)


if __name__ == "__main__":
    main()
