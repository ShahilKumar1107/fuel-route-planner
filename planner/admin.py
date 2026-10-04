from django.contrib import admin

from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "retail_price", "latitude", "longitude")
    list_filter = ("state",)
    search_fields = ("name", "city", "opis_id")
    ordering = ("retail_price",)
