"""Orchestrates one trip plan: geocode -> route (1 API call max) -> match stations -> optimise.

Every step after the routing call is in-memory and vectorised; for a
coast-to-coast trip the local compute is a few tens of milliseconds.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.core.cache import cache
from scipy.spatial import cKDTree

from .geo import chord_to_miles, cumulative_miles, densify, miles_to_chord, resample, resample_indices, to_unit_xyz
from .geocoding import Place, get_gazetteer
from .optimizer import plan_fuel_stops, plan_fuel_stops_dp
from .routing import get_route
from .station_index import current_version, get_station_index

# Route vertices are densified to this spacing before matching stations, so
# "nearest vertex" is within half a mile of "nearest point on the line".
MATCH_SPACING_MILES = 1.0


@dataclass(frozen=True)
class TripRequest:
    start: str
    finish: str
    range_miles: float
    mpg: float
    start_fuel_gallons: float | None = None  # None = full tank
    stop_penalty: float = 10.0  # $ a stop must save to be worth making
    include_geometry: bool = True


def plan_trip(req: TripRequest) -> dict:
    """Plan a trip, serving identical plans from cache (keyed on resolved coordinates)."""
    t0 = time.perf_counter()
    gazetteer = get_gazetteer()
    start = gazetteer.geocode(req.start)
    finish = gazetteer.geocode(req.finish)

    key = _plan_cache_key(start, finish, req)
    cached = cache.get(key)
    if cached is not None:
        result = cached  # cache backends unpickle a fresh object, safe to mutate
        result["summary"]["start"]["query"] = req.start
        result["summary"]["finish"]["query"] = req.finish
        result["meta"].update(
            routing_api_calls=0, route_from_cache=True, plan_from_cache=True, timings={"total_ms": _ms(t0)}
        )
        return result

    result = _compute_plan(req, start, finish, t0)
    cache.set(key, result, timeout=settings.FUEL_PLANNER["ROUTE_CACHE_SECONDS"])
    return result


def _plan_cache_key(start: Place, finish: Place, req: TripRequest) -> str:
    raw = repr(
        (
            round(start.latitude, 4),
            round(start.longitude, 4),
            round(finish.latitude, 4),
            round(finish.longitude, 4),
            req.range_miles,
            req.mpg,
            req.start_fuel_gallons,
            req.stop_penalty,
            req.include_geometry,
            current_version(),
            settings.FUEL_PLANNER["CORRIDOR_MILES"],
        )
    )
    return "planner:plan:v1:" + hashlib.sha1(raw.encode()).hexdigest()


def _compute_plan(req: TripRequest, start: Place, finish: Place, t0: float) -> dict:
    cfg = settings.FUEL_PLANNER
    timings: dict[str, float] = {"geocode_ms": _ms(t0)}

    # 2. Route: 1 call to OSRM, or 0 if cached.
    t = time.perf_counter()
    routed = get_route((start.latitude, start.longitude), (finish.latitude, finish.longitude))
    route = routed.route
    timings["routing_ms"] = _ms(t)

    # 3. Put candidate stations on the route.
    t = time.perf_counter()
    lats, lons = route.lats, route.lons
    cum = cumulative_miles(lats, lons)
    # Our haversine length differs slightly from OSRM's road distance; scale
    # mile markers so they agree with the reported trip distance.
    scale = route.distance_miles / cum[-1] if cum[-1] > 0 else 1.0
    candidates = _stations_along_route(lats, lons, cum, scale, cfg["CORRIDOR_MILES"])
    timings["station_matching_ms"] = _ms(t)

    # 4. Optimise.
    t = time.perf_counter()
    tank_gallons = req.range_miles / req.mpg
    start_gallons = tank_gallons if req.start_fuel_gallons is None else min(req.start_fuel_gallons, tank_gallons)
    index = get_station_index()
    common = dict(
        positions=candidates["position"],
        prices=index.price[candidates["station"]],
        route_miles=route.distance_miles,
        range_miles=req.range_miles,
        start_fuel_miles=start_gallons * req.mpg,
        mpg=req.mpg,
        tie_breaker=candidates["offset"],
    )
    if req.stop_penalty > 0:
        plan = plan_fuel_stops_dp(stop_penalty=req.stop_penalty, **common)
        algorithm = "dp_with_stop_penalty"
    else:
        plan = plan_fuel_stops(**common)
        algorithm = "sliding_window_min_cost"
    timings["optimization_ms"] = _ms(t)

    # 5. Shape the response.
    t = time.perf_counter()
    stops = []
    for n, stop in enumerate(plan.stops, start=1):
        c = stop.station
        i = int(candidates["station"][c])
        price = float(index.price[i])
        gallons = stop.miles_of_fuel / req.mpg
        stops.append(
            {
                "stop_number": n,
                "opis_id": int(index.ids[i]),
                **index.meta[i],
                "lat": round(float(index.lat[i]), 5),
                "lon": round(float(index.lon[i]), 5),
                "price_per_gallon": round(price, 3),
                "mile_marker": round(stop.mile_marker, 1),
                "distance_from_route_miles": round(float(candidates["offset"][c]), 1),
                "fuel_on_arrival_gallons": round(float(stop.fuel_on_arrival_miles) / req.mpg, 2),
                "gallons_purchased": round(gallons, 2),
                "cost": round(gallons * price, 2),
            }
        )

    gallons_bought = float(plan.total_miles_purchased) / req.mpg
    summary = {
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "total_distance_miles": round(route.distance_miles, 1),
        "estimated_drive_time_hours": round(route.duration_seconds / 3600, 2),
        "total_fuel_cost": round(float(plan.total_cost), 2),
        "total_gallons_purchased": round(gallons_bought, 2),
        "average_price_per_gallon": round(float(plan.total_cost) / gallons_bought, 3) if gallons_bought else None,
        "fuel_used_gallons": round(route.distance_miles / req.mpg, 2),
        "number_of_fuel_stops": len(stops),
        "fuel_at_destination_gallons": round(float(plan.fuel_at_destination_miles) / req.mpg, 2),
        "candidate_stations_on_route": int(len(candidates["station"])),
        "vehicle": {
            "range_miles": req.range_miles,
            "mpg": req.mpg,
            "tank_gallons": round(tank_gallons, 2),
            "start_fuel_gallons": round(start_gallons, 2),
        },
        "stop_penalty_dollars": req.stop_penalty,
    }

    result = {"summary": summary, "fuel_stops": stops}
    if req.include_geometry:
        out_lats, out_lons = resample(lats, lons, cum, cfg["OUTPUT_GEOMETRY_SPACING_MILES"])
        result["map"] = _feature_collection(out_lats, out_lons, start, finish, stops, summary)
    timings["serialization_ms"] = _ms(t)
    timings["total_ms"] = _ms(t0)

    result["meta"] = {
        "routing_provider": "OSRM (router.project-osrm.org)",
        "routing_api_calls": routed.api_calls,
        "route_from_cache": routed.from_cache,
        "plan_from_cache": False,
        "optimizer": algorithm,
        "timings": timings,
        "assumptions": [
            f"Vehicle starts with {start_gallons:g} gal; tank holds {tank_gallons:g} gal "
            f"({req.range_miles:g} mi at {req.mpg:g} mpg).",
            "total_fuel_cost is the money spent at fuel stops along the route.",
            f"A stop is only added if it saves more than ${req.stop_penalty:g} "
            "(stop_penalty=0 gives the pure cheapest plan).",
            "Station locations are geocoded to city level; stations within "
            f"{cfg['CORRIDOR_MILES']:g} miles of the route are considered.",
        ],
    }
    return result


def _stations_along_route(lats, lons, cum, scale, corridor_miles) -> dict[str, np.ndarray]:
    """Match stations to the route with a KD-tree on unit-sphere vectors.

    Returns, for each station within ``corridor_miles`` of the route, its
    index into the station table, its mile marker, and its distance off-route.
    """
    index = get_station_index()
    empty = {"station": np.empty(0, dtype=np.int64), "position": np.empty(0), "offset": np.empty(0)}
    if not len(index):
        return empty

    # Cheap bounding-box pre-filter: typically drops >80% of the 6.6k stations.
    lat_pad = corridor_miles / 69.0
    lon_pad = corridor_miles / (69.0 * max(np.cos(np.radians(np.abs(lats).max())), 0.05))
    pool = index.in_bbox(lats.min() - lat_pad, lats.max() + lat_pad, lons.min() - lon_pad, lons.max() + lon_pad)
    if not len(pool):
        return empty

    # Thin the (very dense) road geometry, then fill any long straight gaps, so
    # the KD-tree holds ~2 vertices per mile regardless of the source density.
    keep = resample_indices(cum, MATCH_SPACING_MILES / 2)
    d_lats, d_lons, d_cum = densify(lats[keep], lons[keep], cum[keep], MATCH_SPACING_MILES)
    tree = cKDTree(to_unit_xyz(d_lats, d_lons))
    chord, vertex = tree.query(index.xyz[pool], k=1, distance_upper_bound=miles_to_chord(corridor_miles))
    hit = np.isfinite(chord)
    return {
        "station": pool[hit],
        "position": d_cum[vertex[hit]] * scale,
        "offset": chord_to_miles(chord[hit]),
    }


def _feature_collection(lats, lons, start: Place, finish: Place, stops: list[dict], summary: dict) -> dict:
    """GeoJSON the client can drop straight onto any map (Leaflet, Mapbox, geojson.io)."""
    line = np.column_stack((np.round(lons, 5), np.round(lats, 5))).tolist()
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": line},
            "properties": {
                "kind": "route",
                "distance_miles": summary["total_distance_miles"],
                "total_fuel_cost": summary["total_fuel_cost"],
            },
        },
        _point(start.longitude, start.latitude, {"kind": "start", "name": start.name}),
        _point(finish.longitude, finish.latitude, {"kind": "finish", "name": finish.name}),
    ]
    for s in stops:
        features.append(
            _point(
                s["lon"],
                s["lat"],
                {
                    "kind": "fuel_stop",
                    "stop_number": s["stop_number"],
                    "name": s["name"],
                    "price_per_gallon": s["price_per_gallon"],
                    "gallons_purchased": s["gallons_purchased"],
                    "cost": s["cost"],
                    "mile_marker": s["mile_marker"],
                },
            )
        )
    return {"type": "FeatureCollection", "features": features}


def _point(lon: float, lat: float, props: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
        "properties": props,
    }


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 2)
