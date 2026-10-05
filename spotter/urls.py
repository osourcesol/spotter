from django.urls import path

from .views import PlanOptionsView, RoutePlanView

urlpatterns = [
    path("route/", RoutePlanView.as_view(), name="route-plan"),
    path("options/", PlanOptionsView.as_view(), name="plan-options"),
]