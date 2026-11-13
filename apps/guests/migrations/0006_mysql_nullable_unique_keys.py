# -*- coding: utf-8 -*-
from django.db import migrations, models
from django.db.models import Count


def backfill_keys(apps, schema_editor):
    GuestPhone = apps.get_model("guests", "GuestPhone")
    GuestMembership = apps.get_model("guests", "GuestMembership")

    primary = GuestPhone.objects.filter(is_primary=True)
    duplicates = list(
        primary.values("guest_id").annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            "A Guest has multiple primary phone records; reconcile manually: "
            f"{duplicates}"
        )
    for item in list(primary.values("id", "guest_id")):
        GuestPhone.objects.filter(pk=item["id"]).update(
            primary_guest_key=str(item["guest_id"])
        )

    active = GuestMembership.objects.filter(status="ACTIVE")
    duplicates = list(
        active.values("property_id", "guest_id").annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            "A guest has multiple ACTIVE memberships in one property; "
            f"reconcile manually: {duplicates}"
        )
    for item in list(active.values("id", "property_id", "guest_id")):
        GuestMembership.objects.filter(pk=item["id"]).update(
            active_guest_key=f"{item['property_id']}:{item['guest_id']}"
        )


def drop_partial_index(apps, schema_editor, model_name, constraint_name):
    model = apps.get_model("guests", model_name)
    table = model._meta.db_table
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, table
        )
    if constraint_name not in constraints:
        return
    q = schema_editor.quote_name
    if schema_editor.connection.vendor == "mysql":
        schema_editor.execute(
            f"ALTER TABLE {q(table)} DROP INDEX {q(constraint_name)}"
        )
    else:
        schema_editor.execute(f"DROP INDEX IF EXISTS {q(constraint_name)}")


def drop_phone_partial(apps, schema_editor):
    drop_partial_index(apps, schema_editor, "GuestPhone",
                       "uq_guest_phone_primary")


def drop_membership_partial(apps, schema_editor):
    drop_partial_index(apps, schema_editor, "GuestMembership",
                       "uq_membership_property_guest_active")


class Migration(migrations.Migration):
    dependencies = [("guests", "0005_guest_shared_phone_and_phone_reveal")]

    operations = [
        migrations.AddField(
            model_name="guestphone",
            name="primary_guest_key",
            field=models.CharField(
                blank=True, editable=False, max_length=32, null=True,
                unique=True, verbose_name="主号码唯一键"),
        ),
        migrations.AddField(
            model_name="guestmembership",
            name="active_guest_key",
            field=models.CharField(
                blank=True, editable=False, max_length=64, null=True,
                unique=True, verbose_name="有效会员唯一键"),
        ),
        migrations.RunPython(backfill_keys, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_phone_partial, migrations.RunPython.noop),
                migrations.RunPython(drop_membership_partial, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="guestphone",
                    name="uq_guest_phone_primary",
                ),
                migrations.RemoveConstraint(
                    model_name="guestmembership",
                    name="uq_membership_property_guest_active",
                ),
            ],
        ),
    ]
