import argparse
import csv
import json
from pathlib import Path
import random
import time
import urllib.parse
import urllib.request
import re

from numpy import place

ROOT = Path(__file__).resolve().parent.parent
CSV_FILE = ROOT / "data" / "fuel-prices-for-be-assessment.csv"
GEOCODED_STATIONS_FILE = ROOT / "data" / "fuel-stations-geocoded.csv" # All columns included in spotter's provided csv file + lat, lon, geocode_source, geocode_display
CACHE = ROOT / "data" / "geocode-cache.json"

USER_AGENT = "spotter-fuel-assessment/1.0 (osourcesol@gmail.com)"
DELAY_S = 1.1
QUERY_LIMIT = 5

# Spelling fixes verified by probing (e.g. OpenStreetMap has "Love's", however we have "LOVES" in the CSV).
BRAND_FIX = {"LOVES": "Love's", "CASEYS": "Casey's", "QUIKTRIP": "QuikTrip", "7-ELEVEN": "7-Eleven"}

# Generic words that stop OpenStreetMap from matching a name.
GENERIC = {"TRAVEL", "CENTER", "CENTERS", "STOP", "STOPPING", "PLAZA"}

# OpenStreetMap object kinds that are a single place of business (accepted for name_city).
STATION_CATEGORIES = {"amenity", "shop", "tourism", "building", "office"}

# To expand St/Ft/Mt (e.g. 'St. Louis' == 'Saint Louis')
_WORDS = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}

# Rivers, roads, parks etc. can share a town's name (e.g. "Cedar Creek" the river) must not match.
PLACE_CATEGORIES = {"place", "boundary"}

def load_rows() -> tuple[list[str], list[dict]]:
    with open(CSV_FILE, newline="", encoding="utf-8-sig") as file:
        reader = csv.reader(file)
        header = next(reader)
        assert len(header) == 7, f"Unexpected columns: {header}"
        keys = ["id", "name", "address", "city", "state", "rack", "price"]
        return header, [dict(zip(keys, r)) for r in reader]

def load_cache() -> dict:
    if CACHE.exists():
        data = json.loads(CACHE.read_text(encoding="utf-8"))

        if data.get("version") == 2:
            return data
        
        print("Ignore old format cache")
    
    return {"version": 2, "queries": {}}

def attempts(row: dict) -> list[tuple[str, str]]:
    city, state = squash(row["city"]), squash(row["state"])
    return [
        ("name_city", f"{search_name(row)}, {city}, {state}"),
        ("city_only", f"{city}, {state}")
    ]

def nominatim(query: str) -> list[dict]:
    # request to public OpenStreetMap Nominatim search API
    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "limit": QUERY_LIMIT,
        "addressdetails": 1,
        "countrycodes": "us,ca"
    })
    
    req = urllib.request.Request("https://nominatim.openstreetmap.org/search?" + params,
        headers={"User-Agent": USER_AGENT}
    )

    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)

def in_state(hit: dict, state: str) -> bool:
    """
    OpenStreetMap return codes like US-MI or CA-ON.
    If it gives no code at all we cannot verify.
    """
    iso = hit["iso"]
    return (not iso) or iso.split("-")[-1].upper() == state.upper()

def is_station_like(hit: dict) -> bool:
    category = hit["category"]
    t = hit["type"]
    return category in STATION_CATEGORIES or (category, t) == ("highway", "services")


def resolve(row: dict, cache: dict) -> dict | None:
    # Try each attempt in order
    city, state = squash(row["city"]), squash(row["state"])
    
    for source, query in attempts(row):
        choosen = pick(search(query, cache), source, state, city)
        if choosen:
            hit, label = choosen
            return {
                "lat": float(hit["lat"]),
                "lon": float(hit["lon"]),
                "source": source,
                "display": hit.get("display_name", ""),
                "osm_class": f"{hit['category']}/{hit['type']}"
            }
    
    return {
        "lat": "",
        "lon": "",
        "source": "not_found",
        "display": "",
        "osm_class": ""
    }


def squash(text: str) -> str:
    return " ".join(text.split())

def location_key(row: dict) -> str:
    loc_key = "|".join(squash(row[col]) for col in ("name", "address", "city", "state"))
    return loc_key

def save_cache(cache: dict) -> None:
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")

def compact(text: str) -> str:
    """
    Lowercase, drop apostrophes/punctuation/spaces, expand St/Ft/Mt and strip a leading 'City of'/'Town of'
    e.g 'St. Louis' == 'Saint Louis' and 'DUNDAS' == 'Dundas'
    """
    t = text.lower().replace("'", "")
    t = re.sub(r"^(city|town|village|township|borough) of\s+", "", t.strip())
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = "".join(_WORDS.get(w, w) for w in t.split())

    return t

