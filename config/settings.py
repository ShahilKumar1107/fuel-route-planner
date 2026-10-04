"""
Django settings for the Fuel Route Planner API.

Everything environment-specific is read from environment variables so the same
code runs locally, in CI and in production without edits.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-change-me-in-production",
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.gzip.GZipMiddleware",  # route geometry compresses ~5x
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "planner.middleware.ResponseTimeMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

# Cache = route + plan results, so repeat trips need ZERO routing calls.
# * REDIS_URL set   -> Redis (shared by every worker and host; recommended in production)
# * otherwise       -> file cache under .cache/ (shared by all workers on one machine,
#                      survives restarts, needs no extra service)
REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": 60 * 60 * 24,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": os.environ.get("CACHE_DIR", str(BASE_DIR / ".cache")),
            "TIMEOUT": 60 * 60 * 24,
            "OPTIONS": {"MAX_ENTRIES": 5000},
        }
    }
if "test" in sys.argv[1:2]:  # isolated, in-memory cache for the test suite
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------- #
# Django REST Framework
# --------------------------------------------------------------------------- #
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # Public, read-only computation API: no auth/session work per request.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": os.environ.get("API_RATE_LIMIT", "600/min")},
    "EXCEPTION_HANDLER": "planner.exceptions.api_exception_handler",
}

# --------------------------------------------------------------------------- #
# Fuel planner
# --------------------------------------------------------------------------- #
FUEL_PLANNER = {
    # Vehicle defaults (overridable per request).
    "VEHICLE_RANGE_MILES": env_float("VEHICLE_RANGE_MILES", 500.0),
    "VEHICLE_MPG": env_float("VEHICLE_MPG", 10.0),
    # Dollars a fuel stop must save to be worth the driver's time (0 = pure cheapest).
    "STOP_PENALTY_DOLLARS": env_float("STOP_PENALTY_DOLLARS", 10.0),
    # A station counts as "on the route" if it is within this many miles of it.
    # Stations are geocoded to city centroids, so this must absorb the distance
    # between a town's centre and the interstate exit.
    "CORRIDOR_MILES": env_float("CORRIDOR_MILES", 10.0),
    # Free, key-less OSRM server. Swap for a self-hosted OSRM in production.
    "OSRM_BASE_URL": os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org"),
    "OSRM_TIMEOUT_SECONDS": env_float("OSRM_TIMEOUT_SECONDS", 15.0),
    "ROUTE_CACHE_SECONDS": int(env_float("ROUTE_CACHE_SECONDS", 60 * 60 * 24 * 7)),
    # Spacing of the line returned to clients (full geometry is used internally).
    "OUTPUT_GEOMETRY_SPACING_MILES": env_float("OUTPUT_GEOMETRY_SPACING_MILES", 0.5),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {"planner": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")}},
}
