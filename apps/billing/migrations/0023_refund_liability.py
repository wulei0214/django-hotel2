# -*- coding: utf-8 -*-
"""建立退房后待退款负债及其原支付候选追踪。"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import F, Q


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0022_refund_approval_expiry"),
        ("reservations", "0006_add_booking_access_token"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RefundLiability",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="负债原始金额")),
                ("remaining_amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="待处理余额")),
                ("currency", models.CharField(default="CNY", max_length=3, verbose_name="币种")),
                ("status", models.CharField(
                    choices=[("OPEN", "待处理"), ("PARTIALLY_SETTLED", "部分处理"), ("SETTLED", "已处理")],
                    default="OPEN", max_length=20, verbose_name="负债状态")),
                ("idempotency_key", models.CharField(max_length=96, unique=True, verbose_name="幂等键")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="登记时间")),
                ("settled_at", models.DateTimeField(blank=True, null=True, verbose_name="结清时间")),
                ("created_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="created_refund_liabilities", to=settings.AUTH_USER_MODEL,
                    verbose_name="登记人")),
                ("folio", models.OneToOneField(
                    on_delete=django.db.models.deletion.PROTECT, related_name="refund_liability",
                    to="billing.folio", verbose_name="来源账单")),
                ("property", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="refund_liabilities",
                    to="core.property", verbose_name="所属酒店")),
                ("reservation", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, related_name="refund_liabilities",
                    to="reservations.reservation", verbose_name="预订主单")),
                ("source_payments", models.ManyToManyField(
                    blank=True, help_text="仅记录可供财务核对的原支付候选，不代表系统已分配退款金额。",
                    related_name="candidate_refund_liabilities", to="billing.payment",
                    verbose_name="候选原支付流水")),
            ],
            options={
                "verbose_name": "待退款负债",
                "verbose_name_plural": "待退款负债",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddField(
            model_name="refund",
            name="refund_liability",
            field=models.ForeignKey(
                blank=True, help_text="仅用于财务后续主动发起退款时关联待退款负债；不自动发起退款。",
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="refunds", to="billing.refundliability",
                verbose_name="关联待退款负债"),
        ),
        migrations.AddConstraint(
            model_name="refundliability",
            constraint=models.CheckConstraint(check=Q(amount__gt=0), name="ck_rfl_amt_pos")),
        migrations.AddConstraint(
            model_name="refundliability",
            constraint=models.CheckConstraint(
                check=Q(remaining_amount__gte=0) & Q(remaining_amount__lte=F("amount")),
                name="ck_rfl_rem_rng")),
        migrations.AddConstraint(
            model_name="refundliability",
            constraint=models.CheckConstraint(
                check=(Q(status="OPEN", remaining_amount=F("amount"), settled_at__isnull=True)
                       | Q(status="PARTIALLY_SETTLED", remaining_amount__gt=0,
                           remaining_amount__lt=F("amount"), settled_at__isnull=True)
                       | Q(status="SETTLED", remaining_amount=0, settled_at__isnull=False)),
                name="ck_rfl_status")),
    ]
