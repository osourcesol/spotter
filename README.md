# Fuel Route Planner

A Django REST API that takes a start and a finish in the USA and returns the driving route, where to stop for fuel, and what the fuel will cost. The vehicle has a 500 mile range and does 10 miles per gallon. Stops are chosen from the fuel price file that came with the assessment.

There is also a small map page at `/` that draws the route and the stops, and lets you change the planning rules.

## How it works

1. The start and finish are looked up with OpenRouteService geocoding. Places seen before come from a cache.
2. **One** routing request to OpenRouteService returns the road geometry.
3. Every station near the road is found with a KD-tree. All fuel prices are loaded into memory at startup, so there is no database.
4. A greedy look-ahead planner decides where to buy fuel and how much. In the tests it is compared against an exact linear programming solver (more on that below).
5. The finished plan is cached. The same trip a second time makes no outside requests at all.

So a new trip costs one routing request, plus one geocoding request for each place that has not been seen before. The response says exactly how many in `meta.external_api_calls`.

## Setup

Python 3.12.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in:

- `ORS_API_KEY` (required): a free key from https://openrouteservice.org/dev/#/signup
- `MAP_TILE_URL` (optional): street map tiles for the map page. Without it the page draws plain US state outlines. A free key with no account is at https://carto.com/basemaps/apikey

Run it:

```bash
python manage.py runserver
```

Then open http://127.0.0.1:8000/, pick a start and finish, and press **Plan route**.

Run the tests (no key and no internet needed, everything outside is faked):

```bash
python manage.py test spotter
```

## API

### `POST /api/route/`

```bash
curl -s -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Plano, TX", "finish": "Houston, TX"}'
```

`start` and `finish` can be `"City, ST"`, an address, or `"lat,lng"`. Both must be in the USA.

An abridged response:

```json
{
  "start": {"label": "Plano, Collin County, Texas, USA", "lat": 33.01984, "lon": -96.69888},
  "finish": {"label": "Houston, Harris County, Texas, USA", "lat": 29.76043, "lon": -95.36980},
  "distance_miles": 260.7,
  "assumptions": {
    "max_range_miles": 500.0, "mpg": 10.0, "min_saving_per_gallon": 0.05,
    "min_purchase_gallons": 5.0, "detour_penalty_per_mile": 0.02, "corridor_radius_miles": 5.0,
    "start_tank": "the first fuel-up is priced at the cheapest station within 10 miles of the start"
  },
  "fuel_gallons_total": 26.07,
  "total_fuel_cost_usd": 75.57,
  "start_fill": {
    "mile_marker": 0.0, "gallons": 5.0, "price_per_gallon": 3.499, "cost_usd": 17.5,
    "priced_at": {"name": "EXXON", "city": "Dallas", "state": "TX", "off_route_miles": 3.4, "location_precision": "station"},
    "note": "Initial fuel-up at the cheapest station within 10 miles of the start"
  },
  "fuel_stops": [
    {"order": 1, "name": "One9 #1248", "city": "Wilmer", "state": "TX", "mile_marker": 38.0,
     "off_route_miles": 0.4, "location_precision": "town centre",
     "price_per_gallon": 2.756, "gallons": 21.07, "cost_usd": 58.07}
  ],
  "route_geojson": {"type": "FeatureCollection", "features": ["..."]},
  "meta": {"external_api_calls": 3, "route_from_cache": false, "corridor_radius_miles": 5.0,
           "candidate_stations": 40, "cache_hit": false, "compute_ms": 1850.0}
}
```

- `start_fill` is the fuel for the first leg. The trip starts on an empty tank, so the first fill-up is priced at the cheapest station within 10 miles of the start. If there is none, the nearest station on the route is used and the note says so.
- `fuel_stops` are the stops after that, in order along the route. `mile_marker` is the distance from the start.
- `off_route_miles` is how far a station is from the road. `location_precision` is how exact its position is (see the limitations).
- `route_geojson` is a ready-to-draw FeatureCollection with the route line, both ends and every stop.
- Money is always `gallons x price`, both as shown, rounded to the cent. The total is the sum of the lines.

### Planning rules

The planner has six rules. Each one is optional in the request, and the map page shows them in a settings panel built from `GET /api/options/`.

```bash
curl -s -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Plano, TX", "finish": "Houston, TX", "options": {"min_purchase_gallons": 0, "min_saving_per_gallon": 0}}'
```

| Option | Default | Allowed | What it does |
|---|---|---|---|
| `max_range_miles` | 500 | 50 to 1000 | How far the vehicle goes on a full tank |
| `mpg` | 10 | 1 to 100 | Fuel economy. Range divided by this is the tank size |
| `min_saving_per_gallon` | 0.05 | 0 to 1 | A station only counts as cheaper if it beats the current price by at least this much |
| `min_purchase_gallons` | 5 | 0 to 100 | Never buy less than this at a stop (it cannot be more than the tank holds) |
| `detour_penalty_per_mile` | 0.02 | 0 to 0.5 | Added to a station's price for each mile it is off the route |
| `corridor_radius_miles` | 5 | 0.5 to 50 | How far from the route to look for stations first. It widens to 15 and then 30 miles by itself if the range needs it |

Unknown option names and out of range values are rejected with a `400` that says which one.

Changing only the rules does not cost a routing request, because the route for the same two places is cached.

### Other endpoints

- `GET /api/options/` the rules above with their labels, defaults and limits
- `GET /` the map page
- `GET /us-states.json` the state outlines used when no tile URL is set

### Errors

