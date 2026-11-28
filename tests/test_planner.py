import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from fuel_stop.models import Point, Quote, Route, Station, Trip
from fuel_stop.app import price_provider
from fuel_stop.multi_stop import RefuelingSearch, SearchTimedOut
from fuel_stop.planner import BorderRestrictedRoute, DependencyUnavailable, NoRoute, plan
from fuel_stop.providers import AuthorisedPrices
from scripts.import_fuel_prices import import_prices
from scripts.refresh_tomtom_prices import refresh

SAVED = json.loads((Path(__file__).parent / "fixtures/trips.json").read_text())
NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)


class Replay:
    source = "SYNTHETIC TEST DATA"
    retrieved_at = NOW

    def __init__(self, case):
        self.rows = case["stations"]
        self.direct_km = Decimal(case.get("direct_km", 200))
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
            legs, duration = [self.direct_km], 1000
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
        assert result.runner_up is None
        assert result.savings_eur is None
    if result.status == "prices_unavailable":
        assert result.station.id == case["stations"][0]["id"]
        assert result.route.distance_km == Decimal(200)
        assert result.litres_to_buy == Decimal("7.000")
        assert result.estimated_cost_eur is None
        assert result.price_timestamp is None
        assert result.runner_up is None
        assert result.savings_eur is None
    if result.status == "no_feasible_stop":
        assert result.station is None
        assert result.route is None
        assert result.runner_up is None
        assert result.savings_eur is None


def test_build_guide_cost_comparison():
    replay = Replay({
        "direct_km": 185,
        "trip": {"current_fuel_l": "10"},
        "stations": [
            {"id": "B", "legs": [85, 120], "price": "1.60"},
            {"id": "A", "legs": [75, 115], "price": "1.75"},
        ],
    })
    result = plan(replay.trip, replay, replay, replay, mode="replay", now=NOW)
    assert result.status == "stop"
    assert result.station.id == "A"
    assert result.litres_to_buy == Decimal("4.400")
    assert result.estimated_cost_eur == Decimal("7.70")
    assert result.runner_up.station.id == "B"
    assert result.runner_up.litres_to_buy == Decimal("5.300")
    assert result.runner_up.estimated_cost_eur == Decimal("8.48")
    assert result.runner_up.route.distance_km == Decimal(205)
    assert result.runner_up.detour_km == Decimal(20)
    assert result.savings_eur == Decimal("0.78")


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
    assert result.station.id == "best-cost"
    assert result.detour_km == 0
    assert result.arrival_fuel_l == Decimal("3")
    assert result.estimated_cost_eur is None


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


class ChainReplay:
    source = "synthetic"
    retrieved_at = NOW

    def __init__(self, distance, roads, prices=None, **trip_settings):
        self.points = [Point(lon=14 + index / 100, lat=48) for index in range(1 + max(max(link) for link in roads))]
        self.roads = {(0, len(self.points) - 1): distance, **roads}
        self.prices = prices or {}
        self.trip = Trip.model_validate({
            "origin": self.points[0], "destination": self.points[-1], "fuel_type": "diesel",
            "current_fuel_l": 5, "tank_capacity_l": 10, "consumption_l_per_100km": 10,
            "arrival_reserve_l": 2, "minimum_buffer_l": 1, "max_detour_km": 0, **trip_settings,
        })

    def validate_trip(self, trip):
        pass

    def route_candidates(self, trip, direct):
        return [Station(id=str(i), name=f"Station {i}", point=point, fuel_types=["diesel"], opening_hours="24/7")
                for i, point in enumerate(self.points[1:-1], 1)]

    def candidates(self, trip, direct):
        # Only stations directly reachable from the origin are initial candidates.
        return [station for station in self.route_candidates(trip, direct) if (0, int(station.id)) in self.roads]

    def route(self, points):
        nodes = [self.points.index(point) for point in points]
        try:
            legs = [self.roads[a, b] for a, b in zip(nodes, nodes[1:])]
        except KeyError:
            raise NoRoute() from None
        return Route(distance_km=sum(legs), duration_s=sum(legs) * 60, leg_distances_km=legs,
                     geometry={"type": "LineString", "coordinates": [p.coordinates() for p in points]},
                     attribution="synthetic")

    def quotes(self, fuel_type):
        return [Quote(station_id=str(node), fuel_type=fuel_type, eur_per_l=price, observed_at=NOW)
                for node, price in self.prices.items()]

    def plan(self):
        return plan(self.trip, self, self, self, mode="replay", now=NOW)


@pytest.mark.parametrize("number_of_stops", [2, 20])
def test_required_refueling_chain_without_prices(number_of_stops):
    roads = {(0, 1): 40, **{(i, i + 1): 80 for i in range(1, number_of_stops + 1)}}
    result = ChainReplay(40 + 80 * number_of_stops, roads).plan()
    assert result.status == "prices_unavailable"
    assert result.station is None
    assert [stop.station.id for stop in result.stops] == [str(i) for i in range(1, number_of_stops + 1)]
    assert result.route.leg_distances_km == [40, *([80] * number_of_stops)]
    assert all(stop.arrival_fuel_l == 1 and stop.departure_fuel_l <= 10 for stop in result.stops)
    assert result.arrival_fuel_l == 2
    assert result.estimated_cost_eur is None


