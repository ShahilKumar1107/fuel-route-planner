"""Import the fuel-price CSV into the database, geocoding every station offline.

    python manage.py load_fuel_stations                 # bundled CSV
    python manage.py load_fuel_stations path/to/new.csv # refreshed prices

The import is atomic and idempotent: the table is replaced in a single
transaction, so the API never sees a half-loaded price list.
"""

from __future__ import annotations

import csv
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.models import FuelStation
from planner.services.geocoding import get_gazetteer
from planner.services.station_index import invalidate_station_index

DEFAULT_CSV = Path(__file__).resolve().parents[2] / "data" / "fuel_prices.csv"


class Command(BaseCommand):
    help = "Load truck-stop fuel prices from CSV and geocode them (no network calls)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", nargs="?", default=str(DEFAULT_CSV))

    def handle(self, *args, csv_path: str, **options):
        path = Path(csv_path)
        if not path.exists():
            raise CommandError(f"CSV not found: {path}")

        started = time.perf_counter()
        gazetteer = get_gazetteer()
        stations: dict[int, FuelStation] = {}
        skipped_non_us = skipped_bad = 0
        not_geocoded: set[str] = set()
        rows = 0

        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                rows += 1
                state = row["State"].strip().upper()
                if state not in gazetteer.states:  # Canadian provinces etc.
                    skipped_non_us += 1
                    continue
                try:
                    opis_id = int(row["OPIS Truckstop ID"])
                    price = Decimal(row["Retail Price"].strip()).quantize(Decimal("0.0001"))
                    rack_id = int(row["Rack ID"]) if row["Rack ID"].strip() else None
                except (ValueError, InvalidOperation, KeyError):
                    skipped_bad += 1
                    continue
                if price <= 0:
                    skipped_bad += 1
                    continue

                existing = stations.get(opis_id)
                if existing is not None:
                    # Same truck stop listed more than once: keep the cheapest posted price.
                    if price < existing.retail_price:
                        existing.retail_price = price
                        existing.rack_id = rack_id
                    continue

                city = row["City"].strip()
                hit = gazetteer.lookup(city, state)
                if hit is None:
                    not_geocoded.add(f"{city}, {state}")
                    continue
                lat, lon, _ = hit
                stations[opis_id] = FuelStation(
                    opis_id=opis_id,
                    name=row["Truckstop Name"].strip(),
                    address=row["Address"].strip(),
                    city=city.title() if city.isupper() else city,
                    state=state,
                    rack_id=rack_id,
                    retail_price=price,
                    latitude=lat,
                    longitude=lon,
                )

        if not stations:
            raise CommandError("No stations could be loaded from the CSV.")

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(stations.values(), batch_size=1000)
        invalidate_station_index()

        elapsed = time.perf_counter() - started
        self.stdout.write(
            self.style.SUCCESS(f"Loaded {len(stations)} unique US stations from {rows} rows in {elapsed:.2f}s.")
        )
        self.stdout.write(f"  skipped non-US rows: {skipped_non_us}")
        self.stdout.write(f"  skipped invalid rows: {skipped_bad}")
        if not_geocoded:
            self.stdout.write(
                self.style.WARNING(f"  could not geocode {len(not_geocoded)} cities: {sorted(not_geocoded)}")
            )
