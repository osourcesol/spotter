from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from spotter.services.locations import Location, LocationNotFound, resolve_location
from spotter.services.ors import RoutingError

GEOCODER = "spotter.services.locations.geocode_us"


class ResolveLocationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_coordinates_are_parsed_locally_with_no_network_call(self):
        with mock.patch(GEOCODER) as geocoder:
            self.assertEqual(resolve_location(" 32.78 , -96.80 "),
                             Location(32.78, -96.80, "32.7800,-96.8000", "coordinates"))
            geocoder.assert_not_called()

    def test_coordinates_outside_the_usa_are_rejected(self):
        with mock.patch(GEOCODER) as geocoder:
            for text in ("48.85,2.35", "-33.9,151.2"):            # Paris, Sydney
                with self.assertRaises(LocationNotFound):
                    resolve_location(text)
            geocoder.assert_not_called()

    def test_a_name_is_geocoded_once_then_served_from_cache(self):
        with mock.patch(GEOCODER, return_value=(34.05, -118.24, "Los Angeles, CA, USA")) as geocoder:
            first = resolve_location("Los Angeles, CA")
            again = resolve_location("  los angeles,   ca ")      # different case and spacing
            self.assertEqual(first, Location(34.05, -118.24, "Los Angeles, CA, USA", "geocoder"))
            self.assertEqual(again, Location(34.05, -118.24, "Los Angeles, CA, USA", "cache"))
            self.assertEqual(geocoder.call_count, 1)

    def test_unknown_place_raises_location_not_found(self):
        with mock.patch(GEOCODER, return_value=None):
            with self.assertRaisesMessage(LocationNotFound, "Zzzz"):
                resolve_location("Zzzz, QQ")

    def test_empty_input_raises(self):
        for text in ("", "   ", None):
            with self.assertRaises(LocationNotFound):
                resolve_location(text)

    def test_provider_errors_pass_through_for_the_view_to_handle(self):
        with mock.patch(GEOCODER, side_effect=RoutingError("down", 502)):
            with self.assertRaises(RoutingError):
                resolve_location("Dallas, TX")