from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.shortcuts import render
from django.utils import timezone

from apps.core.models import Property


def public_booking_page(request):
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        property_obj = Property.objects.filter(
            code=getattr(settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"),
            status=Property.Status.ACTIVE,
        ).first()
    if property_obj is None:
        return render(
            request,
            "public/booking.html",
            {"property_name": "酒店预订", "booking_unavailable": True},
            status=503,
        )

    today = timezone.localdate(timezone.now(), ZoneInfo(property_obj.timezone))
    return render(
        request,
        "public/booking.html",
        {
            "property_name": property_obj.name,
            "default_check_in": (today + timedelta(days=1)).isoformat(),
            "default_check_out": (today + timedelta(days=2)).isoformat(),
            "mock_payments_enabled": (
                settings.DEBUG
                and getattr(settings, "DJANGO_ENV", "dev") in {"dev", "test"}
            ),
            "booking_unavailable": False,
        },
    )
