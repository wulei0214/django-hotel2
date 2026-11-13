# -*- coding: utf-8 -*-
"""把审批到期同步为退款终态，释放支付余额占用。"""
from django.db import migrations, models
from django.db.models import Sum


def reconcile_expired_refunds(apps, schema_editor):
    Refund = apps.get_model("billing", "Refund")
    ApprovalRecord = apps.get_model("billing", "ApprovalRecord")
    Payment = apps.get_model("billing", "Payment")

    expired_ids = []
    candidates = (
        Refund.objects.filter(
            status="PENDING",
            legacy_workflow=False,
            approval_record__status="EXPIRED",
        )
        .select_related("approval_record")
        .order_by("pk")
    )
    for refund in candidates.iterator():
        # 只使用审批事实中真实存在的到期时间，不伪造历史处理时间。
        if refund.approval_record.approved_at is None:
            Refund.objects.filter(pk=refund.pk).update(legacy_workflow=True)
            continue
        Refund.objects.filter(pk=refund.pk).update(
            status="EXPIRED",
            processed_at=refund.approval_record.approved_at,
        )
        expired_ids.append(refund.pk)

    payment_ids = list(
        Refund.objects.filter(pk__in=expired_ids).values_list("payment_id", flat=True).distinct()
    )
    for payment in Payment.objects.filter(pk__in=payment_ids).order_by("pk"):
        has_pending = Refund.objects.filter(
            payment_id=payment.pk,
            status__in=("PENDING", "APPROVED"),
        ).exists()
        if has_pending:
            target_status = "REFUND_PENDING"
        else:
            refunded_total = (
                Refund.objects.filter(payment_id=payment.pk, status="SUCCEEDED").aggregate(
                    total=Sum("amount")
                )["total"]
                or 0
            )
            if refunded_total >= payment.amount:
                target_status = "REFUNDED"
            elif refunded_total > 0:
                target_status = "PARTIALLY_REFUNDED"
            else:
                target_status = "SUCCEEDED"
        Payment.objects.filter(pk=payment.pk).update(status=target_status)


def preserve_expired_refund_facts(apps, schema_editor):
    # 不回滚已发生的审批到期事实。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0021_refund_execution_fact_freeze"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="refund", name="ck_refund_approval_facts"),
        migrations.RemoveConstraint(model_name="refund", name="ck_refund_execution_facts"),
        migrations.RemoveConstraint(model_name="refund", name="ck_refund_processed_at_state"),
        migrations.AlterField(
            model_name="refund",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "待审批"),
                    ("APPROVED", "已批准待执行"),
                    ("SUCCEEDED", "已执行完成"),
                    ("REJECTED", "已驳回"),
                    ("EXPIRED", "已过期"),
                ],
                default="PENDING",
                max_length=16,
                verbose_name="退款状态",
            ),
        ),
        migrations.RunPython(reconcile_expired_refunds, preserve_expired_refund_facts),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(legacy_workflow=True)
                    | (
                        models.Q(status="PENDING", approved_by__isnull=True)
                        | models.Q(
                            status__in=("APPROVED", "SUCCEEDED", "REJECTED"),
                            approved_by__isnull=False,
                        )
                        | models.Q(status="EXPIRED", approved_by__isnull=True)
                    )
                ),
                name="ck_refund_approval_facts",
            ),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(legacy_workflow=True)
                    | (
                        models.Q(
                            status="SUCCEEDED",
                            executed_by__isnull=False,
                            executed_at__isnull=False,
                            execution_idempotency_key__isnull=False,
                        )
                        | models.Q(
                            status__in=("PENDING", "APPROVED", "REJECTED", "EXPIRED"),
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
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(legacy_workflow=True)
                    | (
                        models.Q(
                            status__in=("PENDING", "APPROVED"),
                            processed_at__isnull=True,
                        )
                        | models.Q(
                            status__in=("REJECTED", "SUCCEEDED", "EXPIRED"),
                            processed_at__isnull=False,
                        )
                    )
                ),
                name="ck_refund_processed_at_state",
            ),
        ),
    ]
