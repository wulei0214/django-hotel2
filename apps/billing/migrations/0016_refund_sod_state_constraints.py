# -*- coding: utf-8 -*-
"""退款申请/审批/执行三方分离的数据库级约束。

旧版退款在执行人字段上线前已经落账，无法从数据库字段可靠反推独立执行人。
迁移只把无法证明新 SoD 规则的历史记录标记为 legacy_workflow，不改写其金额、
状态、审批人或账本事实。新记录必须满足数据库级申请/审批/执行分离与状态事实完整性。
"""
from django.db import migrations, models
from django.db.models import F, Q


def mark_unverifiable_legacy_refunds(apps, schema_editor):
    Refund = apps.get_model("billing", "Refund")
    invalid_sod = (
        (Q(approved_by__isnull=False) & Q(requested_by=F("approved_by")))
        | (Q(executed_by__isnull=False) & Q(requested_by=F("executed_by")))
        | (Q(executed_by__isnull=False) & Q(approved_by=F("executed_by")))
    )
    invalid_approval = (Q(status="PENDING") & Q(approved_by__isnull=False)) | (
        Q(status__in=("APPROVED", "SUCCEEDED", "REJECTED")) & Q(approved_by__isnull=True)
    )
    invalid_execution = (
        Q(status="SUCCEEDED")
        & (
            Q(executed_by__isnull=True)
            | Q(executed_at__isnull=True)
            | Q(execution_idempotency_key__isnull=True)
        )
    ) | (
        Q(status__in=("PENDING", "APPROVED", "REJECTED"))
        & (
            Q(executed_by__isnull=False)
            | Q(executed_at__isnull=False)
            | Q(execution_idempotency_key__isnull=False)
            | Q(execution_shift__isnull=False)
        )
    )
    Refund.objects.filter(invalid_sod | invalid_approval | invalid_execution).update(
        legacy_workflow=True
    )


def keep_legacy_markers(apps, schema_editor):
    # 不反向清除历史标记；回滚约束不能让历史记录重新伪装成新流程事实。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0015_alter_cashiershiftmovement_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="refund",
            name="legacy_workflow",
            field=models.BooleanField(
                default=False,
                editable=False,
                help_text="迁移前无法证明申请/审批/执行三方分离的历史记录；只读保留，不代表新流程验收。",
                verbose_name="历史退款流程记录",
            ),
        ),
        migrations.RunPython(mark_unverifiable_legacy_refunds, keep_legacy_markers),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | Q(approved_by__isnull=True)
                    | ~Q(requested_by=F("approved_by"))
                ),
                name="ck_refund_sod_request_approve",
            ),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | Q(executed_by__isnull=True)
                    | ~Q(requested_by=F("executed_by"))
                ),
                name="ck_refund_sod_request_execute",
            ),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | Q(executed_by__isnull=True)
                    | Q(approved_by__isnull=True)
                    | ~Q(approved_by=F("executed_by"))
                ),
                name="ck_refund_sod_approve_execute",
            ),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | (
                        Q(status="PENDING", approved_by__isnull=True)
                        | Q(
                            status__in=("APPROVED", "SUCCEEDED", "REJECTED"),
                            approved_by__isnull=False,
                        )
                    )
                ),
                name="ck_refund_approval_facts",
            ),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    Q(legacy_workflow=True)
                    | (
                        Q(
                            status="SUCCEEDED",
                            executed_by__isnull=False,
                            executed_at__isnull=False,
                            execution_idempotency_key__isnull=False,
                        )
                    )
                    | (
                        Q(
                            status__in=("PENDING", "APPROVED", "REJECTED"),
                            executed_by__isnull=True,
                            executed_at__isnull=True,
                            execution_idempotency_key__isnull=True,
                            execution_shift__isnull=True,
                        )
                    )
                ),
                name="ck_refund_execution_facts",
            ),
        ),
    ]
