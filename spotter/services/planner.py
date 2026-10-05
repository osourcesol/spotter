import time
import numpy as np
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import re
from django.core.cache import cache
import json
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

from .corridor import stations_along_route
from .locations import resolve_location
from .optimizer import NoReachableStation, plan_fuel
from .ors import fetch_route
from .stations import get_store

START_WINDOW_MILES = 10.0
WIDER_RADII_MILES = (15.0, 30.0)
_CENT = Decimal("0.01")
PLAN_CACHE_SECONDS = 24 * 3600
ROUTE_CACHE_SECONDS = 24 * 3600

@dataclass(frozen=True)
class PlanOptions:
    max_range_miles: float = 500.0
    mpg: float = 10.0
    min_saving_per_gallon: float = 0.05
    min_purchase_gallons: float = 5.0
    detour_penalty_per_mile: float = 0.02
    corridor_radius_miles: float = 5.0

    @property
    def tank_gallons(self) -> float:
        return self.max_range_miles / self.mpg

    def corridor_radii(self) -> tuple:
        return (self.corridor_radius_miles, *(r for r in WIDER_RADII_MILES if r > self.corridor_radius_miles))

OPTION_SPECS = [
    {"key": "max_range_miles", "label": "Vehicle range (miles)", "min": 50.0, "max": 1000.0, "step": 10.0,
     "help": "How far the vehicle can go on a full tank."},
    {"key": "mpg", "label": "Fuel economy (mpg)", "min": 1.0, "max": 100.0, "step": 0.5,
     "help": "Miles per gallon. Range divided by this gives the tank size."},
    {"key": "min_saving_per_gallon", "label": "Minimum saving to stop ($/gal)", "min": 0.0, "max": 1.0, "step": 0.01,
     "help": "A station only counts as cheaper if it beats the current price by at least this much. 0 gives the cheapest possible plan."},
    {"key": "min_purchase_gallons", "label": "Minimum purchase (gallons)", "min": 0.0, "max": 100.0, "step": 1.0,
     "help": "Never buy less than this at a stop. 0 allows any amount."},
    {"key": "detour_penalty_per_mile", "label": "Detour penalty ($/gal per mile off route)", "min": 0.0, "max": 0.5, "step": 0.005,
     "help": "Added to a station's price for every mile it is off the route. 0 ignores detours."},
    {"key": "corridor_radius_miles", "label": "Search radius (miles)", "min": 0.5, "max": 50.0, "step": 0.5,
     "help": "How far from the route to look for stations first. It widens on its own if the range needs it."},
]

for _spec in OPTION_SPECS:
    _spec["default"] = getattr(PlanOptions, _spec["key"])

_PRECISION = {"name_city": "station", "city_only": "town centre", "area_only": "wider area"}

class SameLocation(ValueError):
    """ start and end locations are same """

def _cents(value: float) -> Decimal:
    # round price to cents, half-up
    return Decimal(str(float(value))).quantize(_CENT, rounding=ROUND_HALF_UP)

def _choose_plan(route, store, options):
    last_error = NoReachableStation(0.0)

    for radius in options.corridor_radii():
        corridor = stations_along_route(route, store, radius)
        
        if len(corridor) == 0:
            continue
            
        real_prices = store.price[corridor.station_index]
        # price plus a penalty for driving off the route
        compared = real_prices + options.detour_penalty_per_mile * corridor.off_route_miles
        near_start = corridor.mile_marker <= START_WINDOW_MILES

        if near_start.any():
            start_position = int(np.flatnonzero(near_start)[np.argmin(compared[near_start])])
        else:
            start_position = 0

        miles = [0.0] + corridor.mile_marker.tolist() + [route.total_miles]
        node_prices = [float(compared[start_position])] + compared.tolist() + [0.0]

        try:
            purchases = plan_fuel(miles, node_prices, max_range_miles=options.max_range_miles, mpg=options.mpg,
                                  min_saving_per_gallon=options.min_saving_per_gallon,
                                  min_purchase_gallons=options.min_purchase_gallons)
        except NoReachableStation as e:
            last_error = e
            continue
        
        return purchases, corridor, start_position, radius, real_prices
    
    raise last_error

def _station_payload(store, index, mile_marker, off_route_miles, price, gallons) -> dict:
    station = store.meta[index]
    payload = {
        "id": station["id"],
        "name": station["name"],
        "address": station["address"],
        "city": station["city"],
        "state": station["state"],
        "lat": round(station["lat"], 5),
        "lon": round(station["lon"], 5),
        "geocode_source": station["geocode_source"],
        "location_precision": _PRECISION.get(station["geocode_source"], "unknown"),
        "mile_marker": round(float(mile_marker), 1),
        "off_route_miles": round(float(off_route_miles), 1),
        "price_per_gallon": round(float(price), 3),
        "gallons": round(float(gallons), 3),
        "cost_usd": float(_cents(gallons * price)),
    }

    return payload

