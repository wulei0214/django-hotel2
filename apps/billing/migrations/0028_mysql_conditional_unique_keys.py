# -*- coding: utf-8 -*-
"""消除 billing 侧 MySQL 8 不支持的条件唯一约束（models.W036）。

涉及两处部分唯一索引：
  * billing/0027 uq_refund_property_execution_reference
    condition=~Q(execution_reference="")，保证同一物业渠道退款凭据不重复；
  * billing/0025 uq_pt_provider_event
    condition=~Q(event_id="")，保证渠道事件幂等。

MySQL 8 忽略这两条约束，只有 SQLite/PostgreSQL 生效。本迁移沿用
billing/0006 的治理方案，改为可空唯一键：
  * Refund.execution_reference_key = "{property_id}:{execution_reference}"，
    仅当参考号非空时写入；现金退款（空参考号）为 NULL，不受唯一约束限制；
  * ProviderTransaction.provider_event_key = "{provider_code}:{event_id}"，
    仅当渠道事件 ID 非空时写入。

两个键由对应模型 save() 统一维护，并与 update_fields 同步落库。
"""
from django.db import migrations, models
from django.db.models import Count


CONSTRAINT_NAMES = (
    ("refund", "uq_refund_property_execution_reference"),
    ("providertransaction", "uq_pt_provider_event"),
)


def _assert_no_duplicates(apps, schema_editor):
    Refund = apps.get_model("billing", "Refund")
    duplicates = list(
        Refund.objects.exclude(execution_reference="")
        .values("property_id", "execution_reference")
        .annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError("存在重复渠道退款凭据，启用数据库唯一键前必须人工核对：" f"{duplicates}")

    ProviderTransaction = apps.get_model("billing", "ProviderTransaction")
    duplicates = list(
        ProviderTransaction.objects.exclude(event_id="")
        .values("provider_code", "event_id")
        .annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError("存在重复渠道事件，启用数据库唯一键前必须人工核对：" f"{duplicates}")


def backfill_keys(apps, schema_editor):
    _assert_no_duplicates(apps, schema_editor)
    Refund = apps.get_model("billing", "Refund")
    for row in list(
        Refund.objects.exclude(execution_reference="").values(
            "id", "property_id", "execution_reference"
        )
    ):
        Refund.objects.filter(pk=row["id"]).update(
            execution_reference_key=(f"{row['property_id']}:{row['execution_reference']}")
        )

    ProviderTransaction = apps.get_model("billing", "ProviderTransaction")
    for row in list(
        ProviderTransaction.objects.exclude(event_id="").values("id", "provider_code", "event_id")
    ):
        ProviderTransaction.objects.filter(pk=row["id"]).update(
            provider_event_key=(f"{row['provider_code']}:{row['event_id']}")
        )


def drop_partial_indexes(apps, schema_editor):
    for model_name, name in CONSTRAINT_NAMES:
        model = apps.get_model("billing", model_name)
        table = model._meta.db_table
        with schema_editor.connection.cursor() as cursor:
            constraints = schema_editor.connection.introspection.get_constraints(cursor, table)
        if name not in constraints:
            # MySQL 上部分索引从未创建（W036），无需处理。
            continue
        q = schema_editor.quote_name
        if schema_editor.connection.vendor == "mysql":
            schema_editor.execute(f"ALTER TABLE {q(table)} DROP INDEX {q(name)}")
        else:
            schema_editor.execute(f"DROP INDEX IF EXISTS {q(name)}")


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0027_wb_recon_diff_refund_ref_unique_shift_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="refund",
            name="execution_reference_key",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=128,
                null=True,
                unique=True,
                verbose_name="退款渠道凭据唯一键",
            ),
        ),
        migrations.AddField(
            model_name="providertransaction",
            name="provider_event_key",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=192,
                null=True,
                unique=True,
                verbose_name="渠道事件唯一键",
            ),
        ),
        migrations.RunPython(backfill_keys, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_partial_indexes, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="refund",
                    name="uq_refund_property_execution_reference",
                ),
                migrations.RemoveConstraint(
                    model_name="providertransaction",
                    name="uq_pt_provider_event",
                ),
            ],
        ),
    ]
