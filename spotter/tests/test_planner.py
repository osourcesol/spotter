import random
from decimal import Decimal

from django.test import SimpleTestCase

from spotter.services.locations import Location
from spotter.services.optimizer import NoReachableStation
from spotter.services.planner import SameLocation, build_plan
from spotter.tests.helpers import FakeWorld, make_store, places, straight_route


def cross_country_world(**kwargs):
    rng = random.Random(1)
    route = straight_route(-120.0, -75.0)
    stations = [(lon, rng.uniform(2.8, 4.2)) for lon in [-119.5 + i for i in range(45)]]
    return FakeWorld(route, make_store(stations), places(**kwargs)), route


class PlanContentTests(SimpleTestCase):
    def test_cross_country_plan_is_internally_consistent(self):
        world, route = cross_country_world()
        with world:
            plan = build_plan("a", "b")

        self.assertGreater(plan["distance_miles"], 2000)
        # Gallons bought equal distance / 10: the trip starts empty and ends empty.
        self.assertAlmostEqual(plan["fuel_gallons_total"], plan["distance_miles"] / 10, delta=0.01)
        # The total is exactly the sum of the line items, to the cent.
        items = [Decimal(str(s["cost_usd"])) for s in plan["fuel_stops"]] + [Decimal(str(plan["start_fill"]["cost_usd"]))]
        self.assertEqual(Decimal(str(plan["total_fuel_cost_usd"])), sum(items))
        # The cost lies between "all cheapest" and "all priciest" fuel.
        gallons = plan["distance_miles"] / 10
        self.assertLessEqual(2.8 * gallons - 0.01, plan["total_fuel_cost_usd"])
        self.assertLessEqual(plan["total_fuel_cost_usd"], 4.2 * gallons + 0.01)
        # No leg between purchases is longer than the 500-mile range.
        marks = [0.0] + [s["mile_marker"] for s in plan["fuel_stops"]] + [plan["distance_miles"]]
        self.assertTrue(all(b - a <= 500.5 for a, b in zip(marks, marks[1:])))
        self.assertGreaterEqual(len(plan["fuel_stops"]), 4)
        self.assertEqual([s["order"] for s in plan["fuel_stops"]], list(range(1, len(plan["fuel_stops"]) + 1)))

    def test_geojson_has_route_both_ends_and_every_stop(self):
        world, _ = cross_country_world()
        with world:
            plan = build_plan("a", "b")
        features = plan["route_geojson"]["features"]
        kinds = [f["properties"]["kind"] for f in features]
        self.assertEqual(kinds.count("route"), 1)
        self.assertEqual(kinds.count("start"), 1)
        self.assertEqual(kinds.count("finish"), 1)
        self.assertEqual(kinds.count("fuel_stop"), len(plan["fuel_stops"]))
        line = next(f for f in features if f["properties"]["kind"] == "route")["geometry"]
        self.assertEqual(line["type"], "LineString")
        self.assertGreater(len(line["coordinates"]), 1000)

    def test_a_short_trip_has_no_stops_only_the_first_fill_up(self):
        route = straight_route(-100.0, -97.0)                       # about 170 miles
        world = FakeWorld(route, make_store([(-99.0, 3.0), (-98.0, 3.2)]),
                          places(lon_from=-100.0, lon_to=-97.0))
        with world:
            plan = build_plan("a", "b")
        self.assertEqual(plan["fuel_stops"], [])
        expected = round(plan["distance_miles"] / 10 * plan["start_fill"]["price_per_gallon"], 2)
        self.assertAlmostEqual(plan["total_fuel_cost_usd"], expected, delta=0.02)


