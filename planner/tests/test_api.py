"""End-to-end API tests. OSRM is mocked with a real recorded NYC -> LA response."""

import json
from pathlib import Path
from unittest import mock

import requests
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase

from planner.services import routing

FIXTURE = Path(__file__).parent / "fixtures" / "osrm_new_york_los_angeles.json"
URL = "/api/v1/route-plan/"
NY_LA = {"start": "New York, NY", "finish": "Los Angeles, CA"}


def osrm_response(status=200, payload=None):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload if payload is not None else json.loads(FIXTURE.read_text())
    return response


class RoutePlanAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_fuel_stations", stdout=open("/dev/null", "w"))

    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(routing, "_get_session")
        self.session = patcher.start().return_value
        self.session.get.return_value = osrm_response()
        self.addCleanup(patcher.stop)

    def get(self, **params):
        return self.client.get(URL, {**NY_LA, **params}, HTTP_ACCEPT="application/json")

    def test_plan_shape_and_invariants(self):
        res = self.get()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        summary, stops = body["summary"], body["fuel_stops"]

        self.assertAlmostEqual(summary["total_distance_miles"], 2800.6, places=1)
        self.assertGreaterEqual(summary["number_of_fuel_stops"], 5)  # 2800 mi needs >= 5 stops after a full tank
        self.assertEqual(summary["number_of_fuel_stops"], len(stops))
        self.assertAlmostEqual(summary["total_fuel_cost"], sum(s["cost"] for s in stops), delta=0.05)
        # Full start tank (50 gal) + purchases == fuel burned (280 gal) + leftover.
        self.assertAlmostEqual(
            50 + summary["total_gallons_purchased"],
            summary["fuel_used_gallons"] + summary["fuel_at_destination_gallons"],
            delta=0.05,
        )
        markers = [s["mile_marker"] for s in stops]
        self.assertEqual(markers, sorted(markers))
        gaps = [b - a for a, b in zip([0] + markers, markers + [summary["total_distance_miles"]])]
        self.assertLessEqual(max(gaps), 500 + 1)  # never more than one tank between stops
        for s in stops:
            self.assertLessEqual(s["fuel_on_arrival_gallons"] + s["gallons_purchased"], 50 + 0.01)

        geo = body["map"]
        self.assertEqual(geo["type"], "FeatureCollection")
        kinds = [f["properties"]["kind"] for f in geo["features"]]
        self.assertEqual(kinds.count("fuel_stop"), len(stops))
        self.assertIn("route", kinds)
        self.assertTrue(body["map_url"].startswith("http://testserver/api/v1/route-plan/map/?"))

    def test_exactly_one_routing_call_then_cached(self):
        first = self.get().json()
        second = self.get().json()
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(first["meta"]["routing_api_calls"], 1)
        self.assertEqual(second["meta"]["routing_api_calls"], 0)
        self.assertTrue(second["meta"]["plan_from_cache"])
        self.assertEqual(first["summary"]["total_fuel_cost"], second["summary"]["total_fuel_cost"])

    def test_route_cache_reused_for_different_vehicle_settings(self):
        self.get()
        res = self.get(stop_penalty=0).json()
        self.assertEqual(self.session.get.call_count, 1)
        self.assertEqual(res["meta"]["optimizer"], "sliding_window_min_cost")

    def test_pure_cheapest_is_never_more_expensive(self):
        practical = self.get().json()["summary"]
        cheapest = self.get(stop_penalty=0).json()["summary"]
        self.assertLessEqual(cheapest["total_fuel_cost"], practical["total_fuel_cost"])
        self.assertGreaterEqual(cheapest["number_of_fuel_stops"], practical["number_of_fuel_stops"])

    def test_post_json_body(self):
        res = self.client.post(URL, {**NY_LA, "include_geometry": False}, content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("map", res.json())

    def test_empty_start_tank_buys_all_trip_fuel(self):
        summary = self.get(start_fuel_gallons=0).json()["summary"]
        self.assertAlmostEqual(
            summary["total_gallons_purchased"],
            summary["fuel_used_gallons"] + summary["fuel_at_destination_gallons"],
            delta=0.05,
        )

    def test_unknown_city(self):
        res = self.get(start="Chicgo, IL")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"]["code"], "location_not_found")
        self.assertIn("Chicago, IL", res.json()["error"]["message"])
        self.session.get.assert_not_called()  # no wasted API call on bad input

    def test_validation_errors(self):
        res = self.client.get(URL, {"start": "Dallas, TX"}, HTTP_ACCEPT="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("finish", res.json()["error"]["fields"])
        self.assertEqual(self.get(mpg=0).status_code, 400)

    def test_vehicle_range_too_short(self):
        res = self.get(range_miles=50)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "no_feasible_fuel_plan")

    def test_no_route(self):
        self.session.get.return_value = osrm_response(400, {"code": "NoRoute", "message": "Impossible route"})
        res = self.get(finish="Honolulu, HI")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.json()["error"]["code"], "route_not_found")

    def test_routing_service_down(self):
        self.session.get.side_effect = requests.ConnectionError("down")
        res = self.get()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["error"]["code"], "routing_service_unavailable")

    def test_osrm_request_shape(self):
        self.get()
        url = self.session.get.call_args.args[0]
        params = self.session.get.call_args.kwargs["params"]
        self.assertIn("/route/v1/driving/-73.996705,40.748380;-118.247896,33.973093", url)  # lon,lat order
        self.assertEqual(params["overview"], "full")
        self.assertEqual(params["geometries"], "polyline6")

    def test_map_page(self):
        res = self.client.get("/api/v1/route-plan/map/", NY_LA)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "leaflet")
        self.assertContains(res, "Total fuel cost")
        bad = self.client.get("/api/v1/route-plan/map/", {"start": "Nowhere, ZZ", "finish": "Dallas, TX"})
        self.assertEqual(bad.status_code, 400)

    def test_health_and_root(self):
        self.assertEqual(self.client.get("/api/v1/health/").json()["stations_loaded"], 6626)
        self.assertEqual(self.client.get("/api/v1/", HTTP_ACCEPT="application/json").status_code, 200)

    def test_response_time_header(self):
        self.assertIn("X-Response-Time-ms", self.get().headers)


class ImportCommandTests(TestCase):
    def test_import_is_idempotent_and_dedupes(self):
        from planner.models import FuelStation

        call_command("load_fuel_stations", stdout=open("/dev/null", "w"))
        call_command("load_fuel_stations", stdout=open("/dev/null", "w"))
        self.assertEqual(FuelStation.objects.count(), 6626)
        self.assertFalse(FuelStation.objects.exclude(state__in=get_us_states()).exists())
        # OPIS 20 appears twice in the CSV with the same price.
        self.assertEqual(FuelStation.objects.filter(opis_id=20).count(), 1)


def get_us_states():
    from planner.services.geocoding import get_gazetteer

    return get_gazetteer().states
