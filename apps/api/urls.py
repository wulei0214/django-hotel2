from django.urls import path

from . import views

app_name = "api"

urlpatterns = [
    path("availability", views.availability, name="availability"),
    path("reservations", views.reservations_create, name="reservations_create"),
    path("reservations/<str:booking_no>", views.reservation_detail, name="reservation_detail"),
    path("reservations/<str:booking_no>/cancel", views.reservation_cancel, name="reservation_cancel"),
    path("payments/intents", views.payment_intent_create, name="payment_intent_create"),
    path("payments/callback", views.payment_callback, name="payment_callback"),
    path("openapi.json", views.openapi_document, name="openapi"),
]
