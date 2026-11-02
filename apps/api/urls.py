from django.urls import path

from . import front_desk, views

app_name = "api"

urlpatterns = [
    path("front-desk/overview", front_desk.overview, name="front_desk_overview"),
    path("front-desk/arrivals", front_desk.arrivals, name="front_desk_arrivals"),
    path("front-desk/departures", front_desk.departures, name="front_desk_departures"),
    path("front-desk/in-house", front_desk.in_house, name="front_desk_in_house"),
    path("front-desk/reservations", front_desk.reservations, name="front_desk_reservations"),
    path("front-desk/reservations/<str:booking_no>", front_desk.reservation_detail, name="front_desk_reservation_detail"),
    path("front-desk/rooms", front_desk.rooms, name="front_desk_rooms"),
    path("front-desk/rooms/assignable", front_desk.assignable_rooms, name="front_desk_assignable_rooms"),
    path("front-desk/folios/<str:booking_no>", front_desk.folio_detail, name="front_desk_folio_detail"),
    path("front-desk/reservation-rooms/<int:reservation_room_id>/assign", front_desk.assign_room, name="front_desk_assign_room"),
    path("front-desk/reservation-rooms/<int:reservation_room_id>/check-in", front_desk.check_in, name="front_desk_check_in"),
    path("front-desk/reservations/<str:booking_no>/payments", front_desk.collect_payment, name="front_desk_collect_payment"),
    path("front-desk/stays/<int:stay_id>/check-out", front_desk.check_out, name="front_desk_check_out"),
    path("availability", views.availability, name="availability"),
    path("reservations", views.reservations_create, name="reservations_create"),
    path("reservations/<str:booking_no>", views.reservation_detail, name="reservation_detail"),
    path("reservations/<str:booking_no>/cancel", views.reservation_cancel, name="reservation_cancel"),
    path("payments/intents", views.payment_intent_create, name="payment_intent_create"),
    path("payments/callback", views.payment_callback, name="payment_callback"),
    path("openapi.json", views.openapi_document, name="openapi"),
]
