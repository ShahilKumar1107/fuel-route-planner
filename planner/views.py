from urllib.parse import urlencode

from django.shortcuts import render
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import PlannerError
from .serializers import RoutePlanRequestSerializer
from .services.planner import TripRequest, plan_trip
from .services.station_index import get_station_index


def _build_trip(data) -> tuple[TripRequest, dict]:
    serializer = RoutePlanRequestSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    v = serializer.validated_data
    trip = TripRequest(
        start=v["start"],
        finish=v["finish"],
        range_miles=v["range_miles"],
        mpg=v["mpg"],
        start_fuel_gallons=v.get("start_fuel_gallons"),
        stop_penalty=v["stop_penalty"],
        include_geometry=v["include_geometry"],
    )
    return trip, v


def _map_url(request, v: dict) -> str:
    params = {"start": v["start"], "finish": v["finish"]}
    for key in ("range_miles", "mpg", "start_fuel_gallons", "stop_penalty"):
        if v.get(key) is not None:
            params[key] = v[key]
    return request.build_absolute_uri("/api/v1/route-plan/map/") + "?" + urlencode(params)


class RoutePlanView(APIView):
    """Plan the cheapest fuel stops between two US locations.

    GET  /api/v1/route-plan/?start=New York, NY&finish=Los Angeles, CA
    POST /api/v1/route-plan/  {"start": "New York, NY", "finish": "Los Angeles, CA"}
    """

    def get(self, request):
        return self._handle(request, request.query_params)

    def post(self, request):
        return self._handle(request, request.data)

    def _handle(self, request, data):
        trip, validated = _build_trip(data)
        result = plan_trip(trip)
        result["map_url"] = _map_url(request, validated)
        return Response(result)


def route_map_view(request):
    """Interactive Leaflet map for the same plan (uses the cached route: no extra API call)."""
    context = {"plan": None, "error": None}
    params = request.GET.copy()
    params["include_geometry"] = "true"
    try:
        trip, _ = _build_trip(params)
        context["plan"] = plan_trip(trip)
    except PlannerError as exc:
        context["error"] = str(exc.detail)
    except Exception as exc:  # validation errors etc.
        detail = getattr(exc, "detail", None)
        context["error"] = str(detail) if detail else "Could not plan this route."
    status = 200 if context["plan"] else 400
    return render(request, "planner/map.html", context, status=status)


@api_view(["GET"])
def health(request):
    return Response({"status": "ok", "stations_loaded": len(get_station_index())})


@api_view(["GET"])
def api_root(request):
    example = {"start": "New York, NY", "finish": "Los Angeles, CA"}
    base = request.build_absolute_uri("/api/v1/route-plan/")
    return Response(
        {
            "service": "Fuel Route Planner",
            "endpoints": {
                "route_plan": base,
                "route_plan_example": base + "?" + urlencode(example),
                "map_example": base + "map/?" + urlencode(example),
                "health": request.build_absolute_uri("/api/v1/health/"),
            },
        }
    )
