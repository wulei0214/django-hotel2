# -*- coding: utf-8 -*-
"""红字冲销改为财务申请/执行、值班经理或店长审批。"""
from django.db import migrations, models
import django.db.models.deletion


def mark_legacy_reversals(apps, schema_editor):
    LedgerEntry = apps.get_model("billing", "LedgerEntry")
    LedgerEntry._base_manager.filter(
        entry_type="REVERSAL", approval_record__isnull=True
    ).update(legacy_workflow=True)


def unmark_legacy_reversals(apps, schema_editor):
    LedgerEntry = apps.get_model("billing", "LedgerEntry")
    LedgerEntry._base_manager.filter(
        entry_type="REVERSAL", legacy_workflow=True
    ).update(legacy_workflow=False)


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
    if execute_permission.name != "可以申请并执行已批准的账本红字冲销（LedgerEntry）":
        execute_permission.name = "可以申请并执行已批准的账本红字冲销（LedgerEntry）"
        execute_permission.save(update_fields=["name"])
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
    if execute_permission:
        execute_permission.name = "可以红字冲销账本凭证（LedgerEntry）"
        execute_permission.save(update_fields=["name"])
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
        migrations.AddField(
            model_name="ledgerentry",
            name="legacy_workflow",
            field=models.BooleanField(
                default=False,
                verbose_name="历史旧流程冲销",
                help_text="仅迁移前无法证明审批关联的历史红字凭证为 True；新冲销必须为 False",
            ),
        ),
        migrations.RunPython(mark_legacy_reversals, unmark_legacy_reversals),
        migrations.AddConstraint(
            model_name="ledgerentry",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(entry_type="REVERSAL", legacy_workflow=True,
                             approval_record__isnull=True)
                    | models.Q(entry_type="REVERSAL", legacy_workflow=False,
                               approval_record__isnull=False,
                               void_of__isnull=False, void_reason__gt="")
                    | (~models.Q(entry_type="REVERSAL") & models.Q(
                        legacy_workflow=False, approval_record__isnull=True,
                        void_of__isnull=True))
                ),
                name="ck_ledger_reversal_approval_link",
            ),
        ),
        migrations.RunPython(
            grant_reversal_sod_permissions,
            reverse_reversal_sod_permissions,
        ),
    ]
