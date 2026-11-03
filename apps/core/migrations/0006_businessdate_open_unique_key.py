# -*- coding: utf-8 -*-
from django.db import migrations, models
from django.db.models import Count


def backfill_open_key(apps, schema_editor):
    BusinessDate = apps.get_model("core", "BusinessDate")
    opened = BusinessDate.objects.filter(status="OPEN")
    duplicates = list(
        opened.values("property_id").annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            "More than one OPEN BusinessDate exists for a property. "
            f"Reconcile manually before applying migration: {duplicates}"
        )
    for item in list(opened.values("id", "property_id")):
        BusinessDate.objects.filter(pk=item["id"]).update(
            open_property_key=str(item["property_id"])
        )


def drop_partial_index(apps, schema_editor):
    model = apps.get_model("core", "BusinessDate")
    table = model._meta.db_table
    name = "uq_business_date_one_open"
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, table
        )
    if name not in constraints:
        return
    q = schema_editor.quote_name
    if schema_editor.connection.vendor == "mysql":
        schema_editor.execute(f"ALTER TABLE {q(table)} DROP INDEX {q(name)}")
    else:
        schema_editor.execute(f"DROP INDEX IF EXISTS {q(name)}")


class Migration(migrations.Migration):
    dependencies = [("core", "0005_statustransition_idempotencyrecord_and_more")]

    operations = [
        migrations.AddField(
            model_name="businessdate",
            name="open_property_key",
            field=models.CharField(
                blank=True, editable=False, max_length=32, null=True,
                unique=True, verbose_name="开放营业日唯一键"),
        ),
        migrations.RunPython(backfill_open_key, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_partial_index, migrations.RunPython.noop)
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="businessdate",
                    name="uq_business_date_one_open",
                )
            ],
        ),
    ]
