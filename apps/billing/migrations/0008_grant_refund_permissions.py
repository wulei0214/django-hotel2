# -*- coding: utf-8 -*-
"""P1-02 配套（方案 2.0 §9.2）：退款权限码正式入矩阵。

此前 billing.refund_request / billing.refund_approve 只在代码中检查、
从未授予任何组——审批链路对全部角色失效。本迁移按既有审批语义补授：

- billing.refund_request  发起退款申请 → 前台、财务
- billing.refund_approve  审批退款     → 值班经理、店长

等价性说明：财务明细查看（hotelapp.finance_access）不含审批权；
审批不依赖 finance_access。回滚：reverse 移除上述组授权（权限点保留）。

注意：migration 内统一使用 historical models；混用当前模型会导致
M2M 实例类型校验失败（ValueError: Must be "ContentType" instance）。
"""
from django.db import migrations


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(
        app_label="billing", model="refund")
    request_perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename="refund_request",
        defaults={"name": "能发起退款申请"})
    approve_perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename="refund_approve",
        defaults={"name": "能审批退款"})
    for name in ("前台", "财务"):
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(request_perm)
    for name in ("值班经理", "店长"):
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(approve_perm)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(
        app_label="billing", model="refund")
    for codename in ("refund_request", "refund_approve"):
        try:
            perm = Permission.objects.get(
                content_type=ct, codename=codename)
        except Permission.DoesNotExist:
            continue
        for group in Group.objects.all():
            group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0007_approval_record"),
        ("hotelapp", "0018_finance_view_staff_readonly"),
    ]
    operations = [migrations.RunPython(grant, revoke)]
