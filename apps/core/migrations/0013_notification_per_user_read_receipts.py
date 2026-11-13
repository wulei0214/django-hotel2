# -*- coding: utf-8 -*-
"""岗位广播通知的已读状态改为员工级回执。

旧 Notification.is_read/read_at 为全局字段：一个员工标记已读会影响同岗位
其他员工。新 NotificationRead 按 (notification, staff_profile) 唯一记录个人已读；
Notification.resolved_at 单独表示运营待办源业务已解决。
"""
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion
import django.utils.timezone


def migrate_legacy_reads(apps, schema_editor):
    Notification = apps.get_model("core", "Notification")
    NotificationRead = apps.get_model("core", "NotificationRead")

    # 运营待办的旧全局 is_read=True 由源业务解决自动写入，迁移为 resolved_at。
    resolved = Notification.objects.filter(is_read=True, dedupe_key__isnull=False).exclude(
        dedupe_key=""
    )
    for row in resolved.only("id", "read_at", "created_at").iterator():
        Notification.objects.filter(pk=row.pk).update(resolved_at=row.read_at or row.created_at)

    # 定向通知可以确定唯一接收员工，安全迁移其个人已读状态。
    direct_reads = Notification.objects.filter(is_read=True, recipient__isnull=False).filter(
        Q(dedupe_key__isnull=True) | Q(dedupe_key="")
    )
    for row in direct_reads.only("id", "recipient_id", "read_at", "created_at").iterator():
        NotificationRead.objects.get_or_create(
            notification_id=row.pk,
            staff_profile_id=row.recipient_id,
            defaults={"read_at": row.read_at or row.created_at},
        )

    # 岗位广播旧全局已读无法识别是哪位员工读取；不猜测接收人，
    # 不创建任何个人回执，迁移后由每位员工自行标记已读。


def keep_legacy_reads_on_reverse(apps, schema_editor):
    # 反向迁移保留新回执数据，避免丢失个人已读事实。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_night_audit_view_permission"),
        ("identity", "0006_invitation_invitation_idx_invitation_profile_status_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="notification",
            name="resolved_at",
            field=models.DateTimeField(
                blank=True,
                help_text="仅运营待办源业务解决时写入；与员工个人已读状态分离",
                null=True,
                verbose_name="待办解决时间",
            ),
        ),
        migrations.CreateModel(
            name="NotificationRead",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "read_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="个人已读时间"
                    ),
                ),
                (
                    "notification",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="read_receipts",
                        to="core.notification",
                        verbose_name="通知",
                    ),
                ),
                (
                    "staff_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="notification_reads",
                        to="identity.staffprofile",
                        verbose_name="员工档案",
                    ),
                ),
            ],
            options={
                "verbose_name": "通知个人已读回执",
                "verbose_name_plural": "通知个人已读回执",
            },
        ),
        migrations.AddIndex(
            model_name="notification",
            index=models.Index(
                fields=["property", "resolved_at", "-created_at"], name="idx_notif_prop_resolved"
            ),
        ),
        migrations.AddConstraint(
            model_name="notificationread",
            constraint=models.UniqueConstraint(
                fields=("notification", "staff_profile"), name="uq_notification_read_staff"
            ),
        ),
        migrations.AddIndex(
            model_name="notificationread",
            index=models.Index(
                fields=["staff_profile", "-read_at"], name="idx_notifread_staff_time"
            ),
        ),
        migrations.RunPython(migrate_legacy_reads, keep_legacy_reads_on_reverse),
    ]
