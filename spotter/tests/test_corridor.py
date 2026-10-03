import time

import numpy as np
from django.test import SimpleTestCase

from spotter.services.corridor import stations_along_route
from spotter.services.geo import haversine_mi
from spotter.services.ors import build_route
from spotter.services.stations import StationStore

MILES_PER_DEGREE_LAT = 69.1


def make_store(points):
    """points: list of (lat, lon). Prices are irrelevant here."""
    return StationStore([{"lat": la, "lon": lo, "price": 3.0} for la, lo in points], 0)


def straight_route():
    """West-to-east route along latitude 36, from lon -100 to -95 (about 280 miles)."""
    lons = np.linspace(-100.0, -95.0, 200)
    coords = [[lon, 36.0] for lon in lons]
    length_m = float(haversine_mi(36.0, -100.0, 36.0, -95.0)) * 1609.344
    return build_route(coords, length_m)


class StationsAlongRouteTests(SimpleTestCase):
    def test_includes_near_stations_and_excludes_far_ones(self):
        store = make_store([
            (36.02, -99.0),   # ~1.4 miles off the route: IN
            (36.20, -97.0),   # ~13.8 miles off the route: OUT at 5 miles
            (36.0, -94.0),    # ~56 miles past the end of the route: OUT
        ])
        corridor = stations_along_route(straight_route(), store, radius_miles=5.0)
        self.assertEqual(corridor.station_index.tolist(), [0])

    def test_wider_radius_picks_up_more(self):
        store = make_store([(36.02, -99.0), (36.20, -97.0)])
        corridor = stations_along_route(straight_route(), store, radius_miles=15.0)
        self.assertEqual(sorted(corridor.station_index.tolist()), [0, 1])

    def test_mile_marker_is_distance_along_route(self):
        route = straight_route()
        store = make_store([(36.0, -98.0)])
        corridor = stations_along_route(route, store)
        expected = float(haversine_mi(36.0, -100.0, 36.0, -98.0)) * route.total_miles / float(
            haversine_mi(36.0, -100.0, 36.0, -95.0))
        self.assertAlmostEqual(float(corridor.mile_marker[0]), expected, delta=1.0)

    def test_off_route_distance_is_accurate_to_about_half_a_mile(self):
        store = make_store([(36.0 + 3.0 / MILES_PER_DEGREE_LAT, -98.0)])   # 3 miles north
        corridor = stations_along_route(straight_route(), store)
        self.assertAlmostEqual(float(corridor.off_route_miles[0]), 3.0, delta=0.5)

    def test_results_are_sorted_by_mile_marker(self):
        store = make_store([(36.0, -96.0), (36.0, -99.0), (36.0, -97.5)])   # deliberately out of order
        corridor = stations_along_route(straight_route(), store)
        self.assertEqual(corridor.station_index.tolist(), [1, 2, 0])
        self.assertTrue(np.all(np.diff(corridor.mile_marker) >= 0))

    def test_repeated_locations_are_all_kept(self):
        store = make_store([(36.0, -98.0), (36.0, -98.0)])   # e.g. one station listed twice
        self.assertEqual(len(stations_along_route(straight_route(), store)), 2)

    def test_no_stations_nearby_gives_empty_result(self):
        corridor = stations_along_route(straight_route(), make_store([(45.0, -80.0)]))
        self.assertEqual(len(corridor), 0)

    def test_speed_on_a_cross_country_route_with_all_stations(self):
        rng = np.random.default_rng(0)
        store = make_store(list(zip(rng.uniform(25, 49, 8141), rng.uniform(-120, -70, 8141))))
        lons = np.linspace(-118.0, -74.0, 5000)
        coords = [[lon, 36.0] for lon in lons]
        route = build_route(coords, float(haversine_mi(36.0, -118.0, 36.0, -74.0)) * 1609.344 * 1.1)
        started = time.perf_counter()
        corridor = stations_along_route(route, store)
        elapsed = time.perf_counter() - started
        self.assertGreater(route.total_miles, 2500)
        self.assertLess(elapsed, 0.5)   # generous ceiling; real figure is far lower
        print(f"\n   [speed] {route.total_miles:.0f}-mile route, 8141 stations -> "
              f"{len(corridor)} in corridor, {elapsed * 1000:.0f} ms")