def city_matches(display_name: str, city: str) -> str:
    # To match the row's city with whole comma-separated part of OpenStreetMap display_name
    compact_text = compact(city)
    return any(compact(part) == compact_text for part in display_name.split(","))

def search_name(row: dict) -> str:
    # e.g. LOVES TRAVEL STOP #248 => Love's  PETRO STOPPING CENTER JOHNSTOWN (Johnstown) => PETRO
    words = squash(re.sub(r"#\s*\d+.*$", "", row["name"])).split()

    if words and words[0].upper() in BRAND_FIX:
        words[0] = BRAND_FIX[words[0].upper()]
    
    city_name = [w for w in words if w.upper() not in GENERIC] or words
    city_words = squash(row["city"]).upper().split()
    n = len(city_words)

    if len(city_name) > n and [w.upper() for w in city_name[-n:]] == city_words:
        city_name = city_name[:-n]

    if compact(" ".join(city_name)) == compact(squash(row["city"])):
        city_name = " ".join(words)
    else:
        city_name = " ".join(city_name)
    
    return city_name

def search(query: str, cache: dict) -> dict | None:
    queries = cache["queries"]

    if query in queries:
        return queries[query]
    
    hits = nominatim(query)
    time.sleep(DELAY_S)
    
    queries[query] = [{
        "lat": float(h["lat"]),
        "lon": float(h["lon"]),
        "display_name": h.get("display_name", ""),
        "category": h.get("category", ""),
        "type": h.get("type", ""),
        "iso": h.get("address", {}).get("ISO3166-2-lvl4", "")
    } for h in hits]
    
    return queries[query]

def pick(hits: dict | None, source: str, state: str, city: str) -> tuple[dict, str] | None:
    # Choose the best acceptible hit
    hits = [h for h in hits if in_state(h, state)]

    if source == "name_city":
        for h in hits:
            if is_station_like(h) and city_matches(h["display_name"], city):
                return h, "name_city"
        return None
    
    places = [h for h in hits if h["category"] in PLACE_CATEGORIES]
    for h in places:
        if compact(h["display_name"].split(",")[0]) == compact(city):
            return h, "city_only"

    return (places[0], "area_only") if places else None

def main() -> None:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--sample", type=int, help="geocode N locations, print them, write no CSV")
    args = arg_parser.parse_args()

    header, rows = load_rows()
    cache = load_cache()

    # One representation row per unique location
    unique: dict[str, dict] = {}
    for r in rows:
        unique.setdefault(location_key(r), r)
    
    print(f"{len(rows)} rows / {len(unique)} unique locations / {len(cache['queries'])} queries cached")

    keys = list(unique)
    
    if args.sample:
        keys = random.Random(42).sample(keys, min(args.sample, len(keys)))
    
    results: dict[str, dict] = {}
    started = time.time()

    try:
        for n, key in enumerate(keys, 1):
            r = unique[key]

            try:
                results[key] = resolve(r, cache)
            except Exception as e:
                print(f"network error for {squash(r['name'])} : {e}")
                continue

            if args.sample:
                g = results[key]
                print(f"\n[{g['source']}] {squash(r['name'])} | {squash(r['address'])} | {squash(r['city'])}, {squash(r['state'])}")
                print(f"   -> {g['lat']}, {g['lon']}, {g['osm_class']}, {g['display'][:100]}")
            elif n%100 == 0:
                print(f"{n}/{len(keys)} location done ({(time.time() - started) / 60:.0f} min elapsed)")

            if n % 25 == 0:
                save_cache(cache)

    except KeyboardInterrupt:
        print("\nInterrupted - progress saved; rerun to continue.")
    finally:
        save_cache(cache)

    if args.sample:
        return

    if len(results) < len(keys):
        print(f"{len(keys) - len(results)} locations unresolved, rerun the script after some time")
        return
    
    output_header = header + ["lat", "lon", "geocode_source", "geocode_display", "osm_class"]
    counts: dict[str, int] = {}
    with open(GEOCODED_STATIONS_FILE, "w", newline="", encoding="utf-8") as file:
        w = csv.writer(file)
        w.writerow(output_header)

        for r in rows:
            g = results[location_key(r)]
            counts[g["source"]] = counts.get(g["source"], 0) + 1
            w.writerow([r["id"], r["name"], r["address"], r["city"], r["state"], r["rack"], r["price"], g["lat"], g["lon"], g["source"], g["display"], g["osm_class"]])

    print(f"\nWrote {GEOCODED_STATIONS_FILE.name}: {len(rows)} rows. By geocode_source (rows): {counts}")

if __name__ == "__main__":
    main()