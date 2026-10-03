from functools import lru_cache
from pathlib import Path
import csv
from unittest import skip
import numpy as np
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .geo import to_xyz

class StationStore:
    def __init__(self, meta: list[dict], skipped_no_coordinates: int):
        self.meta = meta
        self.skipped_no_coordinates = skipped_no_coordinates
        self.lat = np.array([m["lat"] for m in meta], dtype=float)
        self.lon = np.array([m["lon"] for m in meta], dtype=float)
        self.price = np.array([m["price"] for m in meta], dtype=float)
        self.xyz = to_xyz(self.lat, self.lon)

    def __len__(self) -> int:
        return len(self.meta)

def squash(text: str) -> str:
    # trim & collpase repeated whitespaces
    return " ".join(text.split())

def load_store(path: Path) -> StationStore:
    # Read generated geocoded csv file
    try:
        file = open(path, newline="", encoding="utf-8")
    except:
        raise ImproperlyConfigured(f"Geocoded fuel file not found: {path}. Run scripts/geocode_stations.py first.") from None
    
    meta, skipped = [], 0
    with file:
        for row in csv.DictReader(file):
            lat, lon = row["lat"].strip(), row["lon"].strip()

            if not lat or not lon:
                skipped += 1
                continue
                
            meta.append({
                "id": row["OPIS Truckstop ID"].strip(),
                "name": squash(row["Truckstop Name"]),
                "address": squash(row["Address"]),
                "city": squash(row["City"]),
                "state": squash(row["State"]),
                "price": float(row["Retail Price"]),
                "lat": float(lat),
                "lon": float(lon),
                "geocode_source": row["geocode_source"],
            })

    if not meta:
        raise ImproperlyConfigured(f"No usable stations with coordinates in {path}.")
    
    return StationStore(meta, skipped)

@lru_cache(maxsize=1)
def get_store() -> StationStore:
    return load_store(Path(settings.GEOCODED_STATIONS_CSV))
