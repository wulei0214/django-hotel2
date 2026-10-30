# -*- coding: utf-8 -*-
"""将 hotelapp 自动生成的英文权限名称改为中文（后台「组」编辑器展示用）。"""
from django.db import migrations

ACTION_LABELS = {"view": "查看", "add": "新增", "change": "修改", "delete": "删除"}


def chinese_permission_names(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    for p in Permission.objects.filter(content_type__app_label="hotelapp"):
        for en, zh in ACTION_LABELS.items():
            prefix = "Can " + en
            if p.name.startswith(prefix):
                model_name = p.name[len(prefix):].strip()
                p.name = f"能{zh} {model_name}"
                p.save(update_fields=["name"])
                break


def restore_english_names(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    reverse_labels = {v: k for k, v in ACTION_LABELS.items()}
    for p in Permission.objects.filter(content_type__app_label="hotelapp"):
        for zh, en in reverse_labels.items():
            prefix = "能" + zh
            if p.name.startswith(prefix):
                model_name = p.name[len(prefix):].strip()
                p.name = f"Can {en} {model_name}"
                p.save(update_fields=["name"])
                break


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(chinese_permission_names, restore_english_names),
    ]
