import json
from unittest import mock

from django.test import SimpleTestCase

from spotter.services.locations import LocationNotFound
from spotter.services.optimizer import NoReachableStation
from spotter.services.ors import RoutingError
from spotter.services.planner import SameLocation
from spotter.tests.helpers import FakeWorld, make_store, places, straight_route

URL = "/api/route/"
BUILD_PLAN = "spotter.views.build_plan"


def post(client, payload):
    return client.post(URL, data=json.dumps(payload), content_type="application/json")


class RouteEndpointTests(SimpleTestCase):
    def test_success_returns_the_plan_as_json(self):
        with mock.patch(BUILD_PLAN, return_value={"total_fuel_cost_usd": 12.5}) as build:
            response = post(self.client, {"start": "Dallas, TX", "finish": "Austin, TX"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"total_fuel_cost_usd": 12.5})
        build.assert_called_once_with("Dallas, TX", "Austin, TX")

    def test_missing_or_blank_fields_give_400_with_details(self):
        for payload in ({}, {"start": "Dallas, TX"}, {"start": "", "finish": "Austin, TX"}, {"start": "  ", "finish": "x"}):
            response = post(self.client, payload)
            self.assertEqual(response.status_code, 400, payload)
            self.assertEqual(response.json()["error"], "Invalid request")
            self.assertIn("details", response.json())

    def test_unknown_place_and_same_place_give_400_with_the_message(self):
        for error in (LocationNotFound("Could not find 'Zzzz'."), SameLocation("Start and finish are the same place.")):
            with mock.patch(BUILD_PLAN, side_effect=error):
                response = post(self.client, {"start": "a", "finish": "b"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"error": str(error)})

    def test_unreachable_stretch_gives_422_with_the_mile_marker(self):
        with mock.patch(BUILD_PLAN, side_effect=NoReachableStation(1234.4)):
            response = post(self.client, {"start": "a", "finish": "b"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["mile_marker"], 1234)

    def test_routing_provider_problems_keep_their_own_status(self):
        for status in (422, 502, 503):
            with mock.patch(BUILD_PLAN, side_effect=RoutingError("provider said no", status)):
                response = post(self.client, {"start": "a", "finish": "b"})
            self.assertEqual((response.status_code, response.json()), (status, {"error": "provider said no"}))

    def test_form_encoded_requests_work_too(self):
        with mock.patch(BUILD_PLAN, return_value={"ok": True}):
            response = self.client.post(URL, {"start": "a", "finish": "b"})
        self.assertEqual(response.status_code, 200)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(URL).status_code, 405)


class FullStackTests(SimpleTestCase):
    """The real view and planner, with only the outside world (routing, stations, geocoding) faked."""

    def test_end_to_end_response_shape(self):
        route = straight_route(-120.0, -75.0)
        stations = [(-119.5 + i, 3.0 + (i % 7) * 0.1) for i in range(45)]
        with FakeWorld(route, make_store(stations), places()) as world:
            response = post(self.client, {"start": "a", "finish": "b"})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        for key in ("start", "finish", "distance_miles", "assumptions", "fuel_gallons_total",
                    "total_fuel_cost_usd", "start_fill", "fuel_stops", "route_geojson", "meta"):
            self.assertIn(key, body)
        self.assertEqual(body["route_geojson"]["type"], "FeatureCollection")
        self.assertEqual(body["meta"]["external_api_calls"], 3)
        self.assertEqual(world.fetch_route.call_count, 1)