"""NA-13（任务书 V3.0 TASK-06）：夜审员岗位激活——授予常规夜审权限。

负责人裁定（2026-11-22）：夜审员为现役岗位，授
  * core.night_audit_view        —— 夜审中心页面门禁（查看/预览）
  * hotelapp.night_audit_execute —— 夜审执行（常规过租/关账）
不授予营业日重开（business_date_reopen）、Break-glass、退款审批等其他
管理例外——店长/值班经理保留全部例外权限（0014/0016 不变）。

幂等：逐项 add 前查重，可重复执行；亦不回收任何既有权限（回滚走矩阵页）。
"""

from django.db import migrations

GRANTED = (
    ("core", "night_audit_view"),
    ("hotelapp", "night_audit_execute"),
)


def grant_night_auditor(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _created = Group.objects.get_or_create(name="夜审员")
    for app_label, codename in GRANTED:
        permission = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is not None and not group.permissions.filter(pk=permission.pk).exists():
            group.permissions.add(permission)


def ungrant_night_auditor(apps, schema_editor):
    # Add-only 授权迁移（与 core.0012 同哲学）：回滚不撤销可能已由平台
    # 权限中心重新审核的组授权；如需收回夜审员权限走矩阵页 L3 流程。
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0020_restore_room_release_ready_execution"),
        ("core", "0012_night_audit_view_permission"),
    ]

    operations = [
        migrations.RunPython(grant_night_auditor, ungrant_night_auditor),
    ]
