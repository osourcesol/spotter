"""Shared builders for the planner and API tests: a fake straight-line route and fake stations."""
from contextlib import ExitStack
from unittest import mock

import numpy as np
from django.core.cache import cache

from spotter.services.geo import haversine_mi
from spotter.services.locations import Location
from spotter.services.ors import build_route
from spotter.services.stations import StationStore


def straight_route(lon_from, lon_to, lat=36.0, points=400):
    """A route along one latitude, with the road exactly as long as the straight line."""
    lons = np.linspace(lon_from, lon_to, points)
    length_miles = float(haversine_mi(lat, lon_from, lat, lon_to))
    return build_route([[lon, lat] for lon in lons], length_miles * 1609.344)


def make_store(stations):
    """stations: list of (lon, price) or (lon, price, lat_offset_degrees), on a route at latitude 36."""
    rows = []
    for i, item in enumerate(stations):
        lon, price = item[0], item[1]
        offset = item[2] if len(item) > 2 else 0.02
        rows.append({"id": str(i), "name": f"STATION {i}", "address": "I-40", "city": "Town", "state": "TX",
                     "price": price, "lat": 36.0 + offset, "lon": lon, "geocode_source": "name_city"})
    return StationStore(rows, 0)


def places(start_source="geocoder", finish_source="geocoder", lon_from=-120.0, lon_to=-75.0):
    """What the (faked) location resolver returns for the texts 'a' and 'b'."""
    table = {"a": Location(36.0, lon_from, "Start Town, TX, USA", start_source),
             "b": Location(36.0, lon_to, "Finish Town, TX, USA", finish_source)}
    return lambda text: table[text]


class FakeWorld(ExitStack):
    """Context manager that replaces the three outside dependencies of the planner."""

    def __init__(self, route, store, resolver):
        super().__init__()
        self.route, self.store, self.resolver = route, store, resolver

    def __enter__(self):
        super().__enter__()
        cache.clear()
        self.fetch_route = self.enter_context(
            mock.patch("spotter.services.planner.fetch_route", return_value=self.route))
        self.enter_context(mock.patch("spotter.services.planner.get_store", return_value=self.store))
        self.enter_context(mock.patch("spotter.services.planner.resolve_location", side_effect=self.resolver))
        return self