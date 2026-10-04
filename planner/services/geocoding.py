"""Offline US geocoder.

Start/finish locations and all truck stops are resolved against a bundled
gazetteer of ~29.7k US cities (kelvins/US-Cities-Database, MIT licensed).

Why offline: the brief says to call the map/routing API as little as possible.
Resolving places locally means a request needs exactly ONE external call (the
route), and geocoding costs microseconds instead of a network round-trip.
"""

from __future__ import annotations

import csv
import difflib
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from ..exceptions import LocationNotFound

GAZETTEER_PATH = Path(__file__).resolve().parent.parent / "data" / "us_cities.csv"

# Bounding box loosely covering the 50 states (incl. Alaska and Hawaii).
US_LAT_RANGE = (18.0, 72.0)
US_LON_RANGE = (-180.0, -65.0)

# Places the gazetteer lacks (they appear in the fuel-price file) and common spellings.
EXTRA_PLACES = {
    ("port wentworth", "GA"): (32.1488, -81.1632),
    ("university park", "IL"): (41.4431, -87.6836),
    ("evergreen", "AL"): (31.4335, -86.9569),
    ("henrico", "VA"): (37.5059, -77.3324),
    ("elizabethport", "NJ"): (40.6487, -74.1835),
}
ALIASES = {"brookpark": "brook park"}

_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_COUNTRY_RE = re.compile(r",?\s*\b(usa|u\.s\.a\.?|us|united states(?: of america)?)\s*$", re.IGNORECASE)


def normalize_city(name: str) -> str:
    """Canonical form so 'St. Louis', 'Saint Louis' and 'ST LOUIS' all match."""
    s = name.strip().lower()
    s = re.sub(r"[.'’]", "", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bfort\b", "ft", s)
    s = re.sub(r"\bmount\b", "mt", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return ALIASES.get(s, s)


@dataclass(frozen=True, slots=True)
class Place:
    query: str
    name: str
    latitude: float
    longitude: float

    def as_dict(self) -> dict:
        return {"query": self.query, "name": self.name, "lat": self.latitude, "lon": self.longitude}


class Gazetteer:
    def __init__(self, path: Path = GAZETTEER_PATH):
        self._index: dict[tuple[str, str], tuple[float, float, str]] = {}
        self._by_state: dict[str, list[str]] = {}
        self.state_names: dict[str, str] = {}  # "illinois" -> "IL"
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                state = row["STATE_CODE"].strip().upper()
                key = (normalize_city(row["CITY"]), state)
                self.state_names[row["STATE_NAME"].strip().lower()] = state
                if key not in self._index:  # first occurrence wins for duplicate names
                    self._index[key] = (float(row["LATITUDE"]), float(row["LONGITUDE"]), row["CITY"].strip())
        for (city, state), (lat, lon) in EXTRA_PLACES.items():
            self._index.setdefault((city, state), (lat, lon, city.title()))
        for city, state in self._index:
            self._by_state.setdefault(state, []).append(city)
        self.states = frozenset(self._by_state)

    def lookup(self, city: str, state: str) -> tuple[float, float, str] | None:
        return self._index.get((normalize_city(city), state.strip().upper()))

    def suggestions(self, city: str, state: str, n: int = 3) -> list[str]:
        candidates = self._by_state.get(state, [])
        matches = difflib.get_close_matches(normalize_city(city), candidates, n=n, cutoff=0.75)
        return [f"{self._index[(m, state)][2]}, {state}" for m in matches]

    def resolve_state(self, token: str) -> str | None:
        token = token.strip().lower().rstrip(".")
        if token.upper() in self.states:
            return token.upper()
        return self.state_names.get(token)

    def geocode(self, query: str) -> Place:
        """Resolve "City, ST", "City, State", "City ST" or "lat,lon"."""
        raw = (query or "").strip()
        if not raw:
            raise LocationNotFound("Location must not be empty.")

        match = _COORD_RE.match(raw)
        if match:
            lat, lon = float(match.group(1)), float(match.group(2))
            if not (US_LAT_RANGE[0] <= lat <= US_LAT_RANGE[1] and US_LON_RANGE[0] <= lon <= US_LON_RANGE[1]):
                raise LocationNotFound(f"Coordinates {lat}, {lon} are outside the USA.")
            return Place(raw, f"{lat:.5f}, {lon:.5f}", lat, lon)

        cleaned = _COUNTRY_RE.sub("", _ZIP_RE.sub("", raw)).strip(" ,")
        city, state = self._split_city_state(cleaned)
        if not state:
            raise LocationNotFound(
                f"Could not find a US state in '{raw}'. Use 'City, ST' (e.g. 'Dallas, TX') or 'lat,lon'."
            )
        hit = self.lookup(city, state)
        if hit is None:
            hint = self.suggestions(city, state)
            extra = f" Did you mean: {', '.join(hint)}?" if hint else ""
            raise LocationNotFound(f"Unknown US city '{city.strip()}, {state}'.{extra}")
        lat, lon, display = hit
        return Place(raw, f"{display}, {state}", lat, lon)

    def _split_city_state(self, text: str) -> tuple[str, str | None]:
        if "," in text:
            city, _, state_part = text.rpartition(",")
            return city, self.resolve_state(state_part)
        words = text.split()
        # Try the last 1-3 words as a state ("Santa Fe New Mexico", "Austin TX").
        for n in (1, 2, 3):
            if len(words) > n:
                state = self.resolve_state(" ".join(words[-n:]))
                if state:
                    return " ".join(words[:-n]), state
        return text, None


_gazetteer: Gazetteer | None = None
_lock = threading.Lock()


def get_gazetteer() -> Gazetteer:
    """Process-wide singleton, built lazily on first use (~60 ms, then cached)."""
    global _gazetteer
    if _gazetteer is None:
        with _lock:
            if _gazetteer is None:
                _gazetteer = Gazetteer()
    return _gazetteer