def _geojson(route, start, finish, stops) -> dict:
    features = [{
        "type": "Feature",
        "properties": {"kind": "route"},
        "geometry": {
            "type": "LineString",
            "coordinates": [[round(float(lon), 5), round(float(lat), 5)] for lat, lon in zip(route.grid_lat, route.grid_lon)]
        }
    }]

    for kind, place in (("start", start), ("finish", finish)):
        features.append({
            "type": "Feature",
            "properties": {
                "kind": kind,
                "label": place.label
            },
            "geometry": {
                "type": "Point",
                "coordinates": [place.lon, place.lat]
            }
        })
    
    for stop in stops:
        features.append({
            "type": "Feature",
            "properties": {
                "kind": "fuel_stop",
                "order": stop["order"],
                "name": stop["name"],
                "price_per_gallon": stop["price_per_gallon"],
                "gallons": stop["gallons"],
                "off_route_miles": stop["off_route_miles"],
                "location_precision": stop["location_precision"]
            },
            "geometry": {
                "type": "Point",
                "coordinates": [stop["lon"], stop["lat"]]
            }
        })
    
    feature_collection = {
        "type": "FeatureCollection",
        "features": features
    }
    return feature_collection

def _start_fill_payload(store, corridor, position, price, gallons) -> dict:
    station_mile = float(corridor.mile_marker[position])
    station = _station_payload(store, corridor.station_index[position], station_mile, corridor.off_route_miles[position], price, gallons)

    fill = {
        "mile_marker": 0.0,
        "gallons": station.pop("gallons"),
        "price_per_gallon": station.pop("price_per_gallon"),
        "cost_usd": station.pop("cost_usd"),
        "priced_at": station,
    }

    if station_mile <= START_WINDOW_MILES:
        fill["note"] = (f"Initial fuel-up at the cheapest station within {START_WINDOW_MILES:.0f} miles of the start")
    else:
        fill["note"] = (f"The fuel data has no station within {START_WINDOW_MILES:.0f} miles of the start. Priced at the nearest station on the route, {station_mile:.0f} miles ahead.")
    
    return fill

def _get_route(start, finish):
    # same route again (only the rules changed) comes from the cache, no new routing request
    key = "route:%.4f,%.4f>%.4f,%.4f" % (start.lat, start.lon, finish.lat, finish.lon)
    cached = cache.get(key)

    if cached is not None:
        return cached, True

    route = fetch_route((start.lat, start.lon), (finish.lat, finish.lon))
    cache.set(key, route, ROUTE_CACHE_SECONDS)
    return route, False

def build_plan(start_text: str, finish_text: str, options: PlanOptions = None) -> dict:
    options = options or PlanOptions()
    started = time.perf_counter()
    start, finish = resolve_location(start_text), resolve_location(finish_text)

    if (round(start.lat, 4), round(start.lon, 4)) == (round(finish.lat, 4), round(finish.lon, 4)):
        raise SameLocation("start and end locations are same")
    
    route, route_from_cache = _get_route(start, finish)
    store = get_store()
    purchases, corridor, start_position, radius, real_prices = _choose_plan(route, store, options)

    stops, start_fill, line_items = [], None, []

    for purchase in purchases:
        # cost uses the real price, not the penalised one
        if purchase.node == 0:
            price = float(real_prices[start_position])
            line_items.append(_cents(purchase.gallons * price))
            start_fill = _start_fill_payload(store, corridor, start_position, price, purchase.gallons)
            continue
    
        k = purchase.node - 1
        price = float(real_prices[k])
        line_items.append(_cents(purchase.gallons * price))
        stop = _station_payload(store, corridor.station_index[k], corridor.mile_marker[k], corridor.off_route_miles[k], price, purchase.gallons)
        stop["order"] = len(stops) + 1
        stops.append(stop)
    
    total_cost = sum(line_items, Decimal("0.00"))
    total_gallons = float(sum(p.gallons for p in purchases))
    network_calls = (0 if route_from_cache else 1) + sum(1 for place in (start, finish) if place.source == "geocoder")

    plan = {
        "start": {
            "label": start.label,
            "lat": round(start.lat, 5),
            "lon": round(start.lon, 5)
        },
        "finish": {
            "label": finish.label,
            "lat": round(finish.lat, 5),
            "lon": round(finish.lon, 5)
        },
        "distance_miles": round(route.total_miles, 1),
        "assumptions": {
            **asdict(options),
            "start_tank": f"the first fuel-up is priced at the cheapest station within {START_WINDOW_MILES:.0f} miles of the start"
        },
        "fuel_gallons_total": round(total_gallons, 3),
        "total_fuel_cost_usd": float(total_cost),
        "start_fill": start_fill,
        "fuel_stops": stops,
        "route_geojson": _geojson(route, start, finish, stops),
        "meta": {
            "external_api_calls": network_calls,
            "route_from_cache": route_from_cache,
            "corridor_radius_miles": radius,
            "candidate_stations": len(corridor),
            "cache_hit": False,
            "compute_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    }

    return plan

def _cache_key(start_text: str, finish_text: str, options: PlanOptions) -> str:
    def clean(text: str) -> str:
        return re.sub(r"\s*,\s*", ",", " ".join(text.split()).lower())
    
    rules = json.dumps(asdict(options), sort_keys=True)
    return "plan:" + hashlib.sha1(f"{clean(start_text)}|{clean(finish_text)}|{rules}".encode()).hexdigest()

def get_plan(start_text: str, finish_text: str, options: PlanOptions = None) -> dict:
    options = options or PlanOptions()
    started = time.perf_counter()
    key = _cache_key(start_text, finish_text, options)
    cached = cache.get(key)

    if cached is not None:
        meta = {
            **cached["meta"],
            "external_api_calls": 0,
            "cache_hit": True,
            "compute_ms": round((time.perf_counter() - started) * 1000, 1)
        }
        
        return {**cached, "meta": meta}
    
    plan = build_plan(start_text, finish_text, options)
    cache.set(key, plan, PLAN_CACHE_SECONDS)
    
    return plan