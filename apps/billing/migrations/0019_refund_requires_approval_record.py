# -*- coding: utf-8 -*-
"""非待审批退款必须关联正式审批记录；无法追溯的历史记录只读保留。"""
from django.db import migrations, models
from django.db.models import Q


def mark_legacy_refunds_without_approval(apps, schema_editor):
    Refund = apps.get_model("billing", "Refund")
    Refund.objects.filter(
        status__in=("APPROVED", "SUCCEEDED", "REJECTED"),
        approval_record__isnull=True,
        legacy_workflow=False,
    ).update(legacy_workflow=True)


def keep_legacy_markers(apps, schema_editor):
    # 历史标记是审计事实，回滚约束时也不清除。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0018_cashier_shift_nonnegative_cash_constraints"),
    ]

    operations = [
        migrations.RunPython(mark_legacy_refunds_without_approval, keep_legacy_markers),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True) | Q(status="PENDING") | Q(approval_record__isnull=False)
                ),
                name="ck_refund_nonpending_approval_record",
            ),
        ),
    ]
