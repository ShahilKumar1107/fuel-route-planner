"""The optimiser is the core of the project, so it is checked against
independent exact solvers rather than hand-picked expected numbers."""

import itertools

import numpy as np
from django.test import SimpleTestCase
from scipy.optimize import linprog

from planner.exceptions import NoFeasibleFuelPlan
from planner.services.optimizer import plan_fuel_stops, plan_fuel_stops_dp


def lp_min_cost(pos, price, total, rng_miles, start, mpg=10.0):
    """Exact continuous optimum via linear programming (independent oracle).

    Variable q_i = miles of fuel bought at station i (sorted by position).
      tank after buying at k:  start + sum_{j<=k} q_j - pos_k <= range
      fuel on arrival at k:    start + sum_{j<k}  q_j - pos_k >= 0
      reach destination:       start + sum_j q_j - total >= 0
    """
    order = np.argsort(pos)
    pos, price = np.asarray(pos)[order], np.asarray(price)[order]
    n = len(pos)
    if n == 0:
        return 0.0 if start >= total else None
    a_ub, b_ub = [], []
    for k in range(n):
        a_ub.append([1.0 if j <= k else 0.0 for j in range(n)])
        b_ub.append(rng_miles - start + pos[k])
    for k in range(n):
        a_ub.append([-1.0 if j < k else 0.0 for j in range(n)])
        b_ub.append(start - pos[k])
    a_ub.append([-1.0] * n)
    b_ub.append(start - total)
    res = linprog(price / mpg, A_ub=a_ub, b_ub=b_ub, bounds=[(0, None)] * n, method="highs")
    return res.fun if res.status == 0 else None


class SlidingWindowOptimizerTests(SimpleTestCase):
    def test_hand_worked_example(self):
        # 900-mile trip, 500-mile tank, starts full.
        # Expensive station at 100, cheap at 400, mid at 700.
        plan = plan_fuel_stops(
            np.array([100.0, 400.0, 700.0]),
            np.array([4.0, 3.0, 3.5]),
            route_miles=900,
            range_miles=500,
            start_fuel_miles=500,
            mpg=10,
        )
        # Free fuel covers miles 0-500; miles 500-900 are all within 500 mi of
        # the $3.00 station at mile 400, so buy 400 miles (40 gal) there.
        self.assertEqual([s.station for s in plan.stops], [1])
        self.assertAlmostEqual(plan.stops[0].miles_of_fuel, 400.0)
        self.assertAlmostEqual(plan.total_cost, 40 * 3.0)
        self.assertAlmostEqual(plan.fuel_at_destination_miles, 0.0)

    def test_no_stop_needed_when_tank_covers_trip(self):
        plan = plan_fuel_stops(np.array([50.0]), np.array([3.0]), 300, 500, 500, 10)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, 0.0)
        self.assertAlmostEqual(plan.fuel_at_destination_miles, 200.0)

    def test_gap_longer_than_range_is_infeasible(self):
        with self.assertRaises(NoFeasibleFuelPlan):
            plan_fuel_stops(np.array([100.0, 700.0]), np.array([3.0, 3.0]), 1200, 500, 100, 10)

    def test_matches_linear_programming_optimum(self):
        rng = np.random.default_rng(42)
        checked = 0
        for _ in range(600):
            n = int(rng.integers(1, 30))
            total = float(rng.uniform(100, 3000))
            tank = float(rng.choice([300.0, 500.0]))
            start = float(rng.uniform(0, tank))
            pos = rng.uniform(0, total, n)
            price = np.round(rng.uniform(2.5, 5.0, n), 3)
            expected = lp_min_cost(pos, price, total, tank, start)
            try:
                got = plan_fuel_stops(pos, price, total, tank, start, 10).total_cost
            except NoFeasibleFuelPlan:
                got = None
            if expected is None:
                self.assertIsNone(got)
                continue
            checked += 1
            self.assertAlmostEqual(got, expected, places=6)
        self.assertGreater(checked, 150)

    def test_tank_limits_are_respected(self):
        rng = np.random.default_rng(3)
        pos = np.sort(rng.uniform(0, 2800, 400))
        price = rng.uniform(2.7, 4.5, 400)
        plan = plan_fuel_stops(pos, price, 2800, 500, 500, 10)
        for stop in plan.stops:
            self.assertGreaterEqual(stop.fuel_on_arrival_miles, -1e-6)
            self.assertLessEqual(stop.fuel_on_departure_miles, 500 + 1e-6)


class StopPenaltyDPTests(SimpleTestCase):
    def brute_force(self, pos, price, total, tank, start, penalty):
        """Try every subset of stations; LP gives the best fuel cost for each."""
        best = None
        for k in range(len(pos) + 1):
            for subset in itertools.combinations(range(len(pos)), k):
                idx = list(subset)
                cost = lp_min_cost(pos[idx], price[idx], total, tank, start)
                if cost is not None:
                    value = cost + penalty * k
                    best = value if best is None or value < best else best
        return best

    def test_matches_brute_force(self):
        rng = np.random.default_rng(11)
        checked = 0
        for _ in range(150):
            n = int(rng.integers(1, 8))
            total = float(rng.uniform(200, 1500))
            tank = 500.0
            start = float(rng.uniform(0, tank))
            pos = np.round(rng.uniform(0, total, n), 1)
            price = np.round(rng.uniform(2.5, 5.0, n), 2)
            penalty = float(rng.choice([2.0, 5.0, 20.0]))
            expected = self.brute_force(pos, price, total, tank, start, penalty)
            try:
                plan = plan_fuel_stops_dp(pos, price, total, tank, start, 10, penalty)
                got = plan.total_cost + penalty * len(plan.stops)
            except NoFeasibleFuelPlan:
                got = None
            if expected is None:
                self.assertIsNone(got)
                continue
            checked += 1
            # Fuel is discretised to 1 mile (0.1 gal) inside the DP, so allow a
            # few cents; reported amounts themselves are recomputed exactly.
            self.assertLessEqual(got, expected + 0.75)
            self.assertGreaterEqual(got, expected - 1e-6)
        self.assertGreater(checked, 50)

    def test_penalty_reduces_stop_count(self):
        rng = np.random.default_rng(5)
        pos = np.sort(rng.uniform(0, 2800, 450))
        price = rng.uniform(2.7, 4.5, 450)
        cheapest = plan_fuel_stops(pos, price, 2800, 500, 500, 10)
        practical = plan_fuel_stops_dp(pos, price, 2800, 500, 500, 10, stop_penalty=10)
        self.assertLess(len(practical.stops), len(cheapest.stops))
        self.assertGreaterEqual(practical.total_cost, cheapest.total_cost - 1e-6)
        # Every mile of the trip must still be paid for.
        self.assertAlmostEqual(practical.total_miles_purchased, cheapest.total_miles_purchased, places=4)

    def test_infeasible(self):
        with self.assertRaises(NoFeasibleFuelPlan):
            plan_fuel_stops_dp(np.array([100.0, 700.0]), np.array([3.0, 3.0]), 1200, 500, 100, 10, 10)
