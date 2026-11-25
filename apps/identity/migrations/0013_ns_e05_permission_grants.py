# -*- coding: utf-8 -*-
"""NS-E-05 IMPLEMENT-001：五个专项权限的岗位授权（OWNER 2026-11-24 逐项裁定）。

权限点本体由各自领域迁移创建（guests/0011 retention 三码、
guests/0012 membership_status_manage、billing/0038 payment_collect），
本迁移只承接「角色组授权」——与 identity/0004 风格一致。

授权口径（裁定 §3.3 / §4.6 / §5.1）：

| 权限 | 允许岗位 |
|---|---|
| billing.payment_collect | 前台、值班经理、财务、店长 |
| guests.membership_status_manage | 值班经理、店长 |
| guests.retention_request | 店长 |
| guests.retention_approve | 值班经理、店长 |
| guests.retention_execute | 值班经理、店长 |

禁止项（同裁定，不在本迁移授予）：
* 前台不得通过本批任何权限获得 finance_access（收款权不携带读取权）；
* 财务不得获得 membership_status_manage（资金岗不碰会员状态）；
* 前台/财务不得获得 retention_*（PII 清理仅管理岗）。
"""

from django.db import migrations

# (app_label, model, codename) -> 允许的岗位组名
GRANTS = (
    ("billing", "payment", "payment_collect", ("前台", "值班经理", "财务", "店长")),
    ("guests", "guestmembership", "membership_status_manage", ("值班经理", "店长")),
    ("guests", "guest", "retention_request", ("店长",)),
    ("guests", "guest", "retention_approve", ("值班经理", "店长")),
    ("guests", "guest", "retention_execute", ("值班经理", "店长")),
)


def _permission(apps, app_label, model, codename):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct = ContentType.objects.filter(app_label=app_label, model=model).first()
    if ct is None:
        return None
    return Permission.objects.filter(content_type=ct, codename=codename).first()


def grant_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for app_label, model, codename, group_names in GRANTS:
        perm = _permission(apps, app_label, model, codename)
        if perm is None:
            # 权限点缺失说明上游迁移未执行；静默跳过会让授权悄悄失效，
            # 故直接失败，避免"迁移成功但无人有权"的假阳性。
            raise LookupError(f"权限点不存在：{app_label}.{codename}（请先执行领域权限迁移）")
        for name in group_names:
            group = Group.objects.filter(name=name).first()
            if group is not None:
                group.permissions.add(perm)


def revoke_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for app_label, model, codename, _group_names in GRANTS:
        perm = _permission(apps, app_label, model, codename)
        if perm is None:
            continue
        for group in Group.objects.filter(permissions=perm):
            group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0012_permatrix_pending_key_mysql_safe"),
        ("guests", "0012_membership_status_permission"),
        ("billing", "0038_grant_payment_collect_permission"),
    ]

    operations = [
        migrations.RunPython(grant_permissions, revoke_permissions),
    ]
