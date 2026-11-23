# TASK-05：通用 Outbox + Django Worker——OutboxEvent 模型 + (status, next_attempt_at) 索引。
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0019_export_credential"),
    ]

    operations = [
        migrations.CreateModel(
            name="OutboxEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("event_type", models.CharField(max_length=64, verbose_name="事件类型")),
                ("aggregate_type", models.CharField(max_length=64, verbose_name="聚合类型")),
                ("aggregate_id", models.CharField(max_length=64, verbose_name="聚合标识")),
                ("payload", models.JSONField(default=dict, verbose_name="事件载荷")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "待处理"),
                            ("PROCESSING", "处理中"),
                            ("SUCCEEDED", "已成功"),
                            ("RETRY", "等待重试"),
                            ("DEAD", "死信"),
                        ],
                        default="PENDING",
                        max_length=16,
                        verbose_name="处理状态",
                    ),
                ),
                ("attempt_count", models.IntegerField(default=0, verbose_name="已尝试次数")),
                ("max_attempts", models.IntegerField(default=5, verbose_name="最大尝试次数")),
                (
                    "next_attempt_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="下次可认领时间"),
                ),
                (
                    "locked_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="租约锁定时间"),
                ),
                (
                    "lease_owner",
                    models.CharField(
                        blank=True, default="", max_length=128, verbose_name="租约持有者"
                    ),
                ),
                ("last_error", models.TextField(blank=True, default="", verbose_name="最近错误")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "processed_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="完成时间"),
                ),
            ],
            options={
                "verbose_name": "Outbox 事件",
                "verbose_name_plural": "Outbox 事件",
                "ordering": ("id",),
            },
        ),
        migrations.AddIndex(
            model_name="outboxevent",
            index=models.Index(fields=["status", "next_attempt_at"], name="idx_outbox_status_next"),
        ),
    ]