class FirstFillUpTests(SimpleTestCase):
    def test_priced_at_the_cheapest_station_within_ten_miles_of_the_start(self):
        route = straight_route(-100.0, -97.0)
        # three stations within 10 miles of the start (0.05 deg of longitude is about 2.8 miles)
        stations = [(-99.95, 3.40), (-99.90, 3.05), (-99.85, 3.30), (-98.0, 3.9)]
        with FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-97.0)):
            plan = build_plan("a", "b")
        self.assertAlmostEqual(plan["start_fill"]["price_per_gallon"], 3.05)
        self.assertIn("within 10 miles", plan["start_fill"]["note"])

    def test_falls_back_to_the_nearest_station_when_none_is_close(self):
        route = straight_route(-100.0, -97.0)
        stations = [(-99.0, 3.70), (-98.0, 3.10)]                   # first one is about 56 miles away
        with FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-97.0)):
            plan = build_plan("a", "b")
        self.assertAlmostEqual(plan["start_fill"]["price_per_gallon"], 3.70)
    
        def test_says_plainly_when_the_priced_station_is_far_from_the_start(self):
            route = straight_route(-100.0, -97.0)
            stations = [(-99.0, 3.70), (-98.0, 3.10)]                   # nearest station is about 56 miles ahead
            with FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-97.0)):
                fill = build_plan("a", "b")["start_fill"]
            self.assertIn("no station within 10 miles", fill["note"])
            self.assertGreater(fill["priced_at"]["mile_marker"], 40)    # shows where that station really is
            self.assertEqual(fill["priced_at"]["name"], "STATION 0")


class CorridorWideningTests(SimpleTestCase):
    def test_widens_the_search_only_when_the_range_requires_it(self):
        route = straight_route(-110.0, -95.0)                       # about 850 miles
        # stations are 0.17 degrees (about 11.7 miles) off the route: invisible at 5 miles
        stations = [(lon, 3.3, 0.17) for lon in [-109.5 + 0.5 * i for i in range(30)]]
        with FakeWorld(route, make_store(stations), places(lon_from=-110.0, lon_to=-95.0)):
            plan = build_plan("a", "b")
        self.assertEqual(plan["meta"]["corridor_radius_miles"], 15.0)

    def test_uses_the_narrow_corridor_when_it_is_enough(self):
        world, _ = cross_country_world()
        with world:
            self.assertEqual(build_plan("a", "b")["meta"]["corridor_radius_miles"], 5.0)

    def test_no_stations_near_the_route_raises(self):
        route = straight_route(-100.0, -97.0)
        with FakeWorld(route, make_store([(-99.0, 3.0, 5.0)]), places(lon_from=-100.0, lon_to=-97.0)):  # 345 miles north
            with self.assertRaises(NoReachableStation):
                build_plan("a", "b")

    def test_a_gap_longer_than_the_range_raises_with_the_mile_marker(self):
        route = straight_route(-120.0, -75.0)                       # about 2,500 miles
        with FakeWorld(route, make_store([(-119.5, 3.0), (-119.0, 3.1)]), places()):
            with self.assertRaises(NoReachableStation) as caught:
                build_plan("a", "b")
        self.assertGreater(caught.exception.mile_marker, 0)


class ExternalCallCountTests(SimpleTestCase):
    def count(self, start_source, finish_source):
        world, _ = cross_country_world(start_source=start_source, finish_source=finish_source)
        with world:
            return build_plan("a", "b")["meta"]["external_api_calls"]

    def test_counts_one_route_request_plus_one_per_place_that_needed_geocoding(self):
        self.assertEqual(self.count("geocoder", "geocoder"), 3)
        self.assertEqual(self.count("geocoder", "cache"), 2)
        self.assertEqual(self.count("cache", "cache"), 1)
        self.assertEqual(self.count("coordinates", "coordinates"), 1)

    def test_makes_exactly_one_route_request(self):
        world, _ = cross_country_world()
        with world as w:
            build_plan("a", "b")
        self.assertEqual(w.fetch_route.call_count, 1)


class InputTests(SimpleTestCase):
    def test_same_place_is_rejected_before_any_route_request(self):
        same = lambda text: Location(36.0, -100.0, "Here", "cache")
        route = straight_route(-100.0, -97.0)
        with FakeWorld(route, make_store([(-99.0, 3.0)]), same) as world:
            with self.assertRaises(SameLocation):
                build_plan("a", "b")
        world.fetch_route.assert_not_called()