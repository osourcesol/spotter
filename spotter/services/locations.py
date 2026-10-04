import hashlib
import re
from typing import NamedTuple

from django.core.cache import cache
from .ors import geocode_us

CACHE_SECONDS = 7 * 24 * 3600
_LATLON = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

LAT_RANGE, LON_RANGE = (17.0, 72.0), (-180.0, -65.0)

class Location(NamedTuple):
    lat: float
    lon: float
    label: str

class LocationNotFound(ValueError):
    """ the text could not be turned into a location """

def resolve_location(text: str) -> Location:
    text = " ".join((text or "").split())

    if not text:
        raise LocationNotFound("Location is empty")
    
    match = _LATLON.match(text)

    if match:
        lat, lon = float(match.group(1)), float(match.group(2))

        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
            raise LocationNotFound(f"Coordinates '{text}' are outside the USA")
        
        return Location(lat, lon, f"{lat:.4f},{lon:.4f}")
    
    key = "geo:" + hashlib.sha1(text.lower().encode()).hexdigest()
    cached = cache.get(key)

    if cached:
        return Location(*cached)
    
    found = geocode_us(text)

    if found is None:
        raise LocationNotFound(f"Could not find '{text}' in the USA. Try 'City, ST' (for example 'Dallas, TX')")
    
    cache.set(key, found, CACHE_SECONDS)
    return Location(*found)