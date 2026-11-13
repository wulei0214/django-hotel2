# -*- coding: utf-8 -*-
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def grant_review_permission(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    content_type, _ = ContentType.objects.get_or_create(
        app_label="core", model="businessdate")
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type, codename="breakglass_review",
        defaults={"name": "可以复核紧急营业日重开"})
    for group in Group.objects.filter(name__in=("值班经理", "店长")):
        group.permissions.add(permission)


def revoke_review_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    try:
        ct = ContentType.objects.get(app_label="core", model="businessdate")
    except ContentType.DoesNotExist:
        return
    Permission.objects.filter(
        content_type=ct, codename="breakglass_review").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_businessdate_open_unique_key"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="BreakGlassRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name="ID")),
                ("action_code", models.CharField(default="night_audit.reopen",
                    editable=False, max_length=64, verbose_name="授权动作")),
                ("reason", models.CharField(max_length=240, verbose_name="紧急原因")),
                ("password_verified_at", models.DateTimeField(verbose_name="二次密码验证时间")),
                ("expires_at", models.DateTimeField(verbose_name="授权失效时间")),
                ("status", models.CharField(choices=[
                    ("GRANTED", "已授权待执行"), ("USED", "已执行待复核"),
                    ("EXPIRED", "已过期"), ("REVIEWED", "已完成复核")],
                    default="GRANTED", max_length=12, verbose_name="状态")),
                ("used_at", models.DateTimeField(blank=True, null=True, verbose_name="执行时间")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True, verbose_name="复核时间")),
                ("review_decision", models.CharField(blank=True, choices=[
                    ("CONFIRMED", "复核确认"), ("REJECTED", "复核有异议")],
                    max_length=12, verbose_name="复核结论")),
                ("review_reason", models.CharField(blank=True, max_length=240,
                                                   verbose_name="复核说明")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="申请时间")),
                ("business_date", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="break_glass_records", to="core.businessdate",
                    verbose_name="目标营业日")),
                ("property", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="break_glass_records", to="core.property",
                    verbose_name="物业")),
                ("requested_by", models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="break_glass_requested", to=settings.AUTH_USER_MODEL,
                    verbose_name="紧急操作人")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="break_glass_reviewed", to=settings.AUTH_USER_MODEL,
                    verbose_name="事后复核人")),
            ],
            options={
                "verbose_name": "营业日紧急重开授权",
                "verbose_name_plural": "营业日紧急重开授权",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="breakglassrecord",
            index=models.Index(fields=["property", "status", "expires_at"],
                               name="idx_breakglass_prop_status"),
        ),
        migrations.AddIndex(
            model_name="breakglassrecord",
            index=models.Index(fields=["business_date", "created_at"],
                               name="idx_breakglass_date_created"),
        ),
        migrations.AlterModelOptions(
            name="businessdate",
            options={
                "verbose_name": "营业日",
                "verbose_name_plural": "营业日",
                "permissions": [
                    ("breakglass_review", "可以复核紧急营业日重开"),
                ],
            },
        ),
        migrations.RunPython(grant_review_permission, revoke_review_permission),
    ]
