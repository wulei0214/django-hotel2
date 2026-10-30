import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("core", "0001_initial"),
        ("guests", "0001_initial"),
        ("reservations", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="Folio",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("OPEN", "未结"), ("CLOSED", "已结"), ("VOIDED", "已作废")], default="OPEN", max_length=12)),
                ("currency", models.CharField(default="CNY", max_length=3)),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("guest", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="folios", to="guests.guest")),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="folios", to="core.property")),
                ("reservation", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="folio", to="reservations.reservation")),
            ],
            options={"verbose_name": "住客账单", "verbose_name_plural": "住客账单", "ordering": ("-opened_at",)},
        ),
        migrations.CreateModel(
            name="LedgerEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("entry_type", models.CharField(choices=[("ROOM_CHARGE", "房费"), ("DEPOSIT", "押金"), ("PAYMENT", "收款"), ("REFUND", "退款"), ("ADJUSTMENT", "调整"), ("REVERSAL", "红字冲销")], max_length=20)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="CNY", max_length=3)),
                ("business_date", models.DateField()),
                ("reference", models.CharField(max_length=160, unique=True)),
                ("description", models.CharField(blank=True, max_length=240)),
                ("void_reason", models.CharField(blank=True, max_length=240)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("folio", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="entries", to="billing.folio")),
                ("posted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="posted_ledger_entries", to=settings.AUTH_USER_MODEL)),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="ledger_entries", to="core.property")),
                ("stay", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="ledger_entries", to="reservations.stay")),
                ("void_of", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversals", to="billing.ledgerentry")),
            ],
            options={"verbose_name": "不可变账本凭证", "verbose_name_plural": "不可变账本凭证", "ordering": ("created_at", "id")},
        ),
        migrations.CreateModel(
            name="Payment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider", models.CharField(default="mock", max_length=24)),
                ("provider_ref", models.CharField(max_length=80, unique=True)),
                ("method", models.CharField(default="MOCK", max_length=24)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="CNY", max_length=3)),
                ("status", models.CharField(choices=[("CREATED", "已创建"), ("PENDING", "待支付"), ("SUCCEEDED", "成功"), ("FAILED", "失败"), ("CANCELLED", "已取消"), ("REFUND_PENDING", "退款处理中"), ("PARTIALLY_REFUNDED", "部分退款"), ("REFUNDED", "已退款")], default="PENDING", max_length=24)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("folio", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="billing.folio")),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="core.property")),
                ("reservation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="reservations.reservation")),
            ],
            options={"verbose_name": "支付流水", "verbose_name_plural": "支付流水", "ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(model_name="ledgerentry", constraint=models.CheckConstraint(check=~Q(amount=0), name="ck_ledger_amount_nonzero")),
        migrations.AddConstraint(model_name="ledgerentry", constraint=models.CheckConstraint(check=Q(void_of__isnull=True) | Q(void_reason__gt=""), name="ck_ledger_reversal_has_reason")),
        migrations.AddConstraint(model_name="payment", constraint=models.CheckConstraint(check=Q(amount__gt=0), name="ck_payment_amount_positive")),
        migrations.AddIndex(model_name="ledgerentry", index=models.Index(fields=["property", "business_date"], name="billing_ledg_propert_9f4ef8_idx")),
        migrations.AddIndex(model_name="ledgerentry", index=models.Index(fields=["folio", "business_date"], name="billing_ledg_folio_i_31c7c2_idx")),
    ]