def test_a_second_stop_can_reduce_cost_even_when_one_stop_is_feasible():
    replay = ChainReplay(115, {(0, 1): 40, (1, 2): 50, (2, 3): 25, (1, 3): 75},
                         prices={1: 2, 2: 1})
    result = replay.plan()
    assert result.status == "stop"
    assert [stop.station.id for stop in result.stops] == ["1", "2"]
    assert [stop.litres_to_buy for stop in result.stops] == [5, Decimal("3.5")]
    assert result.estimated_cost_eur == Decimal("13.50")
    assert result.arrival_fuel_l == 2


def test_tomtom_snapshot_prices_a_trip_and_survives_failed_refresh(tmp_path):
    replay = ChainReplay(115, {(0, 1): 40, (1, 2): 50, (2, 3): 25, (1, 3): 75})
    mapping = {"1": "test-a", "2": "test-b"}
    snapshot = tmp_path / "prices.json"
    observed_at = NOW - timedelta(minutes=10)

    def tomtom(request):
        assert request.url.path == "/search/2/fuelPrice.json"
        assert request.url.params["key"] == "mock-key"
        assert request.headers["TomTom-Api-Version"] == "1"
        price_id = request.url.params["fuelPrice"]
        return httpx.Response(200, json={"fuelPrice": price_id, "fuels": [
            {"type": "diesel", "price": [{"value": 2 if price_id == "test-a" else 1,
                                        "currency": "EUR", "volumeUnit": "liter"}],
             "updatedAt": observed_at.isoformat()},
            {"type": "sp95_e10", "price": [{"value": 1.55, "currency": "EUR", "volumeUnit": "liter"}],
             "updatedAt": observed_at.isoformat()},
        ]})

    with httpx.Client(transport=httpx.MockTransport(tomtom)) as client:
        assert refresh(client, "mock-key", mapping, snapshot) == 4
    stored = snapshot.read_bytes()
    assert b"mock-key" not in stored
    prices = AuthorisedPrices(None, "", "test-only", "TomTom", snapshot_file=str(snapshot))
    result = plan(replay.trip, replay, replay, prices, mode="replay", now=NOW)
    assert [stop.station.id for stop in result.stops] == ["1", "2"]
    assert result.price_source == "TomTom"
    assert result.estimated_cost_eur == Decimal("13.50")
    assert all(stop.price_timestamp == observed_at for stop in result.stops)
    assert all(q.fuel_type == "petrol95" for q in prices.quotes("petrol95"))
    stale = plan(replay.trip, replay, replay, prices, mode="replay", now=NOW + timedelta(hours=2))
    assert stale.status == "prices_unavailable"

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))) as client:
        with pytest.raises(DependencyUnavailable, match="tomtom_prices"):
            refresh(client, "mock-key", mapping, snapshot)
    assert snapshot.read_bytes() == stored


def test_csv_prices_are_picked_up_without_supplier_configuration(tmp_path, monkeypatch):
    replay = ChainReplay(115, {(0, 1): 40, (1, 2): 50, (2, 3): 25, (1, 3): 75})
    csv_file = tmp_path / "observed.csv"
    snapshot = tmp_path / "local-prices.json"
    for var in ("PRICE_FEED_URL", "PRICE_SNAPSHOT_FILE", "PRICE_PERMISSION_REFERENCE", "PRICE_SOURCE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PRICE_MODE", "observed")
    monkeypatch.setenv("STATION_CATALOGUE_FILE", str(tmp_path / "stations.json"))
    assert not price_provider(None).configured
    csv_file.write_text(
        "station_id;fuel_type;eur_per_l;observed_at\n"
        "1;diesel;2,00;2026-11-21T11:50:00Z\n"
        "1;diesel;1,90;2026-11-21T10:00:00Z\n"
        "2;diesel;1,00;2026-11-21T11:50:00Z\n", encoding="utf-8-sig",
    )
    stations = replay.route_candidates(replay.trip, None)
    assert import_prices(csv_file, stations, snapshot) == 2
    prices = price_provider(None)
    result = plan(replay.trip, replay, replay, prices, mode="replay", now=NOW)
    assert [stop.station.id for stop in result.stops] == ["1", "2"]
    assert result.estimated_cost_eur == Decimal("13.50")
    assert result.price_source == "Local pump observations"
    assert plan(replay.trip, replay, replay, prices, now=NOW + timedelta(hours=2)).status == "prices_unavailable"

    stored = snapshot.read_bytes()
    with csv_file.open("a", encoding="utf-8") as stream:
        stream.write("3;diesel;1,50;2026-11-21T11:50:00Z\n")
    with pytest.raises(ValueError, match="station ID is not in the local catalogue"):
        import_prices(csv_file, stations, snapshot)
    assert snapshot.read_bytes() == stored


