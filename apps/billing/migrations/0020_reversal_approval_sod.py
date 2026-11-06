# -*- coding: utf-8 -*-
"""红字冲销改为财务申请/执行、值班经理或店长审批。"""
from django.db import migrations, models
import django.db.models.deletion


def grant_reversal_sod_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type, _ = ContentType.objects.get_or_create(
        app_label="billing", model="ledgerentry")
    execute_permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="reverse_entry",
        defaults={"name": "可以申请并执行已批准的账本红字冲销（LedgerEntry）"},
    )
    approve_permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="reversal_approve",
        defaults={"name": "可以审批账本红字冲销申请（LedgerEntry）"},
    )

    # 冲销申请与执行由财务岗位负责；经理岗位只审批，不兼任财务执行权。
    for group in Group.objects.all():
        if group.name == "财务":
            group.permissions.add(execute_permission)
        else:
            group.permissions.remove(execute_permission)

        if group.name in ("值班经理", "店长"):
            group.permissions.add(approve_permission)
        else:
            group.permissions.remove(approve_permission)


def reverse_reversal_sod_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type = ContentType.objects.filter(
        app_label="billing", model="ledgerentry").first()
    if content_type is None:
        return
    execute_permission = Permission.objects.filter(
        content_type=content_type, codename="reverse_entry").first()
    approve_permission = Permission.objects.filter(
        content_type=content_type, codename="reversal_approve").first()
    for group in Group.objects.all():
        if approve_permission:
            group.permissions.remove(approve_permission)
        if execute_permission:
            if group.name in ("财务", "值班经理", "店长"):
                group.permissions.add(execute_permission)
            else:
                group.permissions.remove(execute_permission)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0019_refund_requires_approval_record"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("hotelapp", "0014_role_actions"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="ledgerentry",
            options={
                "ordering": ("created_at", "id"),
                "verbose_name": "不可变账本凭证",
                "verbose_name_plural": "不可变账本凭证",
                "permissions": (
                    ("reverse_entry", "可以申请并执行已批准的账本红字冲销（LedgerEntry）"),
                    ("reversal_approve", "可以审批账本红字冲销申请（LedgerEntry）"),
                ),
            },
        ),
        migrations.AddField(
            model_name="ledgerentry",
            name="approval_record",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reversal_entries",
                to="billing.approvalrecord",
                verbose_name="冲销审批记录",
                help_text="PMS2 新冲销必须关联值班经理/店长已批准的审批记录；历史冲销可为空",
            ),
        ),
        migrations.RunPython(
            grant_reversal_sod_permissions,
            reverse_reversal_sod_permissions,
        ),
    ]
