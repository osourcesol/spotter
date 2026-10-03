import tempfile
from pathlib import Path
import numpy as np
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from spotter.services.geo import to_xyz
from spotter.services.stations import load_store

HEADER = ("OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price,lat,lon,geocode_source,geocode_display,osm_class\n")

def write_csv(body: str) -> Path:
    temp_file = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    temp_file.write(HEADER + body)
    temp_file.close()

    return Path(temp_file.name)

class LoadStoreTests(SimpleTestCase):
    def test_keeps_every_row_with_coordinates_including_repeated_locations(self):
        path = write_csv(
            '105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport  ,MI,260,3.269,43.36,-83.88,name_city,x,amenity/fuel\n'
            '105,TA SAGINAW,"I-75, EXIT 144-B",Bridgeport  ,MI,260,3.339,43.36,-83.88,name_city,x,amenity/fuel\n'
            "9,PILOT,US-60,Mesa,AZ,1,3.5,33.4,-111.8,city_only,y,boundary/administrative\n"
        )
        store = load_store(path)
        self.assertEqual(len(store), 3)                              # repeats are NOT merged
        self.assertEqual(store.price.tolist(), [3.269, 3.339, 3.5])
        self.assertEqual(store.meta[0]["city"], "Bridgeport")        # stray spaces trimmed
        self.assertEqual(store.meta[0]["address"], "I-75, EXIT 144-B")
    
    def test_rows_without_coordinates_are_counted_and_skipped(self):
        path = write_csv(
            "1,A,US-1,X,AL,1,3.0,33.0,-86.0,name_city,x,amenity/fuel\n"
            "2,B,US-2,Mc Calla,AL,1,3.1,,,not_found,,\n"
        )
        store = load_store(path)
        self.assertEqual((len(store), store.skipped_no_coordinates), (1, 1))

    def test_arrays_are_aligned(self):
        path = write_csv("1,A,US-1,X,AL,1,3.0,33.0,-86.0,n,x,c\n2,B,US-2,Y,AL,1,3.1,34.0,-87.0,n,x,c\n")
        s = load_store(path)
        self.assertEqual(s.lat.shape, s.lon.shape)
        self.assertEqual(s.xyz.shape, (2, 3))
        self.assertEqual(s.meta[1]["id"], "2")
        self.assertEqual(s.price[1], 3.1)
    
    def test_missing_file_raises_clear_error(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "scripts/geocode_stations.py"):
            load_store(Path("/nonexistent/fuel.csv"))
    
    def test_file_with_no_usable_rows_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            load_store(write_csv("2,B,US-2,Y,AL,1,3.1,,,not_found,,\n"))

class GeoTests(SimpleTestCase):
    def test_one_degree_of_latitude_is_about_69_miles(self):
        a, b = to_xyz(np.array([35.0, 36.0]), np.array([-100.0, -100.0]))
        self.assertAlmostEqual(float(np.linalg.norm(a - b)), 69.1, delta=0.2)