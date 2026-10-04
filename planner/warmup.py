"""Load in-memory data at server start so the first request isn't slow."""

import logging
import time

logger = logging.getLogger(__name__)


def warm() -> None:
    started = time.perf_counter()
    try:
        from scipy.spatial import cKDTree  # noqa: F401  (import cost paid now, not on request 1)

        from .services.geocoding import get_gazetteer
        from .services.station_index import get_station_index

        get_gazetteer()
        stations = len(get_station_index())
    except Exception:  # e.g. migrations not applied yet: don't block startup
        logger.warning("Warm-up skipped", exc_info=True)
        return
    logger.info("Warm-up done: %d stations in memory (%.0f ms)", stations, (time.perf_counter() - started) * 1000)
