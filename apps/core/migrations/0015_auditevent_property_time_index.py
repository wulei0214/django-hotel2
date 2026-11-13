# -*- coding: utf-8 -*-
"""MAINT-06：补齐 AuditEvent (property, -created_at) 复合索引。

审计列表、housekeeping/audit 汇总均按物业 + 时间窗口过滤；append-only
表随时间线性增长，此前只有 entity/action 两条索引，物业级查询需全表扫。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_idempotency_updated_at"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(fields=["property", "-created_at"], name="idx_audit_property_time"),
        ),
    ]
