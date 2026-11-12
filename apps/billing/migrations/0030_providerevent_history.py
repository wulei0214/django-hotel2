# -*- coding: utf-8 -*-
"""WB-119/125：渠道事件历史表 ProviderTransactionEvent。

语义明确化：
  * ProviderTransaction = 渠道交易主记录，(provider_code,
    provider_transaction_id) 唯一，仅承载受控状态机推进后的**最新**事实；
  * ProviderTransactionEvent = 渠道事件历史，每次通知/查询结果各一行，
    event_id 幂等（provider_event_key 可空唯一键），事件永不修改。

此前「同一交易号不同事件 ID 推进状态」会撞 uq_pt_provider_txn
（WB-119），且实例 save 可绕过不可变保护（WB-125）。本迁移建事件表并把
存量主记录的当前事实回填为首条事件；主记录的 uq_pt_provider_txn 保持
不变（继续保证同一交易号只归属一个意图）。
"""
from django.db import migrations, models


def backfill_events(apps, schema_editor):
    """把每条存量渠道交易主记录的当前事实回填为首条事件。"""
    PT = apps.get_model("billing", "ProviderTransaction")
    PTE = apps.get_model("billing", "ProviderTransactionEvent")
    for row in PT.objects.order_by("pk").values(
            "id", "property_id", "intent_id", "provider_code",
            "provider_transaction_id", "event_id", "amount", "currency",
            "channel_status", "signature_verified"):
        if PTE.objects.filter(transaction_id=row["id"]).exists():
            continue
        event_id = (row["event_id"] or "").strip()
        PTE.objects.create(
            property_id=row["property_id"], intent_id=row["intent_id"],
            transaction_id=row["id"],
            provider_code=row["provider_code"],
            provider_transaction_id=row["provider_transaction_id"],
            event_id=event_id, amount=row["amount"], currency=row["currency"],
            channel_status=row["channel_status"],
            signature_verified=row["signature_verified"],
            provider_event_key=(
                f"{row['provider_code']}:{event_id}" if event_id else None))


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0029_checkout_exception_active_folio_key"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProviderTransactionEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("provider_code", models.CharField(max_length=32, verbose_name="渠道代码")),
                ("provider_transaction_id", models.CharField(max_length=128, verbose_name="渠道交易号")),
                ("event_id", models.CharField(blank=True, default="", max_length=128, verbose_name="渠道事件 ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="交易金额")),
                ("currency", models.CharField(max_length=3, verbose_name="币种")),
                ("channel_status", models.CharField(max_length=32, verbose_name="渠道状态")),
                ("signature_verified", models.BooleanField(default=False, verbose_name="验签通过")),
                ("received_at", models.DateTimeField(auto_now_add=True, verbose_name="PMS 接收时间")),
                ("provider_event_key", models.CharField(blank=True, editable=False, max_length=192, null=True, unique=True, verbose_name="渠道事件唯一键")),
                ("intent", models.ForeignKey(on_delete=models.PROTECT, related_name="provider_transaction_events", to="billing.paymentintent", verbose_name="关联支付意图")),
                ("property", models.ForeignKey(on_delete=models.PROTECT, related_name="provider_transaction_events", to="core.property", verbose_name="所属酒店")),
                ("transaction", models.ForeignKey(on_delete=models.PROTECT, related_name="events", to="billing.providertransaction", verbose_name="渠道交易主记录")),
            ],
            options={
                "verbose_name": "渠道事件历史",
                "verbose_name_plural": "渠道事件历史",
                "ordering": ("-received_at", "-pk"),
            },
        ),
        migrations.AddIndex(
            model_name="providertransactionevent",
            index=models.Index(fields=["provider_code", "provider_transaction_id"], name="idx_pte_code_txn"),
        ),
        migrations.RunPython(backfill_events, migrations.RunPython.noop),
    ]
