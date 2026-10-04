# Fuel Route Planner API

A Django REST API that takes a start and finish location in the USA and returns:

- the driving route, as GeoJSON plus an interactive map page,
- the most cost-effective places to refuel along it (the vehicle has a 500-mile range, so long trips need several stops),
- the total money spent on fuel at 10 miles per gallon.

**Headline numbers** for a coast-to-coast trip (New York → Los Angeles, 2,800 miles, 442 candidate stations):

| | |
|---|---|
| External routing API calls | **1** for a new trip, **0** for a repeat |
| Latency, new trip | routing API round-trip + **~15 ms** of our own compute |
| Latency, repeat trip | **~3 ms** end to end |
| Throughput (4 gunicorn workers, laptop-class CPU) | **~430 requests/s**, p95 ≈ 35 ms |
| Optimality | Verified against an exact LP solver on 600 random instances and brute force on 150 |

---

## Quick start

Requires **Python 3.12+** (Django 6.1).

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_fuel_stations                     # imports + geocodes the CSV (~1 s, no network)
python manage.py runserver
```

Then open one of these:

- JSON: <http://127.0.0.1:8000/api/v1/route-plan/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>
- Map: <http://127.0.0.1:8000/api/v1/route-plan/map/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA>

Run the tests with `python manage.py test planner` (32 tests, about 12 s).

For production, run `gunicorn config.wsgi -w 4 --preload` and set `REDIS_URL` if you run more than one server.

---

## API

### `GET /api/v1/route-plan/` or `POST /api/v1/route-plan/`

GET takes query parameters. POST takes the same fields as a JSON body.

| Field | Required | Default | Description |
|---|---|---|---|
| `start` | yes | | `"City, ST"`, `"City, State"`, `"City ST 12345"` or `"lat,lon"` |
| `finish` | yes | | same formats as `start` |
| `range_miles` | no | `500` | distance on a full tank |
| `mpg` | no | `10` | miles per gallon |
| `start_fuel_gallons` | no | full tank | fuel in the tank at departure (`0` means every gallon of the trip is bought on the route) |
| `stop_penalty` | no | `10` | dollars a stop must save to be worth making (see the optimiser section). `0` gives the strictly cheapest plan |
| `include_geometry` | no | `true` | set `false` to drop the GeoJSON line and get a ~2 KB response |

Example (POST):

```json
{ "start": "New York, NY", "finish": "Los Angeles, CA" }
```

Response, trimmed:

```jsonc
{
  "summary": {
    "start":  { "query": "New York, NY", "name": "New York, NY", "lat": 40.74838, "lon": -73.996705 },
    "finish": { "query": "Los Angeles, CA", "name": "Los Angeles, CA", "lat": 33.973093, "lon": -118.247896 },
    "total_distance_miles": 2800.6,
    "estimated_drive_time_hours": 49.88,
    "total_fuel_cost": 709.41,              // money spent at the stops below
    "total_gallons_purchased": 230.06,
    "average_price_per_gallon": 3.084,
    "fuel_used_gallons": 280.06,
    "number_of_fuel_stops": 5,
    "fuel_at_destination_gallons": 0.0,
    "candidate_stations_on_route": 442,
    "vehicle": { "range_miles": 500, "mpg": 10, "tank_gallons": 50, "start_fuel_gallons": 50 },
    "stop_penalty_dollars": 10
  },
  "fuel_stops": [
    {
      "stop_number": 1, "opis_id": 72445, "name": "SHEETZ #639",
      "address": "I-80 Exit 223", "city": "Youngstown", "state": "OH",
      "lat": 41.0986, "lon": -80.6474,
      "price_per_gallon": 3.059, "mile_marker": 379.6, "distance_from_route_miles": 1.5,
      "fuel_on_arrival_gallons": 12.04, "gallons_purchased": 35.24, "cost": 107.81
    }
    // ... 4 more
  ],
  "map": { "type": "FeatureCollection", "features": [ /* route LineString, start, finish, one Point per stop */ ] },
  "map_url": "http://127.0.0.1:8000/api/v1/route-plan/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&...",
  "meta": {
    "routing_api_calls": 1, "route_from_cache": false, "plan_from_cache": false,
    "optimizer": "dp_with_stop_penalty",
    "timings": { "geocode_ms": 0.2, "routing_ms": 405.2, "station_matching_ms": 4.2, "optimization_ms": 6.3, "total_ms": 416.4 },
    "assumptions": [ "..." ]
  }
}
```

The `map` object is standard GeoJSON. You can paste it into <https://geojson.io> or drop it straight into Leaflet or Mapbox. `map_url` opens a ready-made Leaflet page showing the same plan; it reuses the cached result, so it makes no extra API call.

Every response also carries an `X-Response-Time-ms` header.

### Errors

Every error has the same shape: `{"error": {"code": "...", "message": "..."}}`.

| Status | Code | When |
|---|---|---|
| 400 | `invalid_request` | missing or invalid fields (field details are in `fields`) |
| 400 | `location_not_found` | unknown or non-US place. Typos get a hint, e.g. *"Did you mean: Chicago, IL?"* |
| 422 | `route_not_found` | no drivable route, e.g. to Hawaii |
| 422 | `no_feasible_fuel_plan` | some stretch of the route has no station within the vehicle's range |
| 503 | `routing_service_unavailable` | the routing provider is down or timed out |

Other endpoints: `GET /api/v1/health/` and `GET /api/v1/` (index of endpoints).

---

## How it works

```
request ─► geocode (offline, µs) ─► plan cache? ──yes──► respond (~3 ms)
                                        │ no
                                        ▼
                         route cache? ──no──► OSRM: 1 HTTP call ─┐
                                        │ yes                    │
                                        ▼                        ▼
             match stations to route (KD-tree) ─► optimise ─► respond + cache