def test_buy_more_now_to_avoid_fuel_at_the_expensive_next_stop():
    replay = ChainReplay(140, {(0, 1): 40, (1, 2): 50, (2, 3): 50}, prices={1: 1, 2: 2})
    result = replay.plan()
    assert result.status == "stop"
    assert [stop.litres_to_buy for stop in result.stops] == [9, 2]
    assert result.stops[0].departure_fuel_l == 10
    assert result.estimated_cost_eur == 13


@pytest.mark.parametrize("use_table", [False, True])
def test_partial_fill_can_cover_multiple_legs_at_the_cheaper_price(use_table):
    roads = {(0, 1): 40, (1, 2): 50, (2, 3): 60, (3, 4): 15, (2, 4): 100}
    replay = ChainReplay(165, roads, prices={1: 2, 2: 1, 3: 3})

    if use_table:
        def distance_table(points, deadline):
            nodes = [replay.points.index(point) for point in points]
            return {(i, j): replay.roads.get((start, end))
                    for i, start in enumerate(nodes[:-1]) for j, end in enumerate(nodes[1:], 1)}

        replay.distance_table = distance_table

    result = replay.plan()

    assert result.status == "stop"
    assert [stop.station.id for stop in result.stops] == ["1", "2"]
    assert [stop.litres_to_buy for stop in result.stops] == [5, Decimal("8.5")]
    assert result.estimated_cost_eur == Decimal("18.50")
    assert result.arrival_fuel_l == 2
    assert result.route.leg_distances_km == [40, 50, 75]


@pytest.mark.parametrize("real_leg", [50, None])
def test_distance_table_proposal_is_repriced_or_discarded_after_road_verification(real_leg):
    roads = {(0, 1): 40, (2, 3): 25, (1, 3): 75}
    if real_leg is not None:
        roads[1, 2] = real_leg
    replay = ChainReplay(115, roads, prices={1: 2, 2: 1})
    probed = []

    def distance_table(points, deadline):
        nodes = [replay.points.index(point) for point in points]
        return {(i, j): Decimal(35) if (source, target) == (1, 2)
                else replay.roads.get((source, target))
                for i, source in enumerate(nodes[:-1]) for j, target in enumerate(nodes[1:], 1)}

    route = replay.route

    def checked_route(points):
        if len(points) == 2:
            probed.append(tuple(replay.points.index(point) for point in points))
        return route(points)

    replay.route = checked_route
    replay.distance_table = distance_table
    result = replay.plan()
    assert (1, 2) in probed
    assert result.ranking_complete
    if real_leg is None:
        assert [stop.station.id for stop in result.stops] == ["1"]
        assert result.estimated_cost_eur == Decimal("17.00")
    else:
        assert [stop.station.id for stop in result.stops] == ["1", "2"]
        assert result.route.leg_distances_km == [40, 50, 25]
        assert result.estimated_cost_eur == Decimal("13.50")
    assert result.arrival_fuel_l == 2


def test_priced_table_retries_with_full_fuel_states_after_no_proposal(monkeypatch):
    replay = ChainReplay(115, {(0, 1): 40, (1, 2): 50, (2, 3): 25, (1, 3): 75},
                         prices={1: 2, 2: 1})

    def distance_table(points, deadline):
        nodes = [replay.points.index(point) for point in points]
        return {(i, j): replay.roads.get((start, end))
                for i, start in enumerate(nodes[:-1]) for j, end in enumerate(nodes[1:], 1)}

    replay.distance_table = distance_table
    original_search = RefuelingSearch._search
    passes = []

    def search(self, targets, prices, optimize, beat_cost, *, merge_remainders=False):
        passes.append(merge_remainders)
        if merge_remainders:
            return None
        return original_search(self, targets, prices, optimize, beat_cost)

    monkeypatch.setattr(RefuelingSearch, "_search", search)
    result = replay.plan()

    assert passes == [True, False]
    assert result.ranking_complete
    assert result.estimated_cost_eur == Decimal("13.50")


def test_feasible_chain_keeps_its_prices_if_optimization_times_out(monkeypatch):
    replay = ChainReplay(140, {(0, 1): 40, (1, 2): 50, (2, 3): 50}, prices={1: 1, 2: 2})
    original = RefuelingSearch.search

    def search(self, quotes=None, beat_cost=None):
        if quotes is not None:
            raise SearchTimedOut()
        return original(self, quotes, beat_cost)

    monkeypatch.setattr(RefuelingSearch, "search", search)
    result = replay.plan()
    assert result.status == "stop"
    assert result.ranking_complete is False
    assert [stop.price_eur_per_l for stop in result.stops] == [1, 2]
    assert result.estimated_cost_eur == 13


def test_max_detour_applies_to_the_whole_chain():
    replay = ChainReplay(190, {(0, 1): 40, (1, 2): 80, (2, 3): 80}, max_detour_km=9)
    result = replay.plan()
    assert result.status == "no_feasible_stop"
    assert result.stops == []


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
