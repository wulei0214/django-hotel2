# -*- coding: utf-8 -*-
"""收银班次现金字段非负约束；迁移前先拒绝静默修正异常历史账务。"""
from django.db import migrations, models
from django.db.models import Q


def validate_existing_shift_cash(apps, schema_editor):
    CashierShift = apps.get_model("billing", "CashierShift")
    invalid = CashierShift.objects.filter(
        Q(opening_cash__lt=0)
        | Q(expected_cash__lt=0)
        | Q(actual_cash__lt=0)
    )
    if invalid.exists():
        sample_ids = list(invalid.values_list("pk", flat=True)[:20])
        raise RuntimeError(
            "存在负数收银班次现金事实，必须先人工核对并完成审计调整后再迁移；"
            f"异常班次 ID（最多 20 条）：{sample_ids}"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0017_cash_shift_movement_direction_constraint"),
    ]

    operations = [
        migrations.RunPython(validate_existing_shift_cash, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="cashiershift",
            constraint=models.CheckConstraint(
                check=Q(opening_cash__gte=0),
                name="ck_cashier_shift_opening_cash_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="cashiershift",
            constraint=models.CheckConstraint(
                check=Q(expected_cash__isnull=True) | Q(expected_cash__gte=0),
                name="ck_cashier_shift_expected_cash_nonnegative"),
        ),
        migrations.AddConstraint(
            model_name="cashiershift",
            constraint=models.CheckConstraint(
                check=Q(actual_cash__isnull=True) | Q(actual_cash__gte=0),
                name="ck_cashier_shift_actual_cash_nonnegative"),
        ),
    ]
