# -*- coding: utf-8 -*-
from django.db import migrations, models
from django.db.models import Count


def backfill_keys(apps, schema_editor):
    StayRoomSegment = apps.get_model("reservations", "StayRoomSegment")
    ReservationGuest = apps.get_model("reservations", "ReservationGuest")
    InventoryHold = apps.get_model("reservations", "InventoryHold")

    active_segments = StayRoomSegment.objects.filter(ended_at__isnull=True)
    duplicates = list(
        active_segments.values("stay_id").annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            "A Stay has multiple active room segments; reconcile manually: "
            f"{duplicates}"
        )
    for item in list(active_segments.values("id", "stay_id")):
        StayRoomSegment.objects.filter(pk=item["id"]).update(
            active_stay_key=str(item["stay_id"])
        )

    primary_guests = ReservationGuest.objects.filter(is_primary=True)
    duplicates = list(
        primary_guests.values("reservation_id").annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            "A Reservation has multiple primary guest snapshots; "
            f"reconcile manually: {duplicates}"
        )
    for item in list(primary_guests.values("id", "reservation_id")):
        ReservationGuest.objects.filter(pk=item["id"]).update(
            primary_reservation_key=str(item["reservation_id"])
        )

    active_holds = InventoryHold.objects.filter(status__in=("HELD", "CONFIRMED"))
    duplicates = list(
        active_holds.values("reservation_room_id", "stay_date")
        .annotate(total=Count("id")).filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            "A reservation room has duplicate active inventory holds for a "
            f"stay date; reconcile manually: {duplicates}"
        )
    for item in list(
        active_holds.values("id", "reservation_room_id", "stay_date")
    ):
        InventoryHold.objects.filter(pk=item["id"]).update(
            active_hold_key=f"{item['reservation_room_id']}:{item['stay_date'].isoformat()}"
        )


def drop_partial_index(apps, schema_editor, model_name, constraint_name):
    model = apps.get_model("reservations", model_name)
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


def drop_segment_partial(apps, schema_editor):
    drop_partial_index(apps, schema_editor, "StayRoomSegment",
                       "uq_stay_one_active_room_segment")


def drop_guest_partial(apps, schema_editor):
    drop_partial_index(apps, schema_editor, "ReservationGuest",
                       "uq_reservation_guest_primary")


def drop_hold_partial(apps, schema_editor):
    drop_partial_index(apps, schema_editor, "InventoryHold",
                       "uq_hold_active_per_roomdate")


class Migration(migrations.Migration):
    dependencies = [("reservations", "0004_reservationguest_stayguest_inventoryhold_and_more")]

    operations = [
        migrations.AddField(
            model_name="stayroomsegment",
            name="active_stay_key",
            field=models.CharField(
                blank=True, editable=False, max_length=32, null=True,
                unique=True, verbose_name="当前房段唯一键"),
        ),
        migrations.AddField(
            model_name="reservationguest",
            name="primary_reservation_key",
            field=models.CharField(
                blank=True, editable=False, max_length=32, null=True,
                unique=True, verbose_name="主住客唯一键"),
        ),
        migrations.AddField(
            model_name="inventoryhold",
            name="active_hold_key",
            field=models.CharField(
                blank=True, editable=False, max_length=64, null=True,
                unique=True, verbose_name="有效冻结唯一键"),
        ),
        migrations.RunPython(backfill_keys, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_segment_partial, migrations.RunPython.noop),
                migrations.RunPython(drop_guest_partial, migrations.RunPython.noop),
                migrations.RunPython(drop_hold_partial, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="stayroomsegment",
                    name="uq_stay_one_active_room_segment",
                ),
                migrations.RemoveConstraint(
                    model_name="reservationguest",
                    name="uq_reservation_guest_primary",
                ),
                migrations.RemoveConstraint(
                    model_name="inventoryhold",
                    name="uq_hold_active_per_roomdate",
                ),
            ],
        ),
    ]
