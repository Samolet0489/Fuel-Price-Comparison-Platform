"""Search for refueling chains using verified, directed road legs."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from heapq import heappop, heappush
from itertools import count
from time import monotonic
from typing import Callable

from .models import FuelStop, Point, Quote, Route, Station, Trip


ML = Decimal("0.001")
ZERO = Decimal(0)


def road_distance_lower_bound(a: Point, b: Point) -> Decimal:
    # OSRM can snap either endpoint up to 100 m away.
    return max(ZERO, Decimal(str(a.distance_km(b) * .99)) - Decimal("0.2"))


class SearchTimedOut(Exception):
    pass


@dataclass(eq=False)
class Leg:
    target: int
    route: Route
    bought: Decimal


@dataclass(eq=False)
class Arrival:
    node: int
    fuel: Decimal
    distance: Decimal = ZERO
    cost: Decimal = ZERO
    previous: "Arrival | None" = None
    leg: Leg | None = None


class RefuelingSearch:
    def __init__(self, trip: Trip, direct: Route, candidates: list[Station],
                 road_leg: Callable[[Point, Point], Route | None], deadline: float):
        self.trip = trip
        self.rate = trip.consumption_l_per_100km / 100
        self.limit = direct.distance_km + trip.max_detour_km if trip.max_detour_km is not None else Decimal("Infinity")
        self.stations = [None, *candidates, None]
        self.points = [trip.origin, *(station.point for station in candidates), trip.destination]
        self.destination = len(self.points) - 1
        self.deadline = deadline
        self.checked: set[str] = set()
        self.road_leg = road_leg
        self.bounds: dict[tuple[int, int], Decimal] = {}
        self.routes: dict[tuple[int, int], Route | None] = {}

    def lower_bound(self, a: int, b: int) -> Decimal:
        key = (min(a, b), max(a, b))
        if key not in self.bounds:
            self.bounds[key] = road_distance_lower_bound(self.points[a], self.points[b])
        return self.bounds[key]

    def lookup(self, a: int, b: int) -> Route | None:
        key = (a, b)
        if key not in self.routes:
            self.checked.update(station.id for station in (self.stations[a], self.stations[b]) if station is not None)
            self.routes[key] = self.road_leg(self.points[a], self.points[b])
        return self.routes[key]

    def possible_stations(self):
        for index in range(1, self.destination):
            first_leg = self.lower_bound(0, index)
            last_leg = self.lower_bound(index, self.destination)
            if first_leg + last_leg <= self.limit:
                yield index

    def search(self, quotes: dict[str, Quote] | None = None, beat_cost: Decimal | None = None) -> Arrival | None:
        """Prioritize price estimates or, without prices, progress toward the destination.

        A priced departure buys enough to reach the next stop or fills the tank.
        Carrying extra fuel pays off when it replaces purchases at pricier stops.
        Without quotes, a full departure suffices to test reachability.
        """
        destination = self.destination
        trip = self.trip
        possible = list(self.possible_stations())
        prices = {i: quotes[self.stations[i].id].eur_per_l for i in possible
                  if quotes is not None and self.stations[i].id in quotes}
        targets = [*(possible if quotes is None else prices), destination]
        minimum_price = min(prices.values(), default=ZERO)
        heap = []
        serial = count()
        arrivals = {}

        def enqueue(arrival, target=None):
            remaining = self.lower_bound(arrival.node, destination) if arrival.node != destination else ZERO
            lower_distance = arrival.distance + remaining
            distance_priority = lower_distance
            lower_cost = arrival.cost + max(ZERO, remaining * self.rate + trip.arrival_reserve_l - arrival.fuel) * minimum_price
            if target is not None:
                leg_distance = self.lower_bound(arrival.node, target)
                remaining = self.lower_bound(target, destination) if target != destination else ZERO
                distance_priority = arrival.distance + leg_distance + remaining
                lower_distance = max(lower_distance, distance_priority)
                reserve = trip.arrival_reserve_l if target == destination else trip.minimum_buffer_l
                needed = max(ZERO, leg_distance * self.rate + reserve - arrival.fuel)
                if (needed > 0 and arrival.node == 0) or arrival.fuel + needed > trip.tank_capacity_l:
                    return
                lower_cost = max(lower_cost, arrival.cost + needed * prices.get(arrival.node, ZERO))
            if lower_distance <= self.limit and (beat_cost is None or lower_cost < beat_cost):
                if quotes is None:
                    # A fallback only needs a feasible route. Favor reaching the
                    # destination quickly over proving minimum driving distance.
                    distance_priority += remaining
                heappush(heap, (lower_cost, distance_priority, next(serial), arrival, target))

        enqueue(Arrival(0, trip.current_fuel_l))
        while heap:
            if monotonic() >= self.deadline:
                raise SearchTimedOut()
            _, _, _, arrival, target = heappop(heap)
            if arrival.node == destination:
                return arrival
            end = tuple(arrival.leg.route.geometry["coordinates"][-1]) if arrival.leg else ()
            # Whole-millilitre purchases leave this remainder invariant for a
            # given path. Compare labels with the same attainable fuel levels.
            key = (arrival.node, end, arrival.fuel % ML)
            frontier = arrivals.setdefault(key, [])
            if target is None:
                if any(a.cost <= arrival.cost and a.distance <= arrival.distance and a.fuel >= arrival.fuel
                       for a in frontier):
                    continue
                frontier[:] = [a for a in frontier if not (
                    arrival.cost <= a.cost and arrival.distance <= a.distance and arrival.fuel >= a.fuel)]
                frontier.append(arrival)
                for node in targets:
                    if node != arrival.node and (arrival.node != 0 or node != destination):
                        enqueue(arrival, node)
                continue
            if arrival not in frontier:
                continue

            route = self.lookup(arrival.node, target)
            if route is None or (end and end != tuple(route.geometry["coordinates"][0])):
                continue
            reserve = trip.arrival_reserve_l if target == destination else trip.minimum_buffer_l
            remaining = arrival.fuel - route.distance_km * self.rate
            minimum = max(ZERO, reserve - remaining).quantize(ML, rounding=ROUND_CEILING)
            maximum = (trip.tank_capacity_l - arrival.fuel).quantize(ML, rounding=ROUND_FLOOR)
            if arrival.node == 0:
                maximum = ZERO
            if minimum > maximum:
                continue
            amounts = (minimum, maximum) if quotes is not None and target != destination and minimum < maximum else (
                minimum if quotes is not None or target == destination else maximum,)
            for buy in amounts:
                enqueue(Arrival(target, remaining + buy, arrival.distance + route.distance_km,
                                arrival.cost + buy * prices.get(arrival.node, ZERO),
                                arrival, Leg(target, route, buy)))
        return None

    def result(self, arrival: Arrival, *, optimized: bool = False):
        legs = []
        while arrival.leg is not None:
            legs.append(arrival.leg)
            arrival = arrival.previous
        stops = []
        coordinates = []
        distances = []
        distance_since_stop = duration = ZERO
        fuel = self.trip.current_fuel_l
        node = 0
        for leg in reversed(legs):
            distance = leg.route.distance_km
            burn = distance * self.rate
            reserve = self.trip.arrival_reserve_l if leg.target == self.destination else self.trip.minimum_buffer_l
            buy = leg.bought if optimized else max(ZERO, burn + reserve - fuel).quantize(ML, rounding=ROUND_CEILING)
            station = self.stations[node]
            if station is not None and buy > 0:
                stops.append(FuelStop(station=station, arrival_fuel_l=fuel, litres_to_buy=buy,
                                      departure_fuel_l=fuel + buy))
                distances.append(distance_since_stop)
                distance_since_stop = ZERO
            fuel += buy - burn
            node = leg.target
            duration += leg.route.duration_s
            distance_since_stop += distance
            points = leg.route.geometry["coordinates"]
            coordinates.extend(points[1:] if coordinates else points)
        distances.append(distance_since_stop)
        route = Route(distance_km=sum(distances), duration_s=duration, leg_distances_km=distances,
                      geometry={"type": "LineString", "coordinates": coordinates}, attribution=legs[0].route.attribution)
        return stops, route, fuel
