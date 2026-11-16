# -*- coding: utf-8 -*-
"""A3 客人合并（排期 V2.0 第二主线）：Guest 软归档字段。

merged_into / merged_at 成对写入：非空 merged_into 表示档案已合并入主档案
（保留行、不物理删除，满足身份方案 §8.2-4 的完整关联映射留痕要求）；
guest 列表默认 exclude 已合并档案，duplicate 不再出现在可检索结果中。
账务事实（Folio/Payment/LedgerEntry）归属原预订不动，随预订 guest FK
迁移到主档案，本迁移不触碰任何账务数据。
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("guests", "0007_phone_pii_ciphertext_and_document_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="guest",
            name="merged_into",
            field=models.ForeignKey(
                blank=True,
                help_text="非空表示本档案已合并入该主档案（软归档，保留行不物理删除）",
                null=True,
                on_delete=models.PROTECT,
                related_name="merged_sources",
                to="guests.guest",
                verbose_name="合并入主档案",
            ),
        ),
        migrations.AddField(
            model_name="guest",
            name="merged_at",
            field=models.DateTimeField(
                blank=True,
                help_text="执行合并动作的时间；与 merged_into 成对写入",
                null=True,
                verbose_name="合并时间",
            ),
        ),
        migrations.AddIndex(
            model_name="guest",
            index=models.Index(fields=("merged_into",), name="idx_guest_merged_into"),
        ),
    ]
