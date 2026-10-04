"""In-memory, read-optimised view of all fuel stations.

The price table is small (~6.7k US stations) and read on every request, so it
is loaded from the database once per process into contiguous numpy arrays.
Per-request work is then pure vectorised math with no SQL.

Cross-process freshness: ``load_fuel_stations`` bumps a version key in the
shared cache; each worker compares it (one cache read) and reloads if stale.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

import numpy as np
from django.core.cache import cache

from .geo import to_unit_xyz

VERSION_CACHE_KEY = "planner:station-index-version"


@dataclass(frozen=True)
class StationIndex:
    version: str
    ids: np.ndarray  # OPIS ids, int64
    lat: np.ndarray  # float64
    lon: np.ndarray  # float64
    xyz: np.ndarray  # (n, 3) unit vectors for KD-tree queries
    price: np.ndarray  # float64, $/gal
    meta: list[dict]  # name/address/city/state per row, same order

    def __len__(self) -> int:
        return len(self.ids)

    def in_bbox(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> np.ndarray:
        """Indices of stations inside a lat/lon box (cheap pre-filter)."""
        mask = (self.lat >= min_lat) & (self.lat <= max_lat) & (self.lon >= min_lon) & (self.lon <= max_lon)
        return np.flatnonzero(mask)


_index: StationIndex | None = None
_lock = threading.Lock()


def current_version() -> str:
    version = cache.get(VERSION_CACHE_KEY)
    if version is None:
        version = "initial"
        cache.add(VERSION_CACHE_KEY, version, timeout=None)
    return version


def _build(version: str) -> StationIndex:
    from ..models import FuelStation

    rows = list(
        FuelStation.objects.values_list(
            "opis_id", "latitude", "longitude", "retail_price", "name", "address", "city", "state"
        )
    )
    if not rows:
        empty = np.empty(0)
        return StationIndex(version, empty.astype(np.int64), empty, empty, np.empty((0, 3)), empty, [])
    ids, lat, lon, price, names, addresses, cities, states = zip(*rows)
    lat_arr = np.asarray(lat, dtype=np.float64)
    lon_arr = np.asarray(lon, dtype=np.float64)
    meta = [{"name": n, "address": a, "city": c, "state": s} for n, a, c, s in zip(names, addresses, cities, states)]
    return StationIndex(
        version=version,
        ids=np.asarray(ids, dtype=np.int64),
        lat=lat_arr,
        lon=lon_arr,
        xyz=to_unit_xyz(lat_arr, lon_arr),
        price=np.asarray([float(p) for p in price], dtype=np.float64),
        meta=meta,
    )


def get_station_index() -> StationIndex:
    global _index
    version = current_version()
    index = _index
    if index is not None and index.version == version:
        return index
    with _lock:
        if _index is None or _index.version != version:
            _index = _build(version)
        return _index


def invalidate_station_index() -> None:
    """Called after a price import: every worker reloads on its next request."""
    global _index
    cache.set(VERSION_CACHE_KEY, uuid.uuid4().hex, timeout=None)
    with _lock:
        _index = None