```

### 1. One routing call per request, at most

- **Routing:** the free public [OSRM](https://project-osrm.org/) server. It needs no API key and returns the full route geometry and the distance in one call. I ask for `polyline6` geometry (5–8× smaller than GeoJSON) and turn off turn-by-turn steps.
- **Geocoding is offline.** Start and finish are resolved against a bundled gazetteer of 29.7k US cities ([kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database), MIT). Most solutions spend two extra API calls here; this one spends none, and an invalid city fails fast without touching the network.
- **Two caches.** One holds routes (keyed on coordinates rounded to about 11 m). The other holds finished plans (keyed on coordinates, vehicle settings and price-data version). Changing `mpg` or `stop_penalty` reuses the cached route. The default is a file cache that all gunicorn workers share and that survives restarts; with `REDIS_URL` set it uses Redis.

### 2. Fuel price data

The CSV has 8,151 rows but **no coordinates**. The importer (`python manage.py load_fuel_stations`) does the following:

- drops 620 Canadian rows (ON, AB, BC…), since the route is US-only;
- merges duplicate OPIS IDs, keeping the lowest posted price, which leaves **6,626 unique US stations**;
- geocodes every station offline to its city centre. 99.8% match directly; the rest are covered by a small alias table;
- replaces the table in one transaction and bumps a cache version, so every worker reloads prices on its next request.

At request time all stations live in memory as numpy arrays, loaded once per process. No SQL runs per request.

### 3. Finding stations along the route

1. A bounding-box filter cuts the 6.6k stations down to those near the route.
2. The route is resampled to two vertices per mile and loaded into a `scipy` KD-tree built on 3-D unit-sphere vectors. Chord distance on the sphere is monotonic in great-circle distance, so lookups are exact with no map-projection error.
3. One vectorised query returns, for every station, its distance from the route and its **mile marker** along it. Anything within 10 miles is a candidate. That radius allows for the gap between a town's centre and the interstate exit.

### 4. The optimiser: provably cheapest, not a greedy guess

The problem is the classic *gas-station problem*. The tank holds R = 500 miles of fuel, partial fills are allowed, and the goal is to minimise money spent.

**Key insight.** Fuel bought at a station at mile *d* can only be burned on miles in `[d, d + R]`: you can't use it before you buy it, and a full tank is empty R miles later. Therefore:

- *Lower bound:* every mile *x* of the trip costs at least the cheapest price among stations in the window `[x − R, x]`.
- *Achievable:* buying each mile's fuel at the cheapest station in its window never overfills the tank and never runs it dry.

So the optimal cost is the integral of a **sliding-window minimum** of price along the route. That minimum only changes at a station (`d`) or where a station's reach ends (`d + R`). A heap sweep over those breakpoints solves the problem exactly in **O(n log n)**, well under a millisecond. Each station ends up owning one contiguous stretch of miles, and that stretch becomes one fuel stop.

**The practical refinement (`stop_penalty`).** The strictly cheapest plan for NYC → LA has 17 stops, some buying 1.5 gallons to save a few cents. No driver would do that. Each stop therefore carries a cost (default **$10**, roughly 15 minutes of a driver's time), and the API minimises **fuel cost + $10 × stops**. That is a fixed-charge problem, solved exactly by a dynamic program over (station, fuel level):

- Fuel is discretised in 1-mile steps.
- The "how much to buy" choice at each station reduces to a running prefix minimum, so each station costs O(tank size) of vectorised numpy work. A coast-to-coast trip solves in about 6 ms.
- The DP picks *which* stations to use. The exact continuous algorithm then computes *how much* to buy at each one, so the reported gallons and dollars carry no rounding error.

For NYC → LA the result is 5 stops (the minimum possible) for $709.41, versus 17 stops for $694.70. Set `stop_penalty=0` to get the strictly cheapest plan.

**How it is verified.** `planner/tests/test_optimizer.py` compares the exact algorithm against a linear-programming formulation (`scipy.optimize.linprog`) on 600 random trips. It also checks the DP against brute force over every subset of stations.

### 5. Assumptions

| Assumption | Why |
|---|---|
| The truck starts with a **full tank** (overridable with `start_fuel_gallons`) | The realistic default. `start_fuel_gallons=0` makes every gallon of the trip a purchase |
| `total_fuel_cost` = money spent at stops on this trip | That is exactly "money spent on fuel" |
| The truck may arrive at the destination with an empty tank | Standard for this problem; leftover fuel is reported anyway |
| Stations are placed at their city centre | The CSV has no coordinates, and offline geocoding keeps the API at one call. In production I'd batch-geocode the addresses once and store exact coordinates (the model has a `geocode_precision` field for that) |
| Detour distance to a station isn't charged | Stations are within 10 miles of the route; `distance_from_route_miles` is reported and used as a tie-breaker |

---

## Project layout

```
config/                     settings (env-driven), urls, wsgi/asgi (with warm-up)
planner/
  models.py                 FuelStation
  views.py, serializers.py  DRF endpoint (GET + POST), map page, health
  exceptions.py             typed domain errors -> consistent JSON errors
  services/
    geocoding.py            offline gazetteer: "City, ST" / lat,lon -> coordinates
    routing.py              OSRM client: pooled session, timeouts, route cache
    station_index.py        in-memory numpy station table, version-aware reload
    geo.py                  vectorised haversine, polyline decoding, resampling
    optimizer.py            sliding-window exact optimiser + stop-penalty DP
    planner.py              orchestration, plan cache, response shaping
  management/commands/load_fuel_stations.py
  templates/planner/map.html   Leaflet map
  data/                     fuel price CSV + US cities gazetteer
  tests/                    optimiser oracles, geocoding, API (OSRM mocked with a recorded response)
postman_collection.json     ready-to-import Postman requests
```

## Configuration

All settings are optional environment variables; see `.env.example`. The main ones are `REDIS_URL`, `OSRM_BASE_URL` (point it at a self-hosted OSRM for production volume), `VEHICLE_RANGE_MILES`, `VEHICLE_MPG`, `STOP_PENALTY_DOLLARS`, `CORRIDOR_MILES` and `API_RATE_LIMIT`.
