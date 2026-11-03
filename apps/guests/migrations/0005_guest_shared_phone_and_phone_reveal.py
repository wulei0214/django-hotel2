# -*- coding: utf-8 -*-
from django.db import migrations, models
from django.db.models import Count


def mark_shared_phones(apps, schema_editor):
    GuestPhone = apps.get_model("guests", "GuestPhone")
    shared = (
        GuestPhone.objects.values("phone_normalized")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
        .values_list("phone_normalized", flat=True)
    )
    GuestPhone.objects.filter(phone_normalized__in=list(shared)).update(
        shared_review_required=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("guests", "0004_guestmembership_membershiplevelhistory_guestphone_and_more"),
        ("hotelapp", "0017_guest_pii_permissions"),
    ]
    operations = [
        migrations.RemoveConstraint(
            model_name="guest", name="uq_guest_property_phone",
        ),
        migrations.AddIndex(
            model_name="guest",
            index=models.Index(
                fields=["property", "phone_normalized"],
                name="idx_guest_prop_phone",
            ),
        ),
        migrations.AlterModelOptions(
            name="guestphone",
            options={
                "verbose_name": "客人电话",
                "verbose_name_plural": "客人电话",
                "permissions": [
                    ("phone_reveal", "能揭示客人电话号码完整值"),
                ],
            },
        ),
        migrations.RunPython(mark_shared_phones, migrations.RunPython.noop),
    ]
