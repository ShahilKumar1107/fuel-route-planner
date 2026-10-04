"""Vectorised geometry helpers (numpy only, no per-point Python loops in hot paths)."""

from __future__ import annotations

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles. Works on scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def to_unit_xyz(lats, lons) -> np.ndarray:
    """Lat/lon (degrees) -> points on the unit sphere, shape (n, 3).

    Euclidean (chord) distance between unit vectors is monotonic with
    great-circle distance, so a plain KD-tree gives exact nearest neighbours
    without any projection distortion.
    """
    lat = np.radians(np.asarray(lats, dtype=np.float64))
    lon = np.radians(np.asarray(lons, dtype=np.float64))
    cos_lat = np.cos(lat)
    return np.column_stack((cos_lat * np.cos(lon), cos_lat * np.sin(lon), np.sin(lat)))


def miles_to_chord(miles: float) -> float:
    """Great-circle miles -> chord length on the unit sphere."""
    return 2.0 * np.sin(miles / (2.0 * EARTH_RADIUS_MILES))


def chord_to_miles(chord):
    return 2.0 * EARTH_RADIUS_MILES * np.arcsin(np.clip(np.asarray(chord) / 2.0, 0.0, 1.0))


def decode_polyline(encoded: str, precision: int = 6) -> np.ndarray:
    """Decode a Google encoded polyline into an (n, 2) array of [lat, lon].

    The varint parsing is inherently sequential; the cumulative sum and
    scaling are vectorised.
    """
    deltas = []
    append = deltas.append
    result = shift = 0
    for char in encoded:
        b = ord(char) - 63
        result |= (b & 0x1F) << shift
        shift += 5
        if b < 0x20:
            append(~(result >> 1) if result & 1 else result >> 1)
            result = shift = 0
    coords = np.cumsum(np.asarray(deltas, dtype=np.int64).reshape(-1, 2), axis=0)
    return coords / float(10**precision)


def cumulative_miles(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Distance from the first vertex to every vertex, in miles."""
    if len(lats) < 2:
        return np.zeros(len(lats))
    seg = haversine_miles(lats[:-1], lons[:-1], lats[1:], lons[1:])
    return np.concatenate(([0.0], np.cumsum(seg)))


def densify(lats: np.ndarray, lons: np.ndarray, cum: np.ndarray, max_spacing_miles: float):
    """Insert interpolated vertices so no gap exceeds ``max_spacing_miles``.

    Long straight interstate segments can have vertices many miles apart;
    densifying makes "nearest vertex" a good proxy for "nearest point on line".
    """
    if len(cum) < 2:
        return lats, lons, cum
    total = cum[-1]
    samples = np.unique(np.concatenate((cum, np.arange(0.0, total, max_spacing_miles), [total])))
    return np.interp(samples, cum, lats), np.interp(samples, cum, lons), samples


def resample(lats: np.ndarray, lons: np.ndarray, cum: np.ndarray, spacing_miles: float):
    """Down-sample a line to roughly one vertex per ``spacing_miles`` (keeps both ends).

    Real OSRM geometry has a vertex every ~100 m; that density is wasted on
    both station matching and the client payload.
    """
    idx = resample_indices(cum, spacing_miles)
    return lats[idx], lons[idx]


def resample_indices(cum: np.ndarray, spacing_miles: float) -> np.ndarray:
    """Indices of vertices roughly ``spacing_miles`` apart (first and last always kept)."""
    if len(cum) < 3 or cum[-1] <= spacing_miles:
        return np.arange(len(cum))
    targets = np.arange(0.0, cum[-1], spacing_miles)
    return np.unique(np.concatenate((np.searchsorted(cum, targets), [len(cum) - 1])))
