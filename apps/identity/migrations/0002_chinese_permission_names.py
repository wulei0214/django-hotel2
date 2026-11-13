# -*- coding: utf-8 -*-
"""将 hotelapp 自动生成的英文权限名称改为中文（后台「组」编辑器展示用）。

以 codename（动作_模型）为权威来源解析，避免依赖历史 name 字段的英文残留。
"""
from django.db import migrations

ACTION_LABELS = {"view": "查看", "add": "新增", "change": "修改", "delete": "删除"}
MODEL_LABELS = {
    "staff": "员工信息",
    "client": "顾客管理",
    "food": "菜品信息",
    "vip": "会员信息",
    "park": "车位信息",
    "room": "客房信息",
    "order": "餐饮订单",
    "accommodation": "客房订单",
    "auditlog": "审计日志",
}


def chinese_permission_names(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    for p in Permission.objects.filter(content_type__app_label="hotelapp"):
        action, _, model = p.codename.partition("_")
        if action in ACTION_LABELS and model in MODEL_LABELS:
            p.name = f"能{ACTION_LABELS[action]} {MODEL_LABELS[model]}"
            p.save(update_fields=["name"])


def restore_english_names(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    for p in Permission.objects.filter(content_type__app_label="hotelapp"):
        action, _, model = p.codename.partition("_")
        if action in ACTION_LABELS and model in MODEL_LABELS:
            p.name = f"Can {action} {model}"
            p.save(update_fields=["name"])


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(chinese_permission_names, restore_english_names),
    ]
