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
    distance_km: Decimal
    bought: Decimal
    route: Route | None


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
                 road_leg: Callable[[Point, Point], Route | None], deadline: float,
                 road_table: Callable[[list[Point], float], dict[tuple[int, int], Decimal | None] | None] | None = None):
        self.trip = trip
        self.rate = trip.consumption_l_per_100km / 100
        self.limit = direct.distance_km + trip.max_detour_km if trip.max_detour_km is not None else Decimal("Infinity")
        self.stations = [None, *candidates, None]
        self.points = [trip.origin, *(station.point for station in candidates), trip.destination]
        self.destination = len(self.points) - 1
        self.deadline = deadline
        self.checked: set[str] = set()
        self.road_leg = road_leg
        self.road_table = road_table
        self.bounds: dict[tuple[int, int], Decimal] = {}
        self.routes: dict[tuple[int, int], Route | None] = {}
        self.table: dict[tuple[int, int], Decimal | None] | None = None

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
        self.table = None
        possible = list(self.possible_stations())
        prices = {i: quotes[self.stations[i].id].eur_per_l for i in possible
                  if quotes is not None and self.stations[i].id in quotes}
        targets = [*(possible if quotes is None else prices), self.destination]
        if quotes is not None and self.road_table is not None:
            nodes = [0, *targets]
            distances = self.road_table([self.points[i] for i in nodes], self.deadline)
            if distances is not None:
                self.table = {(nodes[a], nodes[b]): distance for (a, b), distance in distances.items()}
                self.checked.update(self.stations[i].id for i in prices)
        while True:
            winner = self._search(targets, prices, quotes is not None, beat_cost)
            if self.table is None or winner is None or self.verify(winner):
                return winner

    def edge_distance(self, source: int, target: int) -> Decimal | None:
        key = (source, target)
        if key in self.routes:
            route = self.routes[key]
            return route.distance_km if route is not None else None
        if self.table is not None:
            return self.table[key]
        return self.lower_bound(source, target)

    def remaining_distances(self, targets: list[int], seeds=None) -> dict[int, Decimal]:
        """Shortest remaining distance through the directed table, ignoring fuel and borders."""
        distances = {self.destination: ZERO} if seeds is None else dict(seeds)
        heap = []
        for target, distance in distances.items():
            heappush(heap, (distance, target))
        sources = [0, *targets[:-1]]
        while heap:
            remaining, target = heappop(heap)
            if target == 0 or remaining != distances[target]:
                continue
            for source in sources:
                distance = self.edge_distance(source, target)
                if distance is None:
                    continue
                total = distance + remaining
                if source not in distances or total < distances[source]:
                    distances[source] = total
                    heappush(heap, (total, source))
        return distances

    def price_bounds(self, targets, prices, remaining, beat_cost):
        """Bound purchase cost by the distance required to reach cheaper fuel."""
        required = remaining.get(0, ZERO) * self.rate + self.trip.arrival_reserve_l - self.trip.current_fuel_l
        if beat_cost is None or required <= 0:
            return []
        limit = beat_cost / required
        cheap = sorted({price for price in prices.values() if price < limit})
        if not cheap:
            return []
        # Split the competitive prices into a few bands. Any continuation
        # that buys fuel visits at least one station at its lowest price.
        band_size = (len(cheap) + 3) // 4
        bands = []
        for first in range(0, len(cheap), band_size):
            low, high = cheap[first], cheap[min(first + band_size, len(cheap)) - 1]
            seeds = {node: remaining[node] for node, price in prices.items()
                     if low <= price <= high and node in remaining}
            bands.append((low, self.remaining_distances(targets, seeds)))
        higher = min((price for price in prices.values() if price >= limit), default=None)
        if higher is not None:
            bands.append((higher, remaining))
        return bands

    def verify(self, arrival: Arrival) -> bool:
        # Table routes have distances but no geometry. Cache the full route for
        # each leg and rerun the search if the verified route changes the graph.
        valid = True
        next_start = None
        while arrival.leg is not None:
            if monotonic() >= self.deadline:
                raise SearchTimedOut()
            leg = arrival.leg
            route = self.lookup(arrival.previous.node, arrival.node)
            if route is None:
                valid = False
                next_start = None
            else:
                points = route.geometry["coordinates"]
                if leg.distance_km != route.distance_km or (next_start is not None and points[-1] != next_start):
                    valid = False
                leg.route = route
                next_start = points[0]
            arrival = arrival.previous
        return valid

    def _search(self, targets, prices, optimize, beat_cost) -> Arrival | None:
        """Prioritize price estimates or, without prices, progress toward the destination.

        A priced departure buys enough to reach the next stop or fills the tank.
        Carrying extra fuel pays off when it replaces purchases at pricier stops.
        Without quotes, a full departure suffices to test reachability.
        """
        destination = self.destination
        trip = self.trip
        minimum_price = min(prices.values(), default=ZERO)
        # If the incumbent caps total purchases below the spare tank capacity,
        # more remaining fuel dominates across millilitre remainders too.
        capacity_can_bind = (beat_cost is None or
                             (trip.tank_capacity_l - trip.current_fuel_l) * minimum_price < beat_cost)
        remaining_distances = self.remaining_distances(targets) if self.table is not None else None
        price_bounds = self.price_bounds(targets, prices, remaining_distances, beat_cost) if remaining_distances else []
        heap = []
        serial = count()
        arrivals = {}

        def remaining_from(node):
            if node == destination:
                return ZERO
            if remaining_distances is not None:
                return remaining_distances.get(node)
            return self.lower_bound(node, destination)

        def enqueue(arrival, target=None):
            remaining = remaining_from(arrival.node)
            if remaining is None:
                return
            lower_distance = arrival.distance + remaining
            distance_priority = lower_distance
            required_purchase = ZERO
            premium = ZERO
            if target is not None:
                leg_distance = self.edge_distance(arrival.node, target)
                remaining = remaining_from(target)
                if leg_distance is None or remaining is None:
                    return
                distance_priority = arrival.distance + leg_distance + remaining
                lower_distance = max(lower_distance, distance_priority)
                reserve = trip.arrival_reserve_l if target == destination else trip.minimum_buffer_l
                required_purchase = max(ZERO, leg_distance * self.rate + reserve - arrival.fuel)
                if ((required_purchase > 0 and arrival.node == 0)
                        or arrival.fuel + required_purchase > trip.tank_capacity_l):
                    return
                if required_purchase:
                    premium = required_purchase * (prices.get(arrival.node, minimum_price) - minimum_price)
            total_purchase = max(required_purchase, (lower_distance - arrival.distance) * self.rate
                                 + trip.arrival_reserve_l - arrival.fuel)
            lower_cost = arrival.cost + total_purchase * minimum_price + premium
            if target is None and total_purchase > 0 and price_bounds:
                closer_cost = min(
                    ((distances[arrival.node] * self.rate + trip.arrival_reserve_l - arrival.fuel) * price
                     for price, distances in price_bounds if arrival.node in distances),
                    default=Decimal("Infinity"),
                )
                lower_cost = max(lower_cost, arrival.cost + closer_cost)
            if lower_distance <= self.limit and (beat_cost is None or lower_cost < beat_cost):
                if not optimize:
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
            end = ()
            if arrival.leg:
                route = arrival.leg.route
                end = tuple(route.geometry["coordinates"][-1]) if route else (arrival.previous.node,)
            # Whole-millilitre purchases leave this remainder invariant for a
            # given path. Compare labels with the same attainable fuel levels.
            remainder = arrival.fuel % ML if capacity_can_bind else ZERO
            key = (arrival.node, end, remainder)
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

            key = (arrival.node, target)
            if self.table is None:
                self.lookup(*key)
            distance = self.edge_distance(*key)
            route = self.routes.get(key)
            if distance is None or (route and len(end) == 2 and end != tuple(route.geometry["coordinates"][0])):
                continue
            reserve = trip.arrival_reserve_l if target == destination else trip.minimum_buffer_l
            remaining = arrival.fuel - distance * self.rate
            minimum = max(ZERO, reserve - remaining).quantize(ML, rounding=ROUND_CEILING)
            maximum = (trip.tank_capacity_l - arrival.fuel).quantize(ML, rounding=ROUND_FLOOR)
            if arrival.node == 0:
                maximum = ZERO
            if minimum > maximum:
                continue
            amounts = (minimum, maximum) if optimize and target != destination and minimum < maximum else (
                minimum if optimize or target == destination else maximum,)
            for buy in amounts:
                enqueue(Arrival(target, remaining + buy, arrival.distance + distance,
                                arrival.cost + buy * prices.get(arrival.node, ZERO),
                                arrival, Leg(target, distance, buy, route)))
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
