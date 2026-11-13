# -*- coding: utf-8 -*-
"""退款申请/审批/执行三分离与现金班次事实模型。"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0011_grant_reverse_entry_permission"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="cashier_shift",
            field=models.ForeignKey(
                blank=True,
                help_text="人工现金收款必须关联当班；线上/非现金支付可为空",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="payments",
                to="billing.cashiershift",
                verbose_name="收银班次",
            ),
        ),
        migrations.AlterField(
            model_name="refund",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "待审批"),
                    ("APPROVED", "已批准待执行"),
                    ("SUCCEEDED", "已执行完成"),
                    ("REJECTED", "已驳回"),
                ],
                default="PENDING",
                max_length=16,
                verbose_name="退款状态",
            ),
        ),
        migrations.AddField(
            model_name="refund",
            name="executed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="executed_refunds",
                to=settings.AUTH_USER_MODEL,
                verbose_name="执行人",
            ),
        ),
        migrations.AddField(
            model_name="refund",
            name="executed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="执行时间"),
        ),
        migrations.AddField(
            model_name="refund",
            name="execution_reference",
            field=models.CharField(blank=True, max_length=80, verbose_name="退款渠道/凭据参考号"),
        ),
        migrations.AddField(
            model_name="refund",
            name="execution_idempotency_key",
            field=models.CharField(
                blank=True, max_length=96, null=True, unique=True, verbose_name="退款执行幂等键"
            ),
        ),
        migrations.AddField(
            model_name="refund",
            name="execution_shift",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="executed_refunds",
                to="billing.cashiershift",
                verbose_name="退款执行班次",
            ),
        ),
        migrations.AddField(
            model_name="cashiershift",
            name="closed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="closed_cashier_shifts",
                to=settings.AUTH_USER_MODEL,
                verbose_name="关班人",
            ),
        ),
        migrations.AlterModelOptions(
            name="refund",
            options={
                "verbose_name": "退款申请",
                "verbose_name_plural": "退款申请",
                "ordering": ("-created_at",),
                "permissions": (
                    ("refund_request", "可以登记退款申请"),
                    ("refund_approve", "可以审批退款申请"),
                    ("refund_execute", "可以执行已批准退款"),
                ),
            },
        ),
        migrations.AlterModelOptions(
            name="cashiershift",
            options={
                "verbose_name": "现金班次",
                "verbose_name_plural": "现金班次",
                "ordering": ("-opened_at",),
                "permissions": (("cash_movement_manage", "可以登记班次现金收入与支出"),),
            },
        ),
        migrations.CreateModel(
            name="CashierShiftMovement",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "direction",
                    models.CharField(
                        choices=[("IN", "现金收入/补入"), ("OUT", "现金支出/取出")], max_length=3
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("reason", models.CharField(max_length=240)),
                ("idempotency_key", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cashier_shift_movements",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="登记人",
                    ),
                ),
                (
                    "shift",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cash_movements",
                        to="billing.cashiershift",
                        verbose_name="收银班次",
                    ),
                ),
            ],
            options={
                "ordering": ("created_at", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="cashiershiftmovement",
            constraint=models.UniqueConstraint(
                fields=("shift", "idempotency_key"), name="uq_shift_movement_idempotency"
            ),
        ),
        migrations.AddConstraint(
            model_name="cashiershiftmovement",
            constraint=models.CheckConstraint(
                check=models.Q(("amount__gt", 0)), name="ck_shift_movement_amount_positive"
            ),
        ),
    ]
