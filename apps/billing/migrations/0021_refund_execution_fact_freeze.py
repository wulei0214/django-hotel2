# -*- coding: utf-8 -*-
"""冻结退款终态事实，并阻止未执行退款预写入执行参考号。

历史记录若不满足新状态事实约束，仅标记 legacy_workflow；不补造处理时间、
执行参考号或审批/执行人员，不改写历史财务事实。
"""
from django.db import migrations, models
from django.db.models import Q


def mark_unverifiable_legacy_refunds(apps, schema_editor):
    Refund = apps.get_model("billing", "Refund")
    invalid_processed_at = (
        Q(status__in=("PENDING", "APPROVED"), processed_at__isnull=False)
        | Q(status__in=("REJECTED", "SUCCEEDED"), processed_at__isnull=True)
        | ~Q(status__in=("PENDING", "APPROVED", "REJECTED", "SUCCEEDED"))
    )
    invalid_execution_reference = (
        ~Q(status="SUCCEEDED") & ~Q(execution_reference="")
    )
    Refund.objects.filter(
        invalid_processed_at | invalid_execution_reference
    ).update(legacy_workflow=True)


def keep_legacy_markers(apps, schema_editor):
    # 回滚约束不清除历史标记，避免历史数据重新被识别为新流程事实。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0020_reversal_approval_sod"),
    ]

    operations = [
        migrations.RunPython(
            mark_unverifiable_legacy_refunds, keep_legacy_markers),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | (
                        Q(status__in=("PENDING", "APPROVED"),
                          processed_at__isnull=True)
                        | Q(status__in=("REJECTED", "SUCCEEDED"),
                            processed_at__isnull=False)
                    )
                ),
                name="ck_refund_processed_at_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | Q(status="SUCCEEDED")
                    | Q(execution_reference="")
                ),
                name="ck_refund_execution_reference_state",
            ),
        ),
    ]
