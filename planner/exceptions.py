"""Domain errors and a DRF exception handler that renders them consistently."""

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler


class PlannerError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "planner_error"
    default_detail = "The route could not be planned."


class LocationNotFound(PlannerError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "location_not_found"


class RouteNotFound(PlannerError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "route_not_found"


class NoFeasibleFuelPlan(PlannerError):
    """A stretch of the route is longer than the vehicle range with no station in it."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "no_feasible_fuel_plan"


class RoutingServiceUnavailable(PlannerError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "routing_service_unavailable"


def api_exception_handler(exc, context):
    """Return every error as {"error": {"code", "message"}} (validation errors keep field detail)."""
    response = exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, APIException):
        detail = exc.detail
        if isinstance(detail, (dict, list)):
            body = {"error": {"code": "invalid_request", "message": "Invalid request.", "fields": detail}}
        else:
            body = {"error": {"code": getattr(detail, "code", exc.default_code), "message": str(detail)}}
        response.data = body
    return response
