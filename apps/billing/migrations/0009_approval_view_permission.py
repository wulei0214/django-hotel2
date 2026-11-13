# -*- coding: utf-8 -*-
"""P0-01 配套：审批记录只读权限码 billing.approval_view 入矩阵。

此前 /api/v1/approvals 列表仅要求“登录 + 物业上下文”，匿名请求在 DEFAULT
物业上下文下即可探测审批记录。本迁移创建专项只读权限点并按审批相关角色授予：

- billing.approval_view  查看审批记录列表 → 店长、值班经理、财务

边界：前台/收银等只发起不审批的岗位不授予；可见范围另行按“是否持有任一
注册动作的审批权限”收敛（无审批权者仅见本人发起的记录）。
回滚：reverse 移除上述组授权（权限点保留，与 0008 口径一致）。

注意：migration 内统一使用 historical models / get_or_create，可重复执行。
"""
from django.db import migrations


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label="billing", model="approvalrecord")
    perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename="approval_view", defaults={"name": "能查看高危审批记录"}
    )
    for name in ("店长", "值班经理", "财务"):
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(perm)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label="billing", model="approvalrecord")
    try:
        perm = Permission.objects.get(content_type=ct, codename="approval_view")
    except Permission.DoesNotExist:
        return
    for group in Group.objects.all():
        group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0008_grant_refund_permissions"),
    ]
    operations = [migrations.RunPython(grant, revoke)]
