# -*- coding: utf-8 -*-
"""RSV-03：提前离店库存释放区间标记。

保留合同原始 check_out 不变，仅记录释放起始日；库存对账重算 sold
时据此排除已释放夜晚，避免把已释放量算回 sold 并写坏库存日历。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0009_pending_booking_gate"),
    ]

    operations = [
        migrations.AddField(
            model_name="reservationroom",
            name="inventory_released_from",
            field=models.DateField(blank=True, null=True, verbose_name="提前离店库存释放起始日"),
        ),
    ]
