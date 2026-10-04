"""Driving route from the free public OSRM server: ONE HTTP call per new route.

* ``overview=full`` returns the whole geometry in that single response.
* ``geometries=polyline6`` is ~5-8x smaller on the wire than GeoJSON.
* ``steps=false`` / ``annotations=false`` skip turn-by-turn data we don't use.
* Results are cached (keyed on ~11 m-rounded coordinates), so a repeated or
  nearby-identical request makes ZERO external calls.
* A pooled ``requests.Session`` reuses the TLS connection between requests.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter

from ..exceptions import RouteNotFound, RoutingServiceUnavailable
from .geo import METERS_PER_MILE, decode_polyline

logger = logging.getLogger(__name__)

_session: requests.Session | None = None
_session_lock = threading.Lock()


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                session = requests.Session()
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                session.headers["User-Agent"] = "fuel-route-planner/1.0 (assessment project)"
                _session = session
    return _session


@dataclass(frozen=True)
class Route:
    lats: np.ndarray
    lons: np.ndarray
    distance_miles: float
    duration_seconds: float


@dataclass(frozen=True)
class RouteResult:
    route: Route
    from_cache: bool
    api_calls: int


def _cache_key(start: tuple[float, float], end: tuple[float, float]) -> str:
    return "planner:route:v1:{:.4f},{:.4f}:{:.4f},{:.4f}".format(*start, *end)


def get_route(start: tuple[float, float], end: tuple[float, float]) -> RouteResult:
    """``start``/``end`` are (lat, lon). Returns the route and how it was obtained."""
    key = _cache_key(start, end)
    cached = cache.get(key)
    if cached is not None:
        return RouteResult(route=cached, from_cache=True, api_calls=0)

    route = _fetch_osrm(start, end)
    cache.set(key, route, timeout=settings.FUEL_PLANNER["ROUTE_CACHE_SECONDS"])
    return RouteResult(route=route, from_cache=False, api_calls=1)


def _fetch_osrm(start: tuple[float, float], end: tuple[float, float]) -> Route:
    cfg = settings.FUEL_PLANNER
    # OSRM expects lon,lat order.
    url = "{base}/route/v1/driving/{:.6f},{:.6f};{:.6f},{:.6f}".format(
        start[1], start[0], end[1], end[0], base=cfg["OSRM_BASE_URL"].rstrip("/")
    )
    params = {
        "overview": "full",
        "geometries": "polyline6",
        "steps": "false",
        "annotations": "false",
        "alternatives": "false",
    }
    try:
        response = _get_session().get(url, params=params, timeout=cfg["OSRM_TIMEOUT_SECONDS"])
    except requests.RequestException as exc:
        logger.warning("OSRM request failed: %s", exc)
        raise RoutingServiceUnavailable("The routing service could not be reached. Please retry.") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    code = payload.get("code")
    if response.status_code == 400 or code in {"NoRoute", "NoSegment", "InvalidValue", "InvalidQuery"}:
        raise RouteNotFound(payload.get("message") or "No drivable route exists between these locations.")
    if response.status_code != 200 or code != "Ok" or not payload.get("routes"):
        logger.warning("OSRM unexpected response %s: %s", response.status_code, str(payload)[:300])
        raise RoutingServiceUnavailable("The routing service returned an unexpected response. Please retry.")

    best = payload["routes"][0]
    coords = decode_polyline(best["geometry"], precision=6)
    if len(coords) < 2:
        raise RouteNotFound("The routing service returned an empty route.")
    return Route(
        lats=np.ascontiguousarray(coords[:, 0]),
        lons=np.ascontiguousarray(coords[:, 1]),
        distance_miles=float(best["distance"]) / METERS_PER_MILE,
        duration_seconds=float(best["duration"]),
    )
