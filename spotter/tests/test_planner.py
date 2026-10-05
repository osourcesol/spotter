from decimal import ROUND_HALF_UP, Decimal
import random
from decimal import Decimal

from django.core.cache import cache
from django.test import SimpleTestCase

from spotter.services.locations import Location
from spotter.services.optimizer import NoReachableStation
from spotter.services.ors import RoutingError
from spotter.services.planner import OPTION_SPECS, PlanOptions, SameLocation, build_plan, get_plan
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

    def test_every_line_multiplies_out_to_its_cost(self):
        world, _ = cross_country_world()
        with world:
            plan = build_plan("a", "b")
        for row in plan["fuel_stops"] + [plan["start_fill"]]:
            shown = Decimal(str(row["gallons"])) * Decimal(str(row["price_per_gallon"]))
            self.assertEqual(shown.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal(str(row["cost_usd"])))
    
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


class WorthwhileStopsTests(SimpleTestCase):
    def test_a_short_trip_does_not_stop_to_save_a_fraction_of_a_cent(self):
        route = straight_route(-100.0, -97.0)
        stations = [(-99.95, 3.000), (-99.0, 2.999), (-98.0, 2.97)]
        with FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-97.0)):
            plan = build_plan("a", "b")
        self.assertEqual(plan["fuel_stops"], [])
        self.assertEqual(plan["assumptions"]["min_saving_per_gallon"], 0.05)

    def test_a_much_cheaper_station_still_gets_a_stop(self):
        route = straight_route(-100.0, -97.0)
        stations = [(-99.95, 3.40), (-99.0, 2.90)]
        with FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-97.0)):
            plan = build_plan("a", "b")
        self.assertEqual(len(plan["fuel_stops"]), 1)


class DetourAndSplashTests(SimpleTestCase):
    def test_a_station_far_off_the_route_loses_to_a_slightly_pricier_one_on_it(self):
        route = straight_route(-110.0, -95.0)
        stations = [
            (-109.95, 3.30, 0.0),
            (-103.0, 3.00, 0.07),
            (-103.0, 3.02, 0.0),
            (-96.0, 3.50, 0.0),
        ]
        with FakeWorld(route, make_store(stations), places(lon_from=-110.0, lon_to=-95.0)):
            plan = build_plan("a", "b")
        self.assertEqual(plan["meta"]["candidate_stations"], 4)
        names = [stop["name"] for stop in plan["fuel_stops"]]
        self.assertIn("STATION 2", names)
        self.assertNotIn("STATION 1", names)
        self.assertTrue(all(stop["off_route_miles"] < 2 for stop in plan["fuel_stops"]))

    def test_money_uses_the_real_price_not_the_detour_adjusted_one(self):
        route = straight_route(-110.0, -95.0)
        stations = [(-109.95, 3.30, 0.0), (-103.0, 3.06, 0.0), (-96.0, 3.50, 0.0)]
        with FakeWorld(route, make_store(stations), places(lon_from=-110.0, lon_to=-95.0)):
            plan = build_plan("a", "b")
        for stop in plan["fuel_stops"]:
            self.assertIn(stop["price_per_gallon"], (3.30, 3.06, 3.50))
            self.assertAlmostEqual(stop["cost_usd"], stop["gallons"] * stop["price_per_gallon"], delta=0.01)

    def test_prices_that_fall_a_little_at_every_station_do_not_cause_a_stop_at_each(self):
        route = straight_route(-100.0, -95.4)
        miles_and_prices = [(0.0, 3.499), (15.0, 3.087), (23.0, 2.966), (31.0, 2.899), (35.0, 2.882), (38.0, 2.756)]
        stations = [(-100.0 + miles / 56.0, price) for miles, price in miles_and_prices]
        with FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-95.4)):
            plan = build_plan("a", "b")
        self.assertLessEqual(len(plan["fuel_stops"]), 1)
        gallons_each = [plan["start_fill"]["gallons"]] + [s["gallons"] for s in plan["fuel_stops"]]
        self.assertTrue(all(g >= 5.0 or g == gallons_each[-1] for g in gallons_each))

    def test_every_station_says_how_far_off_the_route_it_is_and_how_exact_its_location_is(self):
        world, _ = cross_country_world()
        with world:
            plan = build_plan("a", "b")
        stop = plan["fuel_stops"][0]
        self.assertIn("off_route_miles", stop)
        self.assertEqual(stop["location_precision"], "station")
        feature = next(f for f in plan["route_geojson"]["features"] if f["properties"]["kind"] == "fuel_stop")
        self.assertIn("off_route_miles", feature["properties"])
        self.assertIn("location_precision", feature["properties"])
        self.assertIn("min_purchase_gallons", plan["assumptions"])
        self.assertIn("detour_penalty_per_mile", plan["assumptions"])


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

class PlanCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_a_repeated_trip_is_served_from_memory(self):
        world, _ = cross_country_world()
        with world as w:
            first = get_plan("a", "b")
            again = get_plan("  A ", "B")                           # same trip, different case and spacing
        self.assertEqual(w.fetch_route.call_count, 1)               # the route was fetched only once
        self.assertEqual(first["meta"]["cache_hit"], False)
        self.assertEqual(again["meta"]["cache_hit"], True)
        self.assertEqual(again["meta"]["external_api_calls"], 0)
        self.assertEqual(again["total_fuel_cost_usd"], first["total_fuel_cost_usd"])

    def test_a_different_trip_is_not_confused_with_a_cached_one(self):
        world, _ = cross_country_world()
        with world as w:
            get_plan("a", "b")
            get_plan("b", "a")                                      # the reverse trip is a different request
        self.assertEqual(w.fetch_route.call_count, 2)

    def test_failures_are_not_cached(self):
        world, route = cross_country_world()
        with world as w:
            w.fetch_route.side_effect = [RoutingError("provider down", 502), route]
            with self.assertRaises(RoutingError):
                get_plan("a", "b")
            plan = get_plan("a", "b")                               # the retry really tries again
        self.assertEqual(w.fetch_route.call_count, 2)
        self.assertFalse(plan["meta"]["cache_hit"])


