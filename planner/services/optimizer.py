"""Minimum-cost refuelling plan along a fixed route.

Problem
-------
Stations sit at known mile markers ``d_i`` along the route with price ``p_i``.
The tank holds ``R`` miles of fuel (500 mi = 50 gal at 10 mpg) and starts
with ``F`` miles of fuel. Choose how much to buy where, so the vehicle never
runs dry, and total spend is minimal. Partial fill-ups are allowed.

Key insight (why this is exactly optimal, not a heuristic)
----------------------------------------------------------
Think of the trip as a sequence of individual miles. Fuel bought at station
``s`` can only ever burn on miles inside ``[d_s, d_s + R]``: you can't use it
before you buy it, and a tank of size R is empty again R miles later.

* Lower bound: every mile ``x`` must be powered by fuel from some station in
  the window ``[x - R, x]``, so it costs at least the cheapest price in that
  window.
* Achievable: assigning each mile to the cheapest station in its window never
  overfills the tank (at any point y, fuel held covers only miles in
  ``(y, y + R]``), and never runs dry (each mile's fuel is bought before it).

So: optimal cost = integral over the route of the sliding-window minimum
price. The window minimum only changes at ``d_i`` or ``d_i + R``, so we sweep
those breakpoints with a heap -> O(n log n) for n candidate stations
(typically a few hundred), i.e. well under a millisecond.

A free starting tank of F miles covers miles ``(0, F]``. Each station ends up
owning one contiguous stretch of miles, which becomes one fuel stop.

Correctness is cross-checked in tests against an exact LP solver.

Practical refinement: a cost per stop
-------------------------------------
Pure fuel-cost minimisation happily makes 15+ stops to buy 2 gallons a few
cents cheaper. Real drivers lose time at every stop, so the API also accepts
``stop_penalty`` (dollars per stop): a stop is only worth adding if it saves
more than that. That turns the problem into a fixed-charge problem, solved
exactly by ``plan_fuel_stops_dp``, a dynamic program over (station, fuel
level) with fuel discretised to 0.5 mile (0.05 gal) steps. Per station the
transition is three vectorised numpy passes (a prefix-minimum does the "how
much to buy" step), so a coast-to-coast trip with ~450 candidate stations
solves in a few milliseconds.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

import numpy as np

from ..exceptions import NoFeasibleFuelPlan

EPS = 1e-9


@dataclass(frozen=True, slots=True)
class PlannedStop:
    station: int  # index into the candidate arrays passed in
    mile_marker: float  # where along the route the stop is
    miles_of_fuel: float  # fuel bought, expressed in miles of driving
    fuel_on_arrival_miles: float
    fuel_on_departure_miles: float


@dataclass(frozen=True, slots=True)
class FuelPlan:
    stops: list[PlannedStop]
    total_cost: float
    total_miles_purchased: float
    fuel_at_destination_miles: float


def plan_fuel_stops(
    positions: np.ndarray,
    prices: np.ndarray,
    route_miles: float,
    range_miles: float,
    start_fuel_miles: float,
    mpg: float,
    tie_breaker: np.ndarray | None = None,
) -> FuelPlan:
    """Return the cheapest set of stops and how much to buy at each.

    ``positions`` (mile markers) and ``prices`` ($/gal) are parallel arrays of
    candidate stations already known to lie on the route. ``tie_breaker``
    (e.g. distance off-route) decides between equally priced stations.
    """
    if range_miles <= 0 or mpg <= 0:
        raise ValueError("range_miles and mpg must be positive")
    start_fuel = float(min(max(start_fuel_miles, 0.0), range_miles))
    total = float(route_miles)
    n = len(positions)
    if tie_breaker is None:
        tie_breaker = np.zeros(n)

    if start_fuel >= total - EPS:  # Destination reachable on the starting tank.
        return FuelPlan([], 0.0, 0.0, start_fuel - total)

    # Only stations that can contribute: before the destination and with a
    # window reaching past where the free fuel runs out.
    useful = np.flatnonzero((positions < total) & (positions + range_miles > start_fuel) & (positions >= 0))
    order = useful[np.lexsort((positions[useful], tie_breaker[useful], prices[useful]))]  # stable key order
    rank = np.empty(n, dtype=np.int64)
    rank[order] = np.arange(len(order))  # total order (price, tie_breaker, position)
    by_pos = useful[np.argsort(positions[useful], kind="stable")]

    ends = positions[by_pos] + range_miles
    breakpoints = np.concatenate(([start_fuel, total], positions[by_pos], ends))
    breakpoints = np.unique(breakpoints[(breakpoints >= start_fuel) & (breakpoints <= total)])

    heap: list[tuple[int, int]] = []  # (rank, station)
    owner_miles: dict[int, float] = {}
    owner_order: list[int] = []
    ptr = 0
    m = len(by_pos)

    for a, b in zip(breakpoints[:-1], breakpoints[1:]):
        # Stations at or before mile a become available for miles (a, b].
        while ptr < m and positions[by_pos[ptr]] <= a + EPS:
            s = by_pos[ptr]
            heapq.heappush(heap, (rank[s], s))
            ptr += 1
        # Drop stations whose fuel can't reach mile b (window has slid past them).
        while heap and positions[heap[0][1]] + range_miles < b - EPS:
            heapq.heappop(heap)
        if not heap:
            raise NoFeasibleFuelPlan(_gap_message(a, range_miles, total))
        s = heap[0][1]
        if s not in owner_miles:
            owner_miles[s] = 0.0
            owner_order.append(s)
        owner_miles[s] += float(b - a)

    # Build the stop list in driving order and simulate the tank for sanity + reporting.
    stations = sorted(owner_order, key=lambda i: positions[i])
    stops: list[PlannedStop] = []
    fuel, at = start_fuel, 0.0
    cost = bought = 0.0
    for s in stations:
        fuel -= positions[s] - at
        at = float(positions[s])
        buy = owner_miles[s]
        assert fuel >= -1e-6 and fuel + buy <= range_miles + 1e-6, "invariant violated"
        stops.append(PlannedStop(int(s), at, buy, max(fuel, 0.0), min(fuel + buy, range_miles)))
        fuel += buy
        cost += buy / mpg * float(prices[s])
        bought += buy
    fuel -= total - at
    return FuelPlan(stops, cost, bought, max(fuel, 0.0))


def plan_fuel_stops_dp(
    positions: np.ndarray,
    prices: np.ndarray,
    route_miles: float,
    range_miles: float,
    start_fuel_miles: float,
    mpg: float,
    stop_penalty: float,
    tie_breaker: np.ndarray | None = None,
    resolution_miles: float = 1.0,
) -> FuelPlan:
    """Exact minimum of (fuel cost + stop_penalty x number of stops).

    State: fuel level on arrival at each station, in ticks of
    ``resolution_miles``. At a station you either drive past (no cost) or
    stop and fill to any level h >= arrival level g, paying
    ``penalty + price * (h - g)``. For a fixed h the best g minimises
    ``V[g] - price * g`` over g <= h, which is a running (prefix) minimum,
    so each station costs O(levels) vectorised work.

    The DP picks WHICH stations to use; the exact purchase amounts for that
    set are then recomputed with the continuous algorithm above, so reported
    gallons and dollars carry no discretisation error.

    ``total_cost`` in the result is fuel money only (penalty excluded).
    """
    if range_miles <= 0 or mpg <= 0:
        raise ValueError("range_miles and mpg must be positive")
    q = float(resolution_miles)
    total = float(route_miles)
    cap = int(np.floor(range_miles / q + 1e-9))
    start = int(np.floor(min(max(start_fuel_miles, 0.0), range_miles) / q + 1e-9))
    if start_fuel_miles >= total - EPS:
        return FuelPlan([], 0.0, 0.0, start_fuel_miles - total)

    n = len(positions)
    if tie_breaker is None:
        tie_breaker = np.zeros(n)
    # Candidates in driving order; equal positions ordered by price then detour.
    keep = np.flatnonzero((positions >= 0) & (positions < total))
    order = keep[np.lexsort((tie_breaker[keep], prices[keep], np.rint(positions[keep] / q)))]
    # Several stations often share a mile marker (same town). At one point only
    # the cheapest can ever be worth using, so drop the rest: fewer DP layers.
    station_ticks = np.rint(positions[order] / q).astype(np.int64)
    first = np.ones(len(order), dtype=bool)
    first[1:] = station_ticks[1:] != station_ticks[:-1]
    order, station_ticks = order[first], station_ticks[first]
    ticks = np.concatenate((station_ticks, [int(np.rint(total / q))]))
    dest_tick = ticks[-1]

    levels = np.arange(cap + 1)
    INF = np.inf
    value = np.full(cap + 1, INF)
    value[start] = 0.0
    prev_tick = 0
    prev_mile = 0.0
    stopped: list[np.ndarray] = []  # per station: bool[level after decision]
    came_from: list[np.ndarray] = []  # per station: arrival level if stopped

    for k, s in enumerate(order):
        value = _drive(value, ticks[k] - prev_tick)
        if not np.isfinite(value).any():
            raise NoFeasibleFuelPlan(_gap_message(prev_mile, range_miles, total))
        prev_tick, prev_mile = ticks[k], float(positions[s])
        unit = float(prices[s]) * q / mpg  # $ per tick
        adjusted = value - unit * levels  # V[g] - p*g
        best = np.minimum.accumulate(adjusted)  # min over g <= h
        # Arrival level achieving that minimum (latest one on ties).
        argbest = np.maximum.accumulate(np.where(adjusted <= best, levels, 0))
        stop_cost = stop_penalty + unit * levels + best
        take = stop_cost < value - 1e-12
        stopped.append(take)
        came_from.append(argbest)
        value = np.where(take, stop_cost, value)

    value = _drive(value, dest_tick - prev_tick)
    if not np.isfinite(value).any():
        last = positions[order[-1]] if len(order) else 0.0
        raise NoFeasibleFuelPlan(_gap_message(last, range_miles, total))

    # Cheapest arrival; on ties prefer arriving with the least leftover fuel.
    level = int(np.flatnonzero(value <= value.min() + 1e-9)[0])

    # Walk back through the decisions.
    purchases: list[tuple[int, int, int]] = []  # (candidate idx, arrival level, departure level)
    for k in range(len(order) - 1, -1, -1):
        level += ticks[k + 1] - ticks[k]  # undo the drive to the next point
        if stopped[k][level]:
            arrival = int(came_from[k][level])
            purchases.append((int(order[k]), arrival, level))
            level = arrival
    purchases.reverse()

    chosen = np.asarray([s for s, _, _ in purchases], dtype=np.int64)
    exact = _exact_amounts(chosen, positions, prices, total, range_miles, start_fuel_miles, mpg, tie_breaker)
    if exact is None:  # rounding made a borderline set look feasible: fall back safely
        return plan_fuel_stops(positions, prices, total, range_miles, start_fuel_miles, mpg, tie_breaker)
    return exact


def _exact_amounts(chosen, positions, prices, total, range_miles, start_fuel_miles, mpg, tie_breaker):
    """Optimal continuous purchases restricted to the stations in ``chosen``."""
    try:
        sub = plan_fuel_stops(
            positions[chosen], prices[chosen], total, range_miles, start_fuel_miles, mpg, tie_breaker[chosen]
        )
    except NoFeasibleFuelPlan:
        return None
    stops = [
        PlannedStop(
            int(chosen[st.station]),
            st.mile_marker,
            st.miles_of_fuel,
            st.fuel_on_arrival_miles,
            st.fuel_on_departure_miles,
        )
        for st in sub.stops
    ]
    return FuelPlan(stops, sub.total_cost, sub.total_miles_purchased, sub.fuel_at_destination_miles)


def _drive(value: np.ndarray, ticks: int) -> np.ndarray:
    """Burn ``ticks`` of fuel: level h on arrival came from level h + ticks."""
    if ticks <= 0:
        return value
    out = np.full_like(value, np.inf)
    if ticks < len(value):
        out[: len(value) - ticks] = value[ticks:]
    return out


def _gap_message(after_mile: float, range_miles: float, total: float) -> str:
    return (
        f"No fuel station within {range_miles:.0f} miles to cover the stretch after mile "
        f"{after_mile:.0f} of {total:.0f}. The route cannot be completed with this vehicle range."
    )
