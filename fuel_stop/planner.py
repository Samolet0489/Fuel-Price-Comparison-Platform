"""Deterministic calculation layer. No HTTP, credentials, or Kubernetes here."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from time import monotonic
from typing import Protocol

from .models import FuelStop, Plan, Point, PricedStop, Quote, Route, Station, Trip
from .multi_stop import RefuelingSearch, SearchTimedOut, road_distance_lower_bound


class DependencyUnavailable(Exception):
    def __init__(self, dependency: str, reason: str | None = None):
        self.dependency = dependency
        self.reason = reason
        super().__init__(dependency)


class OutsideAustria(Exception):
    pass


class NoRoute(Exception):
    pass


class BorderRestrictedRoute(NoRoute):
    """OSRM's returned route leaves the local Austrian boundary."""


class Router(Protocol):
    def validate_trip(self, trip: Trip) -> None: ...
    def route(self, points: list[Point]) -> Route: ...


class Stations(Protocol):
    def candidates(self, trip: Trip, direct: Route) -> list[Station]: ...


class Prices(Protocol):
    source: str
    retrieved_at: datetime | None
    def quotes(self, fuel_type: str) -> list[Quote]: ...


def plan(trip: Trip, router: Router, stations: Stations, prices: Prices,
         *, mode="live", now: datetime | None = None, max_price_age_s=3600) -> Plan:
    # Leave time for the worker to return its result before the attempt expires.
    search_deadline = monotonic() + 24
    router.validate_trip(trip)
    direct = router.route([trip.origin, trip.destination])
    rate = trip.consumption_l_per_100km / Decimal(100)
    direct_arrival = trip.current_fuel_l - direct.distance_km * rate
    if direct_arrival >= trip.arrival_reserve_l:
        return Plan(status="direct", mode=mode, route=direct, litres_to_buy=0,
                    estimated_cost_eur=0, arrival_fuel_l=direct_arrival, detour_km=0,
                    message="Destination reachable with the requested arrival reserve")

    feasible = []
    compared = set()
    route_candidates = getattr(stations, "route_candidates", None)
    single_stop_fuel = trip.current_fuel_l + trip.tank_capacity_l - trip.minimum_buffer_l - trip.arrival_reserve_l
    can_use_one_stop = road_distance_lower_bound(trip.origin, trip.destination) * rate <= single_stop_fuel
    initial_candidates = stations.candidates(trip, direct) if route_candidates is None or can_use_one_stop else []
    for station in initial_candidates:
        if trip.fuel_type not in station.fuel_types or station.opening_hours != "24/7":
            continue
        compared.add(station.id)
        try:
            route = router.route([trip.origin, station.point, trip.destination])
        except NoRoute:
            continue
        if len(route.leg_distances_km) != 2:
            raise DependencyUnavailable("routing")
        remaining = trip.current_fuel_l - route.leg_distances_km[0] * rate
        detour = max(Decimal(0), route.distance_km - direct.distance_km)
        if remaining < trip.minimum_buffer_l:
            continue
        if trip.max_detour_km is not None and detour > trip.max_detour_km:
            continue
        needed = max(Decimal(0), route.leg_distances_km[1] * rate + trip.arrival_reserve_l - remaining)
        # Buy whole millilitres, rounding UP so reserve is never rounded away.
        buy = needed.quantize(Decimal("0.001"), rounding=ROUND_CEILING)
        if remaining + buy > trip.tank_capacity_l:
            continue
        arrival = remaining + buy - route.leg_distances_km[1] * rate
        feasible.append((station, route, buy, arrival, detour))

    common = dict(mode=mode, stations_compared=len(compared), feasible_stations=len(feasible),
                  station_source=getattr(stations, "source", None),
                  station_data_timestamp=getattr(stations, "data_timestamp", None))
    search = None
    fallback = None
    if not feasible and route_candidates is not None:
        search = make_search(trip, router, direct, route_candidates(trip, direct), search_deadline)
        try:
            fallback = search.search()
        except SearchTimedOut:
            raise DependencyUnavailable("planning_search", "multi_stop_search_timeout") from None
    if not feasible and fallback is None:
        if search is not None:
            common["stations_compared"] = len(compared | search.checked)
        return Plan(status="no_feasible_stop", **common,
                    message="No refueling plan satisfies reachability, tank, fuel, opening and detour constraints")
    try:
        quotes = prices.quotes(trip.fuel_type)
    except DependencyUnavailable as exc:
        if exc.dependency != "prices":
            raise
        quotes = []
    # Check freshness at lookup completion: a quote published while routes were
    # being calculated is not a future quote.
    now = now or datetime.now(timezone.utc)
    valid = {}
    for quote in quotes:
        age = (now - quote.observed_at).total_seconds()
        if quote.fuel_type == trip.fuel_type and 0 <= age <= max_price_age_s:
            old = valid.get(quote.station_id)
            if old is None or old.observed_at < quote.observed_at:
                valid[quote.station_id] = quote
    ranked = []
    for station, route, buy, arrival, detour in feasible:
        quote = valid.get(station.id)
        if quote:
            cost = buy * quote.eur_per_l
            ranked.append((cost, route.distance_km * rate, route.duration_s, station.id,
                           station, route, buy, arrival, detour, quote))
    common.update(priced_stations=len(ranked), ranking_complete=len(ranked) == len(feasible),
                  price_source=prices.source, price_retrieved_at=prices.retrieved_at)
    ranked.sort(key=lambda row: row[:4])
    chain = search.result(fallback) if fallback is not None else None
    known_cost = ranked[0][0] if ranked else (chain_cost(chain[0], valid) if chain else None)
    if valid and route_candidates is not None:
        if search is None:
            search = make_search(trip, router, direct, route_candidates(trip, direct), search_deadline)
        candidate_ids = {search.stations[i].id for i in search.possible_stations()}
        common["ranking_complete"] = common["ranking_complete"] and candidate_ids <= valid.keys()
        if len(candidate_ids & valid.keys()) >= 2:
            try:
                priced = search.search(valid, beat_cost=known_cost)
            except SearchTimedOut:
                priced = None
                common["ranking_complete"] = False
            if priced is not None:
                chain = search.result(priced, optimized=True)
    if chain is not None:
        return chain_plan(search, chain, direct, common, compared, feasible, ranked, valid)
    if search is not None:
        common["stations_compared"] = len(compared | search.checked)
    if not ranked:
        station, route, buy, arrival, detour = min(
            feasible, key=lambda stop: (stop[4], stop[1].duration_s, stop[0].id)
        )
        return Plan(status="prices_unavailable", **common, station=station, route=route,
                    stops=[single_stop(station, route, trip, buy)],
                    litres_to_buy=buy, arrival_fuel_l=arrival, detour_km=detour,
                    message="No usable price quotes; showing the least-detour feasible stop")

    cost, _, _, _, station, route, buy, arrival, detour, quote = ranked[0]
    cents = Decimal("0.01")
    estimated_cost = cost.quantize(cents, rounding=ROUND_HALF_UP)
    runner_up = savings = None
    if len(ranked) > 1:
        next_cost, _, _, _, next_station, next_route, next_buy, next_arrival, next_detour, next_quote = ranked[1]
        runner_up = PricedStop(
            station=next_station, route=next_route, litres_to_buy=next_buy,
            estimated_cost_eur=next_cost.quantize(cents, rounding=ROUND_HALF_UP),
            arrival_fuel_l=next_arrival, detour_km=next_detour,
            price_eur_per_l=next_quote.eur_per_l, price_timestamp=next_quote.observed_at,
        )
        savings = runner_up.estimated_cost_eur - estimated_cost
    return Plan(status="stop", **common, station=station, route=route,
                stops=[single_stop(station, route, trip, buy, quote)],
                litres_to_buy=buy, estimated_cost_eur=estimated_cost, runner_up=runner_up, savings_eur=savings,
                arrival_fuel_l=arrival, detour_km=detour, price_eur_per_l=quote.eur_per_l,
                price_timestamp=quote.observed_at,
                message="Lowest purchase cost among priced feasible candidates; check ranking_complete for missing prices")


