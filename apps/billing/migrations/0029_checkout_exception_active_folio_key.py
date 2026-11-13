# -*- coding: utf-8 -*-
"""WB-112：为 CheckoutExceptionRelease 增加 Folio 级在途唯一 DB 兜底。

MySQL 8 不支持 condition UniqueConstraint（models.W036，约束不会创建），
因此沿用项目既有的 nullable-unique-key 模式：在途申请写
``cxr:folio:<folio_id>``，结案/驳回写 NULL，由普通唯一索引保证同一账单
最多一笔在途申请。服务层 Folio 行锁仍是主防线，本约束只作最后兜底。
"""
from django.db import migrations, models


ACTIVE = ("REQUESTED", "APPROVED", "RELEASED")


def backfill(apps, schema_editor):
    Model = apps.get_model("billing", "CheckoutExceptionRelease")
    seen = set()
    for row in Model.objects.filter(status__in=ACTIVE).order_by("pk").only("pk", "folio_id"):
        if row.folio_id in seen:
            # 历史脏数据：同一账单多笔在途只保留首笔的键，其余留 NULL，
            # 避免迁移因唯一键冲突失败；由人工核对处置。
            continue
        seen.add(row.folio_id)
        Model.objects.filter(pk=row.pk).update(active_folio_key=f"cxr:folio:{row.folio_id}")


def clear(apps, schema_editor):
    Model = apps.get_model("billing", "CheckoutExceptionRelease")
    Model.objects.all().update(active_folio_key=None)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0028_mysql_conditional_unique_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="checkoutexceptionrelease",
            name="active_folio_key",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                null=True,
                unique=True,
                verbose_name="在途放行唯一键",
            ),
        ),
        migrations.RunPython(backfill, clear),
    ]
