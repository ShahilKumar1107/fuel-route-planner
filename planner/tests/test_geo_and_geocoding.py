import numpy as np
from django.test import SimpleTestCase

from planner.exceptions import LocationNotFound
from planner.services.geo import (
    chord_to_miles,
    cumulative_miles,
    decode_polyline,
    densify,
    haversine_miles,
    miles_to_chord,
    resample,
)
from planner.services.geocoding import get_gazetteer


class GeoTests(SimpleTestCase):
    def test_decode_polyline_reference_example(self):
        # Google's documented example (precision 5).
        coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
        np.testing.assert_allclose(coords, [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]])

    def test_haversine_known_distance(self):
        # New York -> Los Angeles straight line is ~2,445 miles.
        self.assertAlmostEqual(float(haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)), 2445, delta=5)

    def test_chord_round_trip(self):
        self.assertAlmostEqual(float(chord_to_miles(miles_to_chord(10.0))), 10.0, places=6)

    def test_densify_and_resample(self):
        lats, lons = np.array([40.0, 40.0]), np.array([-100.0, -98.0])  # ~105 mi straight segment
        cum = cumulative_miles(lats, lons)
        d_lats, d_lons, d_cum = densify(lats, lons, cum, 1.0)
        self.assertLessEqual(np.diff(d_cum).max(), 1.0 + 1e-9)
        self.assertAlmostEqual(d_cum[-1], cum[-1])
        r_lats, r_lons = resample(d_lats, d_lons, d_cum, 10.0)
        self.assertEqual((r_lats[0], r_lons[-1]), (d_lats[0], d_lons[-1]))
        self.assertLess(len(r_lats), len(d_lats))


class GeocodingTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.g = get_gazetteer()

    def test_accepted_formats(self):
        for query, expected in [
            ("Chicago, IL", "Chicago, IL"),
            ("chicago il", "Chicago, IL"),
            ("St. Louis, Missouri", "Saint Louis, MO"),
            ("Santa Fe New Mexico", "Santa Fe, NM"),
            ("Austin, TX 78701", "Austin, TX"),
            ("Los Angeles, CA, USA", "Los Angeles, CA"),
        ]:
            with self.subTest(query=query):
                self.assertEqual(self.g.geocode(query).name, expected)

    def test_coordinates(self):
        place = self.g.geocode("40.7128, -74.0060")
        self.assertEqual((place.latitude, place.longitude), (40.7128, -74.006))

    def test_suggests_on_typo(self):
        with self.assertRaisesMessage(LocationNotFound, "Did you mean: Chicago, IL"):
            self.g.geocode("Chicgo, IL")

    def test_rejects_non_us(self):
        for query in ("Paris, France", "51.5,-0.12", "", "Toronto, ON"):
            with self.subTest(query=query), self.assertRaises(LocationNotFound):
                self.g.geocode(query)
