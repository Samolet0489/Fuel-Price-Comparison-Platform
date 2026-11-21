"""Deterministic calculation layer. No HTTP, credentials, or Kubernetes here."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Protocol

from .models import Plan, Point, PricedStop, Quote, Route, Station, Trip


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
    router.validate_trip(trip)
    direct = router.route([trip.origin, trip.destination])
    rate = trip.consumption_l_per_100km / Decimal(100)
    direct_arrival = trip.current_fuel_l - direct.distance_km * rate
    if direct_arrival >= trip.arrival_reserve_l:
        return Plan(status="direct", mode=mode, route=direct, litres_to_buy=0,
                    estimated_cost_eur=0, arrival_fuel_l=direct_arrival, detour_km=0,
                    message="Destination reachable with the requested arrival reserve")

    feasible = []
    compared = 0
    for station in stations.candidates(trip, direct):
        if trip.fuel_type not in station.fuel_types or station.opening_hours != "24/7":
            continue
        compared += 1
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

    common = dict(mode=mode, stations_compared=compared, feasible_stations=len(feasible),
                  station_source=getattr(stations, "source", None),
                  station_data_timestamp=getattr(stations, "data_timestamp", None))
    if not feasible:
        return Plan(status="no_feasible_stop", **common,
                    message="No single stop satisfies reachability, tank, fuel, opening and detour constraints")
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
    if not ranked:
        station, route, buy, arrival, detour = min(
            feasible, key=lambda stop: (stop[4], stop[1].duration_s, stop[0].id)
        )
        return Plan(status="prices_unavailable", **common, station=station, route=route,
                    litres_to_buy=buy, arrival_fuel_l=arrival, detour_km=detour,
                    message="No usable price quotes; showing the least-detour feasible stop")

    ranked.sort(key=lambda row: row[:4])
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
                litres_to_buy=buy, estimated_cost_eur=estimated_cost, runner_up=runner_up, savings_eur=savings,
                arrival_fuel_l=arrival, detour_km=detour, price_eur_per_l=quote.eur_per_l,
                price_timestamp=quote.observed_at,
                message="Lowest purchase cost among priced feasible candidates; check ranking_complete for missing prices")
