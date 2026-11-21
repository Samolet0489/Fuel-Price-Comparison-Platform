import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from fuel_stop.models import Point, Quote, Route, Station, Trip
from fuel_stop.planner import BorderRestrictedRoute, DependencyUnavailable, NoRoute, plan

SAVED = json.loads((Path(__file__).parent / "fixtures/trips.json").read_text())
NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)


class Replay:
    source = "SYNTHETIC TEST DATA"
    retrieved_at = NOW

    def __init__(self, case):
        self.rows = case["stations"]
        self.trip = Trip.model_validate({**SAVED["trip"], **case.get("trip", {})})
        self.points = {r["id"]: Point(lon=15 + i / 100, lat=48) for i, r in enumerate(self.rows)}

    def validate_trip(self, trip):
        pass  # Live country validation is separately covered by adapter tests.

    def candidates(self, trip, direct):
        return [Station(id=r["id"], name=r["id"], point=self.points[r["id"]],
                        fuel_types=[r.get("fuel", "diesel")], opening_hours=r.get("hours", "24/7"),
                        source=self.source) for r in self.rows]

    def route(self, points):
        if len(points) == 2:
            legs, duration = [Decimal(200)], 1000
        else:
            row = next(r for r in self.rows if self.points[r["id"]] == points[1])
            if row.get("no_route"):
                raise NoRoute()
            legs = [Decimal(str(x)) for x in row["legs"]]
            duration = row.get("duration_s", 1000)
        return Route(distance_km=sum(legs), duration_s=duration, leg_distances_km=legs,
                     geometry={"type": "LineString", "coordinates": [p.coordinates() for p in points]},
                     attribution="SYNTHETIC: straight lines, not navigation geometry")

    def quotes(self, fuel_type):
        return [Quote(station_id=r["id"], fuel_type=r.get("fuel", "diesel"), eur_per_l=r["price"],
                      observed_at=NOW - timedelta(seconds=r.get("age_s", 0)))
                for r in self.rows if "price" in r]


@pytest.mark.parametrize("case", SAVED["cases"], ids=lambda c: c["name"])
def test_saved_cases(case):
    replay = Replay(case)
    result = plan(replay.trip, replay, replay, replay, mode="replay", now=NOW)
    assert result.status == case["expected"]
    if "winner" in case:
        assert result.station.id == case["winner"]
        assert result.arrival_fuel_l >= replay.trip.arrival_reserve_l
        assert result.ranking_complete == case.get("complete", True)
    if "litres" in case:
        assert result.litres_to_buy == Decimal(case["litres"])
    if "cost" in case:
        assert result.estimated_cost_eur == Decimal(case["cost"])
    if result.status == "direct":
        assert result.stations_compared == 0
    if case["name"] == "partial_prices":
        assert (result.stations_compared, result.feasible_stations, result.priced_stations) == (2, 2, 1)


def test_routing_failure_does_not_claim_a_winner():
    replay = Replay(SAVED["cases"][1])
    class FailingRouter:
        def validate_trip(self, trip):
            replay.validate_trip(trip)

        def route(self, points):
            if len(points) == 3:
                raise DependencyUnavailable("routing")
            return replay.route(points)
    with pytest.raises(DependencyUnavailable, match="routing"):
        plan(replay.trip, FailingRouter(), replay, replay, now=NOW)


def test_price_failure_is_distinct_from_infeasible():
    replay = Replay(SAVED["cases"][1])
    class MissingPrices:
        source = "not configured"
        retrieved_at = None
        def quotes(self, fuel_type):
            raise DependencyUnavailable("prices")
    result = plan(replay.trip, replay, replay, MissingPrices(), now=NOW)
    assert result.status == "prices_unavailable"
    assert result.feasible_stations == 2


def test_station_route_crossing_border_is_excluded():
    replay = Replay(SAVED["cases"][1])
    original = replay.route
    def restricted(points):
        if len(points) == 3 and points[1] == replay.points["best-cost"]:
            raise BorderRestrictedRoute()
        return original(points)
    replay.route = restricted
    result = plan(replay.trip, replay, replay, replay, mode="replay", now=NOW)
    assert result.status == "stop"
    assert result.station.id == "cheap-pump"
    assert result.stations_compared == 2
    assert result.feasible_stations == 1


@pytest.mark.parametrize("change", [
    {"current_fuel_l": 41}, {"consumption_l_per_100km": 0},
    {"arrival_reserve_l": -1}, {"minimum_buffer_l": 41},
    {"max_detour_km": -1}, {"fuel_type": "electric"},
    {"current_fuel_l": "NaN"}, {"typo_field": 1},
])
def test_invalid_input(change):
    with pytest.raises(ValidationError):
        Trip.model_validate({**SAVED["trip"], **change})


if __name__ == "__main__":
    replay = Replay(SAVED["cases"][1])
    print(plan(replay.trip, replay, replay, replay, mode="replay", now=NOW).model_dump_json(indent=2))
