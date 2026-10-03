import random
import time

import numpy as np
from django.test import SimpleTestCase
from scipy.optimize import linprog

from spotter.services.optimizer import NoReachableStation, plan_fuel

RANGE, MPG = 500.0, 10.0
TANK = RANGE / MPG


def total_cost(purchases):
    return sum(p.cost for p in purchases)


def optimal_cost_by_linear_programming(miles, prices):
    """Reference answer: the exact cheapest plan, found by an LP solver. None if impossible.
    x[i] = gallons bought at node i. Constraints keep the tank between 0 and its capacity at
    every node, and the total bought equals the fuel the trip needs."""
    n, m = len(miles), len(miles) - 1
    rows, limits = [], []
    for k in range(n):
        need = miles[k] / MPG
        bought_before = np.zeros(m)
        bought_before[: min(k, m)] = 1
        rows.append(-bought_before); limits.append(-need)          # never arrive with < 0 fuel
        if k < m:
            bought_through = np.zeros(m)
            bought_through[: k + 1] = 1
            rows.append(bought_through); limits.append(TANK + need)  # never exceed tank capacity
    result = linprog(np.array(prices[:m]), A_ub=np.array(rows), b_ub=np.array(limits),
                     A_eq=[np.ones(m)], b_eq=[miles[-1] / MPG], bounds=(0, None), method="highs")
    return result.fun if result.success else None


class PlanFuelBehaviourTests(SimpleTestCase):
    def test_short_trip_buys_exactly_the_fuel_needed_at_the_start(self):
        plan = plan_fuel([0.0, 200.0], [3.5, 0.0])
        self.assertEqual(len(plan), 1)
        self.assertAlmostEqual(plan[0].gallons, 20.0)
        self.assertAlmostEqual(total_cost(plan), 70.0)

    def test_buys_only_enough_to_reach_a_cheaper_station(self):
        # Expensive start, cheap station at mile 100: buy just 10 gallons now, the rest there.
        plan = plan_fuel([0.0, 100.0, 300.0], [3.5, 3.0, 0.0])
        self.assertEqual([(p.node, round(p.gallons, 6)) for p in plan], [(0, 10.0), (1, 20.0)])
        self.assertAlmostEqual(total_cost(plan), 10 * 3.5 + 20 * 3.0)

    def test_fills_the_tank_when_nothing_ahead_is_cheaper(self):
        # Cheap start, pricier stations ahead: fill up at the start (50 gal), top up where needed.
        plan = plan_fuel([0.0, 400.0, 800.0], [3.0, 3.4, 0.0])
        self.assertAlmostEqual(plan[0].gallons, TANK)
        self.assertAlmostEqual(sum(p.gallons for p in plan), 80.0)

    def test_gap_longer_than_range_raises_with_the_mile_marker(self):
        with self.assertRaises(NoReachableStation) as caught:
            plan_fuel([0.0, 600.0, 1200.0], [3.0, 3.0, 0.0])
        self.assertEqual(caught.exception.mile_marker, 0.0)

    def test_destination_exactly_one_range_away_is_reachable(self):
        self.assertAlmostEqual(sum(p.gallons for p in plan_fuel([0.0, 500.0], [3.0, 0.0])), 50.0)

    def test_bad_input_is_rejected(self):
        with self.assertRaises(ValueError):
            plan_fuel([0.0, 100.0], [3.0])                    # lengths differ
        with self.assertRaises(ValueError):
            plan_fuel([0.0, 300.0, 100.0], [3.0, 3.0, 0.0])   # not sorted


class SameLocationTests(SimpleTestCase):
    """The same location can be listed twice with different prices. The cheaper one must win."""

    def test_cheaper_twin_wins_in_either_order(self):
        for prices in ([3.5, 3.0, 3.2, 0.0], [3.5, 3.2, 3.0, 0.0]):
            plan = plan_fuel([0.0, 100.0, 100.0, 300.0], prices)
            self.assertAlmostEqual(total_cost(plan), 10 * 3.5 + 20 * 3.0, msg=str(prices))
            self.assertNotIn(3.2, [p.price for p in plan])      # never buys from the pricier twin

    def test_cheaper_twin_wins_at_the_start_location_too(self):
        # Two stations at mile 0 (like several Dallas stations at the city centre).
        plan = plan_fuel([0.0, 0.0, 0.0, 200.0], [3.4, 3.0, 3.2, 0.0])
        self.assertAlmostEqual(total_cost(plan), 20 * 3.0)


class PlanFuelAgainstExactSolverTests(SimpleTestCase):
    def check_invariants(self, plan, miles, prices):
        total = miles[-1]
        self.assertAlmostEqual(sum(p.gallons for p in plan), total / MPG, places=6)  # arrive on empty
        fuel, position = 0.0, 0.0
        for p in plan:                                      # replay the plan
            fuel -= (p.mile_marker - position) / MPG
            self.assertGreaterEqual(fuel, -1e-6)            # never run dry
            fuel += p.gallons
            self.assertLessEqual(fuel, TANK + 1e-6)         # never overfill
            position = p.mile_marker
        self.assertGreaterEqual(fuel - (total - position) / MPG, -1e-6)

    def test_matches_exact_solution_on_random_trips(self):
        rng = random.Random(7)
        feasible = 0
        for _ in range(500):
            n, distance = rng.randint(3, 40), rng.uniform(50, 4500)
            miles = [0.0] + sorted(rng.uniform(0, distance) for _ in range(n - 2)) + [distance]
            prices = [rng.uniform(2.7, 4.5) for _ in range(n - 1)] + [0.0]
            expected = optimal_cost_by_linear_programming(miles, prices)
            try:
                plan = plan_fuel(miles, prices)
            except NoReachableStation:
                self.assertIsNone(expected)       # we may only give up when the exact solver also can't
                continue
            feasible += 1
            self.assertAlmostEqual(total_cost(plan), expected, places=6)
            self.check_invariants(plan, miles, prices)
        self.assertGreater(feasible, 100)         # the test really exercised the algorithm

    def test_matches_exact_solution_when_many_stations_share_a_location(self):
        rng = random.Random(11)
        for _ in range(300):
            n, distance = rng.randint(4, 40), rng.uniform(100, 3000)
            # Snap to 25-mile spots so several stations land on the same mile marker.
            miles = [0.0] + sorted(round(rng.uniform(0, distance) / 25) * 25.0 for _ in range(n - 2)) + [distance]
            miles = [min(m, distance) for m in miles]
            prices = [rng.uniform(2.7, 4.5) for _ in range(n - 1)] + [0.0]
            expected = optimal_cost_by_linear_programming(miles, prices)
            try:
                plan = plan_fuel(miles, prices)
            except NoReachableStation:
                self.assertIsNone(expected)
                continue
            self.assertAlmostEqual(total_cost(plan), expected, places=6)

    def test_speed_with_a_cross_country_number_of_stations(self):
        rng = random.Random(3)
        n, distance = 1500, 3000.0
        miles = [0.0] + sorted(rng.uniform(0, distance) for _ in range(n - 2)) + [distance]
        prices = [rng.uniform(2.7, 4.5) for _ in range(n - 1)] + [0.0]
        started = time.perf_counter()
        plan = plan_fuel(miles, prices)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5)
        print(f"\n   [speed] {n} stations over {distance:.0f} miles -> {len(plan)} purchases, {elapsed * 1000:.1f} ms")