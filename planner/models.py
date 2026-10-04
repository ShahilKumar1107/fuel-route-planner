from django.db import models


class FuelStation(models.Model):
    """A truck stop with its retail diesel price.

    One row per OPIS truck-stop ID. The source CSV repeats some IDs with
    several prices; the importer keeps the lowest one.
    """

    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(max_digits=7, decimal_places=4)
    latitude = models.FloatField()
    longitude = models.FloatField()
    # "city" = city-centroid geocode. Kept so precise geocodes can be added later.
    geocode_precision = models.CharField(max_length=16, default="city")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["opis_id"]
        indexes = [models.Index(fields=["retail_price"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state}) ${self.retail_price}"
