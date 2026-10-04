from dataclasses import dataclass
from pyexpat import features
import numpy as np
import requests
from django.conf import settings

from .geo import METERS_PER_MILE, haversine_mi

ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
GEOCODE_URL = "https://api.openrouteservice.org/geocode/search"

COARSE_LAYERS = {"county", "macrocounty", "region", "macroregion", "country", "coarse"}

_session = requests.Session()

class RoutingError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status

@dataclass
class Route:
    total_miles: float
    grid_miles: np.ndarray
    grid_lat: np.ndarray
    grid_lon: np.ndarray

def build_route(coordinates, distance: float) -> Route:
    arr = np.asarray(coordinates, dtype=float)

    if arr.ndim != 2 or len(arr) < 2:
        raise RoutingError("Routing provider returned an empty route")
    
    lon, lat = arr[:, 0], arr[:, 1]

    # distance along polyline
    segments = haversine_mi(lat[:-1], lon[:-1], lat[1:], lon[1:])
    along = np.concatenate(([0.0], np.cumsum(segments)))

    if along[-1] <= 0:
        raise RoutingError("Routing provider returned a zero-length route")
    
    # the total should be equal to the road distance as defined in API
    total = distance / METERS_PER_MILE
    along *= total / along[-1]

    # resample distance
    grid = np.append(np.arange(0.0, total, 1.0), total)
    
    return Route(total, grid, np.interp(grid, along, lat), np.interp(grid, along, lon))

def _error_message(resp) -> str:
    try:
        err = resp.json().get("error")
    except(ValueError, AttributeError):
        err = None
    
    if isinstance(err, dict):
        err = err.get("message")
    
    return err or "No route found"

def fetch_route(start: tuple[float, float], finish: tuple[float, float]) -> Route:
    # Note: ORS requires coordinates as [lon, lat]
    
    if not settings.ORS_API_KEY:
        raise RoutingError("ORS_API_KEY is not configured on the server.", 500)
    
    try:
        resp = _session.post(
            ORS_URL,
            timeout=(3, 20),
            headers={"Authorization": settings.ORS_API_KEY},
            json={
                "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],
                "instructions": False,
                "radiuses": [-1, -1]
            }
        )
    except requests.RequestException as e:
        raise RoutingError(f"Routing provider unreachable: {e}") from e
    
    if resp.status_code in (401, 403):
        raise RoutingError("Either unautorhized or reached maximum daily limit", 502)

    if resp.status_code == 429:
        raise RoutingError("Reached rate limit, please try again shortly.", 503)
    
    if 400 <= resp.status_code < 500:
        raise RoutingError(_error_message(resp), 422)

    if not resp.ok:
        raise RoutingError(f"ORS error ({resp.status_code}).", 502)

    try:
        feature = resp.json()["features"][0]
        coordinates = feature["geometry"]["coordinates"]
        distance = feature["properties"]["summary"]["distance"]
    except(KeyError, IndexError, TypeError, ValueError) as e:
        raise RoutingError("Returned an unexpected response.") from e

    return build_route(coordinates, distance)

def geocode_us(text: str) -> tuple[float, float, str] | None:
    if not settings.ORS_API_KEY:
        raise RoutingError("ORS_API_KEY is not configured", 500)

    # find a place in US by name or address
    try:
        resp = _session.get(
            GEOCODE_URL,
            timeout=(3,10),
            headers={"Authorization": settings.ORS_API_KEY},
            params={"text": text, "boundary.country": "US", "size": 1}
        )
    except requests.RequestException as e:
        raise RoutingError(f"Geocoding provider unreachable: {e}") from e

    if resp.status_code in (401, 403):
        raise RoutingError("Either unautorhized or reached maximum daily limit", 502)

    if resp.status_code == 429:
        raise RoutingError("Reached rate limit, please try again shortly.", 503)
    
    if 400 <= resp.status_code < 500:
        raise RoutingError(_error_message(resp), 422)

    if not resp.ok:
        raise RoutingError(f"ORS error ({resp.status_code}).", 502)
    
    try:
        features = resp.json().get("features") or []
        
        if not features:
            return None

        properties = features[0].get("properties", {})

        if properties.get("layer") in COARSE_LAYERS:
            return None
        
        lon, lat = features[0]["geometry"]["coordinates"][:2]
        label = properties.get("label") or text

        return float(lat), float(lon), label
    except(KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        raise RoutingError("Geocoding provider returned an unexpected response") from e

    