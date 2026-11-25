"""NS-E-05 IMPLEMENT-001 / FIN-06：统一收款执行权权限点。

OWNER 2026-11-24 逐项裁定 FIN-06 = APPROVED。

billing.payment_collect 同时门禁两个收款入口：
  * 财务中心 POST /api/v1/billing/payments（finance_access + 本码叠加）；
  * 前台 POST /front-desk/reservations/{no}/payments（替代原
    change_accommodation 隐式授权）。

本迁移只创建权限点本体；角色授权（前台/值班经理/财务/店长）由
identity 域迁移 0013 承接，与 guests 域权限同一分工。

边界（裁定 §3.3，不在本权限内）：
  * 不携带 finance_access——收款权不授予财务中心读取能力；
  * 与 billing.refund_execute 严格分离，不得合并；
  * 现金收款仍须本人 OPEN 收银班次（服务层守卫）。
"""

from django.db import migrations

CODENAME = "payment_collect"
NAME = "能登记 预订收款（财务中心与前台统一入口）"


def create_payment_collect_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label="billing", model="payment")
    Permission.objects.get_or_create(content_type=ct, codename=CODENAME, defaults={"name": NAME})


def remove_payment_collect_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct = ContentType.objects.filter(app_label="billing", model="payment").first()
    if ct is None:
        return
    Permission.objects.filter(content_type=ct, codename=CODENAME).delete()


class Migration(migrations.Migration):
    dependencies = [("billing", "0037_legacy_client_vip_archive")]

    operations = [
        migrations.RunPython(
            create_payment_collect_permission,
            remove_payment_collect_permission,
        ),
    ]
