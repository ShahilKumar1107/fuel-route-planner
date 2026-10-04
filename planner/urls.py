from django.urls import path

from . import views

urlpatterns = [
    path("", views.api_root, name="api-root"),
    path("route-plan/", views.RoutePlanView.as_view(), name="route-plan"),
    path("route-plan/map/", views.route_map_view, name="route-plan-map"),
    path("health/", views.health, name="health"),
]
