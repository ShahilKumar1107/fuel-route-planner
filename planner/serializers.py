from django.conf import settings
from rest_framework import serializers


class RoutePlanRequestSerializer(serializers.Serializer):
    start = serializers.CharField(
        max_length=200,
        help_text="'City, ST' (e.g. 'New York, NY') or 'lat,lon'.",
    )
    finish = serializers.CharField(max_length=200, help_text="Same formats as start.")
    range_miles = serializers.FloatField(
        required=False,
        min_value=50,
        max_value=2000,
        help_text="Max distance on a full tank. Default 500.",
    )
    mpg = serializers.FloatField(required=False, min_value=1, max_value=100, help_text="Default 10.")
    start_fuel_gallons = serializers.FloatField(
        required=False,
        min_value=0,
        allow_null=True,
        help_text="Fuel in the tank at the start. Default: full tank.",
    )
    stop_penalty = serializers.FloatField(
        required=False,
        min_value=0,
        max_value=1000,
        help_text="Dollars a stop must save to be worth making (driver time). 0 = pure cheapest plan. Default 10.",
    )
    include_geometry = serializers.BooleanField(
        required=False,
        default=True,
        help_text="Include the GeoJSON route line. Set false for a smaller payload.",
    )

    def validate(self, attrs):
        cfg = settings.FUEL_PLANNER
        attrs.setdefault("range_miles", cfg["VEHICLE_RANGE_MILES"])
        attrs.setdefault("mpg", cfg["VEHICLE_MPG"])
        attrs.setdefault("stop_penalty", cfg["STOP_PENALTY_DOLLARS"])
        if attrs["start"].strip().lower() == attrs["finish"].strip().lower():
            raise serializers.ValidationError({"finish": "Finish must be different from start."})
        return attrs