| Status | When |
|---|---|
| 400 | A place cannot be found, a place is outside the USA, start and finish are the same, or the request is invalid |
| 422 | No station can be reached within the range (the message gives the mile), or no road route exists |
| 502 / 503 | The routing or geocoding provider is unreachable, or its rate limit was hit |
| 500 | `ORS_API_KEY` is not set |

## How the plan is chosen

Starting at the beginning of the route, at every station the planner looks at everything within one tank of range:

- If a cheaper station is ahead, buy only enough to reach the first one.
- If nothing ahead is cheaper, fill the tank and drive to the cheapest station in range.

The destination counts as a station with price zero, so the last stop buys only what is needed to arrive.

This is the classic "gas station" greedy algorithm, and with both practical rules set to 0 it gives the exact minimum cost. The tests check that claim: about 800 random trips are solved with this planner and with an exact linear programming solver (`scipy.optimize.linprog`), and the costs must match to the cent.

Two rules make the plan practical instead of exact:

- A cheaper station only counts if it is cheaper by `min_saving_per_gallon` or more.
- A stop buys at least `min_purchase_gallons`, but never more than the trip still needs.

Without them, prices that fall a little at every station produce a stop every few miles for a fraction of a gallon. The Plano to Houston trip went from 5 stops to 1 for about $2 more.

Driving to a station that is off the route costs fuel and time. Because only one routing request is allowed per trip, the real detour cannot be routed. Instead, every mile a station is off the route adds `detour_penalty_per_mile` to its price in the planner's comparison. The prices reported in the response are always the real ones.

### What the rules cost

Real results for the same trips, with different rules (stops after the first fill-up, and the total cost):

| Trip | Exact (0 / 0) | Default (0.05 / 5) | Stricter (0.15 / 10) |
|---|---|---|---|
| Dallas to Nashville (668 mi) | 6 stops, $188.56 | 3 stops, $189.63 | 2 stops, $190.80 |
| Denver to Dallas (799 mi) | 6 stops, $236.56 | 4 stops, $237.90 | 2 stops, $242.01 |
| Los Angeles to New York (2,796 mi) | 21 stops, $879.53 | 15 stops, $879.33 | 12 stops, $886.74 |

The defaults remove most of the pointless stops for under 1% of the cost. Stricter rules remove a few more, but each one costs more than the one before. The default in LA to NY is a little cheaper than the "exact" plan because the exact plan is exact for price plus detour penalty, while the reported cost is the pump price only.

A medium trip like Dallas to Nashville only needs one refuel to cover the distance. It gets 3 stops because each of them saves about a dollar or more. Raise the minimum saving in the settings panel to see fewer.

## Results

Each of these was checked by a script that recomputes the totals from the response and the original CSV: gallons add up to distance divided by mpg, the lines add up to the total, no leg is longer than the range, a replay of the tank never runs dry, and every price shown exists for that station in the file.

| Kind | Route | Miles | Stops after first fill-up | Gallons | Total fuel cost |
|---|---|---|---|---|---|
| Short | Dallas to Oklahoma City | 210 | 1 | 21.0 | $61.95 |
| Medium | Dallas to Nashville | 668 | 3 | 66.9 | $189.63 |
| Medium | Chicago to Atlanta | 719 | 3 | 71.9 | $215.28 |
| Medium | Denver to Dallas | 799 | 4 | 79.9 | $237.90 |
| Long | Los Angeles to New York | 2,796 | 15 | 279.6 | $879.33 |
| Long | Seattle to Miami | 3,334 | 16 | 333.4 | $1,021.37 |

Speed: a new trip takes about 0.7 to 2 seconds, almost all of it OpenRouteService and geocoding. Picking the stops themselves takes a millisecond or less (the optimizer plans 1,500 stations over 3,000 miles in about 0.2 ms). The same trip again is answered from cache in about 1 ms with no outside requests.

## The fuel data

The file from the client has 8,151 rows and no coordinates, so every station was geocoded once with Nominatim and the result is saved in `data/fuel-stations-geocoded.csv`. That file is committed, so you do not need to run the geocoder. To redo it, run `python scripts/geocode_stations.py` (it takes a while, Nominatim allows one request per second).

For each row it first looks for the station by name and city, and falls back to the city alone. Results that are only a state or a county are rejected.

Some stations appear more than once with different prices. Each row is treated as its own offer, and the cheaper one wins.

## Known limitations

- **About half of the stations are placed at the centre of their town**, not at the exact address, because the file only has a city. Their pin and their `off_route_miles` are approximate. The response marks them with `location_precision: "town centre"` and the page says so.
- **36 rows could not be located** and are left out.
- **Some stretches have no stations in the file.** For example, there are none in California along the road from Los Angeles to Las Vegas, so that trip's first fill-up is priced at a station in Nevada about 240 miles ahead. The note in the response says this when it happens.
- **Detours are estimated, not routed** (see above).
- **A stop can be made for a small saving on a short trip.** The rules work per gallon, so a 210 mile trip can still stop once to save about 44 cents.
- **Prices are the snapshot in the file**, not live prices.
- **The cache lives in the memory of one process.** It is reset when the server restarts, and every worker has its own. For production this should be Redis (change `CACHES` in `config/settings.py`).
- **USA only.** Coordinates outside the USA are rejected.

## Project layout

```
config/                  Django settings and urls
data/                    the client file and the geocoded copy
scripts/                 geocode_stations.py
spotter/
  views.py, urls.py      the API and the map page
  serializers.py         request validation
  index.html             the map page (Leaflet)
  services/
    planner.py           ties it together, caching, the response
    optimizer.py         the fuel purchase planning
    corridor.py          stations along the route (KD-tree)
    ors.py               OpenRouteService client
    locations.py         start and finish lookup
    stations.py          the in-memory station data
  tests/                 100+ tests, nothing outside is called
```