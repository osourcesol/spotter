import time
import numpy as np
from decimal import ROUND_HALF_UP, Decimal
import hashlib
import re
from django.core.cache import cache

from .corridor import stations_along_route
from .locations import resolve_location
from .optimizer import NoReachableStation, plan_fuel
from .ors import fetch_route
from .stations import get_store

MAX_RANGE_MILES = 500.0
MPG = 10.0
CORRIDOR_RADII_MILES = (5.0, 15.0, 30.0)
START_WINDOW_MILES = 10.0
_CENT = Decimal("0.01")
PLAN_CACHE_SECONDS = 24 * 3600

class SameLocation(ValueError):
    """ start and end locations are same """

def _cents(value: float) -> Decimal:
    # round price to cents, half-up
    return Decimal(str(float(value))).quantize(_CENT, rounding=ROUND_HALF_UP)

def _choose_plan(route, store):
    last_error = NoReachableStation(0.0)

    for raidus in CORRIDOR_RADII_MILES:
        corridor = stations_along_route(route, store, raidus)
        
        if len(corridor) == 0:
            continue
            
        prices = store.price[corridor.station_index]
        near_start = corridor.mile_marker <= START_WINDOW_MILES

        if near_start.any():
            start_position = int(np.flatnonzero(near_start)[np.argmin(prices[near_start])])
        else:
            start_position = 0
        
        start_price = float(prices[start_position])

        miles = [0.0] + corridor.mile_marker.tolist() + [route.total_miles]
        node_prices = [start_price] + prices.tolist() + [0.0]

        try:
            purchases = plan_fuel(miles, node_prices, max_range_miles=MAX_RANGE_MILES, mpg=MPG)
        except NoReachableStation as e:
            last_error = e
            continue
        
        return purchases, corridor, start_position, raidus
    
    raise last_error

def _station_payload(store, index, mile_marker, off_route_miles, purchase) -> dict:
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
        "mile_marker": round(float(mile_marker), 1),
        "off_route_miles": round(float(off_route_miles), 1),
        "price_per_gallon": round(float(purchase.price), 3),
        "gallons": round(float(purchase.gallons), 3),
        "cost_usd": float(_cents(purchase.cost)),
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
                "gallons": stop["gallons"]
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

def _start_fill_payload(store, corridor, position, purchase) -> dict:
    station_mile = float(corridor.mile_marker[position])
    station = _station_payload(store, corridor.station_index[position], station_mile, corridor.off_route_miles[position], purchase)

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

def build_plan(start_text: str, finish_text: str) -> dict:
    started = time.perf_counter()
    start, finish = resolve_location(start_text), resolve_location(finish_text)

    if (round(start.lat, 4), round(start.lon, 4)) == (round(finish.lat, 4), round(finish.lon, 4)):
        raise SameLocation("start and end locations are same")
    
    route = fetch_route((start.lat, start.lon), (finish.lat, finish.lon))
    store = get_store()
    purchases, corridor, start_position, radius = _choose_plan(route, store)

    stops, start_fill, line_items = [], None, []

    for purchase in purchases:
        line_items.append(_cents(purchase.cost))

        if purchase.node == 0:
            start_fill = _start_fill_payload(store, corridor, start_position, purchase)
            continue
    
        k = purchase.node - 1
        stop = _station_payload(store, corridor.station_index[k], corridor.mile_marker[k], corridor.off_route_miles[k], purchase)
        stop["order"] = len(stops) + 1
        stops.append(stop)
    
    total_cost = sum(line_items, Decimal("0.00"))
    total_gallons = float(sum(p.gallons for p in purchases))
    network_calls = 1 + sum(1 for place in (start, finish) if place.source == "geocoder")

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
            "max_range_miles": MAX_RANGE_MILES,
            "miles_per_gallon": MPG,
            "start_tank": f"the first fuel-up is priced at the cheapest station within {START_WINDOW_MILES:.0f} miles of the start"
        },
        "fuel_gallons_total": round(total_gallons, 3),
        "total_fuel_cost_usd": float(total_cost),
        "start_fill": start_fill,
        "fuel_stops": stops,
        "route_geojson": _geojson(route, start, finish, stops),
        "meta": {
            "external_api_calls": network_calls,
            "corridor_radius_miles": radius,
            "candidate_stations": len(corridor),
            "cache_hit": False,
            "compute_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    }

    return plan

def _cache_key(start_text: str, finish_text: str) -> str:
    def clean(text: str) -> str:
        return re.sub(r"\s*,\s*", ",", " ".join(text.split()).lower())
    
    return "plan:" + hashlib.sha1(f"{clean(start_text)}|{clean(finish_text)}".encode()).hexdigest()

def get_plan(start_text: str, finish_text: str) -> dict:
    started = time.perf_counter()
    key = _cache_key(start_text, finish_text)
    cached = cache.get(key)

    if cached is not None:
        meta = {
            **cached["meta"],
            "external_api_calls": 0,
            "cache_hit": True,
            "compute_ms": round((time.perf_counter() - started) * 1000, 1)
        }
        
        return {**cached, "meta": meta}
    
    plan = build_plan(start_text, finish_text)
    cache.set(key, plan, PLAN_CACHE_SECONDS)
    
    return plan