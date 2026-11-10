from django.urls import path

from . import views

app_name = "health"

urlpatterns = [
    path("live/", views.LivenessView.as_view(), name="live"),
    path("ready/", views.ReadinessView.as_view(), name="ready"),
]
