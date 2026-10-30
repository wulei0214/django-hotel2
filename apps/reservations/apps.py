from django.apps import AppConfig


class ReservationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.reservations"
    label = "reservations"
    verbose_name = "预订与在住"