STAIRCASE = [(0.0, 3.499), (15.0, 3.087), (23.0, 2.966), (31.0, 2.899), (35.0, 2.882), (38.0, 2.756)]


def staircase_world(**kwargs):
    route = straight_route(-100.0, -95.4)
    stations = [(-100.0 + miles / 56.0, price) for miles, price in STAIRCASE]
    return FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-95.4, **kwargs))


class PlanOptionsTests(SimpleTestCase):
    def test_the_defaults_are_what_the_spec_list_publishes(self):
        defaults = PlanOptions()
        for spec in OPTION_SPECS:
            self.assertEqual(spec["default"], getattr(defaults, spec["key"]))
            self.assertLessEqual(spec["min"], spec["default"])
            self.assertLessEqual(spec["default"], spec["max"])
        self.assertEqual(len(OPTION_SPECS), 6)

    def test_the_search_radius_widens_from_the_chosen_start(self):
        self.assertEqual(PlanOptions().corridor_radii(), (5.0, 15.0, 30.0))
        self.assertEqual(PlanOptions(corridor_radius_miles=20).corridor_radii(), (20, 30.0))
        self.assertEqual(PlanOptions(corridor_radius_miles=40).corridor_radii(), (40,))

    def test_the_rules_used_are_reported_back(self):
        with staircase_world():
            plan = build_plan("a", "b", PlanOptions(mpg=20.0, min_purchase_gallons=2.0))
        self.assertEqual(plan["assumptions"]["mpg"], 20.0)
        self.assertEqual(plan["assumptions"]["min_purchase_gallons"], 2.0)
        self.assertEqual(plan["assumptions"]["max_range_miles"], 500.0)

    def test_turning_the_minimum_purchase_off_brings_the_small_stops_back(self):
        with staircase_world():
            practical = build_plan("a", "b")
            exact = build_plan("a", "b", PlanOptions(min_purchase_gallons=0.0, min_saving_per_gallon=0.0))
        self.assertLessEqual(len(practical["fuel_stops"]), 1)
        self.assertGreaterEqual(len(exact["fuel_stops"]), 4)
        self.assertLess(exact["total_fuel_cost_usd"], practical["total_fuel_cost_usd"])

    def test_turning_the_detour_penalty_off_picks_the_cheaper_station_even_off_the_route(self):
        route = straight_route(-110.0, -95.0)
        stations = [(-109.95, 3.30, 0.0), (-103.0, 3.00, 0.07), (-103.0, 3.02, 0.0), (-96.0, 3.50, 0.0)]
        with FakeWorld(route, make_store(stations), places(lon_from=-110.0, lon_to=-95.0)):
            ignore = build_plan("a", "b", PlanOptions(detour_penalty_per_mile=0.0, min_saving_per_gallon=0.0))
            default = build_plan("a", "b")
        self.assertEqual(ignore["meta"]["candidate_stations"], 4)
        self.assertIn("STATION 1", [s["name"] for s in ignore["fuel_stops"]])
        self.assertNotIn("STATION 1", [s["name"] for s in default["fuel_stops"]])
        far = next(s for s in ignore["fuel_stops"] if s["name"] == "STATION 1")
        self.assertGreater(far["off_route_miles"], 4.0)

    def test_a_different_fuel_economy_changes_the_gallons(self):
        with staircase_world():
            plan = build_plan("a", "b", PlanOptions(mpg=20.0, max_range_miles=1000.0))
        self.assertAlmostEqual(plan["fuel_gallons_total"], plan["distance_miles"] / 20.0, delta=0.01)

    def test_a_shorter_range_can_make_a_trip_impossible(self):
        route = straight_route(-100.0, -97.0)
        stations = [(-99.95, 3.0), (-97.05, 3.0)]
        with FakeWorld(route, make_store(stations), places(lon_from=-100.0, lon_to=-97.0)):
            self.assertTrue(build_plan("a", "b", PlanOptions(max_range_miles=500.0)))
            with self.assertRaises(NoReachableStation):
                build_plan("a", "b", PlanOptions(max_range_miles=100.0))


class RulesAndCachingTests(SimpleTestCase):
    def test_changing_only_the_rules_reuses_the_route_and_costs_no_routing_request(self):
        with staircase_world(start_source="cache", finish_source="cache") as world:
            first = get_plan("a", "b")
            second = get_plan("a", "b", PlanOptions(min_purchase_gallons=0.0))
        self.assertEqual(world.fetch_route.call_count, 1)
        self.assertEqual(first["meta"]["external_api_calls"], 1)
        self.assertEqual(second["meta"]["external_api_calls"], 0)
        self.assertTrue(second["meta"]["route_from_cache"])
        self.assertFalse(second["meta"]["cache_hit"])

    def test_the_plan_cache_tells_different_rules_apart(self):
        with staircase_world() as world:
            default = get_plan("a", "b")
            same_again = get_plan("a", "b")
            different = get_plan("a", "b", PlanOptions(min_purchase_gallons=0.0))
        self.assertTrue(same_again["meta"]["cache_hit"])
        self.assertFalse(different["meta"]["cache_hit"])
        self.assertNotEqual(len(default["fuel_stops"]), len(different["fuel_stops"]))
        self.assertEqual(world.fetch_route.call_count, 1)