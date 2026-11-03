# -*- coding: utf-8 -*-
from django.db import migrations, models
from django.db.models import Count


def _assert_unique(queryset, fields, label):
    duplicates = list(
        queryset.values(*fields).annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            f"{label} has duplicate active records; reconcile manually before "
            f"enabling database uniqueness: {duplicates}"
        )


def backfill_keys(apps, schema_editor):
    Folio = apps.get_model("billing", "Folio")
    CashierShift = apps.get_model("billing", "CashierShift")

    masters = Folio.objects.filter(folio_type="MASTER")
    _assert_unique(masters, ["reservation_id"], "MASTER folio")
    for item in list(masters.values("id", "reservation_id")):
        Folio.objects.filter(pk=item["id"]).update(
            master_reservation_key=str(item["reservation_id"])
        )

    opened = CashierShift.objects.filter(status="OPEN")
    _assert_unique(opened, ["property_id", "cashier_id"], "OPEN cashier shift")
    for item in list(opened.values("id", "property_id", "cashier_id")):
        CashierShift.objects.filter(pk=item["id"]).update(
            open_shift_key=f"{item['property_id']}:{item['cashier_id']}"
        )


def drop_partial_index(apps, schema_editor, model_name, constraint_name):
    model = apps.get_model("billing", model_name)
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


def drop_folio_partial(apps, schema_editor):
    drop_partial_index(apps, schema_editor, "Folio",
                       "uq_folio_master_per_reservation")


def drop_shift_partial(apps, schema_editor):
    drop_partial_index(apps, schema_editor, "CashierShift",
                       "uq_cashier_shift_one_open")


class Migration(migrations.Migration):
    dependencies = [("billing", "0005_chargeline_taxline_cashiershift_and_more")]

    operations = [
        migrations.AddField(
            model_name="folio",
            name="master_reservation_key",
            field=models.CharField(
                blank=True, editable=False, max_length=32, null=True,
                unique=True, verbose_name="主账唯一键"),
        ),
        migrations.AddField(
            model_name="cashiershift",
            name="open_shift_key",
            field=models.CharField(
                blank=True, editable=False, max_length=96, null=True,
                unique=True, verbose_name="开放班次唯一键"),
        ),
        migrations.RunPython(backfill_keys, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_folio_partial, migrations.RunPython.noop),
                migrations.RunPython(drop_shift_partial, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="folio",
                    name="uq_folio_master_per_reservation",
                ),
                migrations.RemoveConstraint(
                    model_name="cashiershift",
                    name="uq_cashier_shift_one_open",
                ),
            ],
        ),
    ]
