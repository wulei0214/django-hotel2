# -*- coding: utf-8 -*-
"""班次现金变动方向由 MySQL/SQLite 数据库约束，不能只依赖表单 choices。"""
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0016_refund_sod_state_constraints"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="cashiershiftmovement",
            constraint=models.CheckConstraint(
                check=Q(direction__in=("IN", "OUT")),
                name="ck_shift_movement_direction"),
        ),
    ]
