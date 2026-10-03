from unittest import mock

import numpy as np
import requests
from django.test import SimpleTestCase, override_settings

from spotter.services.geo import METERS_PER_MILE, haversine_mi
from spotter.services.ors import RoutingError, build_route, fetch_route

START, FINISH = (36.0, -100.0), (36.0, -99.0)


def straight_line(n=11):
    """A west-to-east line at latitude 36 as [lon, lat] pairs, like the API returns."""
    return [[lon, 36.0] for lon in np.linspace(-100.0, -99.0, n)]


def fake_response(status=200, body=None, json_error=False):
    resp = mock.Mock(status_code=status, ok=status < 400)
    if json_error:
        resp.json.side_effect = ValueError("not json")
    else:
        resp.json.return_value = body
    return resp


def good_body(distance_m):
    return {"features": [{"geometry": {"coordinates": straight_line()},
                          "properties": {"summary": {"distance": distance_m}}}]}


class BuildRouteTests(SimpleTestCase):
    def test_total_matches_api_distance_and_grid_is_one_mile_apart(self):
        geometric_mi = float(haversine_mi(36.0, -100.0, 36.0, -99.0))
        route = build_route(straight_line(), geometric_mi * 1.1 * METERS_PER_MILE)  # road is 10% longer
        self.assertAlmostEqual(route.total_miles, geometric_mi * 1.1, places=6)
        self.assertEqual(route.grid_miles[0], 0.0)
        self.assertAlmostEqual(route.grid_miles[-1], route.total_miles, places=6)
        self.assertTrue(np.allclose(np.diff(route.grid_miles)[:-1], 1.0))   # 1-mile spacing
        self.assertAlmostEqual(route.grid_lon[0], -100.0, places=6)      # starts at the start
        self.assertAlmostEqual(route.grid_lon[-1], -99.0, places=6)      # ends at the end

    def test_rejects_empty_or_zero_length_routes(self):
        with self.assertRaises(RoutingError):
            build_route([[-100.0, 36.0]], 1000)
        with self.assertRaises(RoutingError):
            build_route([[-100.0, 36.0], [-100.0, 36.0]], 1000)


@override_settings(ORS_API_KEY="test-key")
class FetchRouteTests(SimpleTestCase):
    def call(self, response=None, side_effect=None):
        with mock.patch("spotter.services.ors._session.post", return_value=response, side_effect=side_effect) as post:
            try:
                return fetch_route(START, FINISH), post
            except RoutingError as exc:
                return exc, post

    def test_success_sends_one_correct_request(self):
        route, post = self.call(fake_response(200, good_body(90000)))
        self.assertAlmostEqual(route.total_miles, 90000 / METERS_PER_MILE)
        self.assertEqual(post.call_count, 1)                                     # exactly ONE call
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["coordinates"], [[-100.0, 36.0], [-99.0, 36.0]])  # [lon, lat]
        self.assertIs(kwargs["json"]["instructions"], False)
        self.assertEqual(kwargs["headers"], {"Authorization": "test-key"})
        self.assertIsNotNone(kwargs["timeout"])

    def test_error_statuses_are_mapped(self):
        cases = [
            (fake_response(401, {"error": "x"}), 502),
            (fake_response(403, {"error": "Daily quota reached or API key unauthorized"}), 502),
            (fake_response(429, {}), 503),
            (fake_response(500, {}), 502),
        ]
        for resp, expected in cases:
            exc, _ = self.call(resp)
            self.assertEqual(exc.status, expected, resp.status_code)

    def test_unroutable_point_returns_422_with_message_in_either_error_format(self):
        exc, _ = self.call(fake_response(404, {"error": {"code": 2010, "message": "Could not find routable point"}}))
        self.assertEqual((exc.status, str(exc)), (422, "Could not find routable point"))
        exc, _ = self.call(fake_response(400, {"error": "Parameter exceeds limits"}))
        self.assertEqual((exc.status, str(exc)), (422, "Parameter exceeds limits"))
        exc, _ = self.call(fake_response(404, json_error=True))
        self.assertEqual((exc.status, str(exc)), (422, "No route found"))

    def test_network_failure_becomes_502(self):
        exc, _ = self.call(side_effect=requests.Timeout("slow"))
        self.assertEqual(exc.status, 502)

    def test_unexpected_success_body_becomes_502(self):
        exc, _ = self.call(fake_response(200, {"features": []}))
        self.assertEqual(exc.status, 502)

    @override_settings(ORS_API_KEY="")
    def test_missing_key_fails_before_any_network_call(self):
        exc, post = self.call(fake_response(200, good_body(1000)))
        self.assertEqual(exc.status, 500)
        post.assert_not_called()