def single_stop(station, route, trip, buy, quote=None):
    arrival = trip.current_fuel_l - route.leg_distances_km[0] * trip.consumption_l_per_100km / 100
    return FuelStop(station=station, arrival_fuel_l=arrival, litres_to_buy=buy, departure_fuel_l=arrival + buy,
                    price_eur_per_l=quote.eur_per_l if quote else None,
                    price_timestamp=quote.observed_at if quote else None)


def make_search(trip, router, direct, candidates, deadline):
    def road_leg(start, end):
        try:
            return router.route([start, end])
        except NoRoute:
            return None

    eligible = [station for station in candidates
                if trip.fuel_type in station.fuel_types and station.opening_hours == "24/7"]
    return RefuelingSearch(trip, direct, eligible, road_leg, deadline, getattr(router, "distance_table", None))


def chain_cost(stops, quotes):
    if any(stop.station.id not in quotes for stop in stops):
        return None
    return sum((stop.litres_to_buy * quotes[stop.station.id].eur_per_l for stop in stops), Decimal(0))


def chain_plan(search, chain, direct, common, compared, feasible, ranked, quotes):
    stops, route, fuel = chain
    detour = max(Decimal(0), route.distance_km - direct.distance_km)
    if not stops:
        return Plan(status="direct", mode=common["mode"], route=route, litres_to_buy=0,
                    estimated_cost_eur=0, arrival_fuel_l=fuel, detour_km=detour,
                    message="Destination reachable with the requested arrival reserve")
    total = chain_cost(stops, quotes)
    priced = total is not None
    cost = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if priced else None
    for stop in stops:
        quote = quotes.get(stop.station.id)
        if quote is not None:
            stop.price_eur_per_l = quote.eur_per_l
            stop.price_timestamp = quote.observed_at
    first = stops[0] if len(stops) == 1 else None
    stop_ids = {stop.station.id for stop in stops}
    coverage = {
        **common,
        "ranking_complete": priced and common["ranking_complete"],
        "stations_compared": len(compared | search.checked),
        "feasible_stations": len(stop_ids | {row[0].id for row in feasible}),
        "priced_stations": len((stop_ids & quotes.keys()) | {row[4].id for row in ranked}),
    }
    return Plan(status="stop" if priced else "prices_unavailable", **coverage,
                station=first.station if first else None, stops=stops, route=route,
                litres_to_buy=sum(stop.litres_to_buy for stop in stops),
                arrival_fuel_l=fuel, detour_km=detour,
                estimated_cost_eur=cost, price_eur_per_l=first.price_eur_per_l if first else None,
                price_timestamp=first.price_timestamp if first else None,
                message=("Lowest purchase cost found among routes with usable prices" if priced
                         else "No fully priced plan found; showing a feasible refueling route"))
