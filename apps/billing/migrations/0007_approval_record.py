# -*- coding: utf-8 -*-
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0006_mysql_nullable_unique_keys"),
        ("core", "0006_businessdate_open_unique_key"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ApprovalRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("action_code", models.CharField(max_length=64, verbose_name="动作代码")),
                ("entity_type", models.CharField(max_length=64, verbose_name="目标实体类型")),
                ("entity_id", models.CharField(max_length=64, verbose_name="目标实体ID")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "待审批"),
                            ("APPROVED", "已批准"),
                            ("REJECTED", "已驳回"),
                            ("EXPIRED", "已过期"),
                            ("CANCELLED", "已撤销"),
                        ],
                        default="PENDING",
                        max_length=12,
                        verbose_name="审批状态",
                    ),
                ),
                ("reason", models.CharField(max_length=240, verbose_name="申请原因")),
                (
                    "payload_snapshot",
                    models.JSONField(blank=True, null=True, verbose_name="申请参数快照"),
                ),
                (
                    "idempotency_key",
                    models.CharField(max_length=64, unique=True, verbose_name="幂等键"),
                ),
                (
                    "approved_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="审批时间"),
                ),
                (
                    "expires_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="过期时间"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="申请时间")),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approvals_approved",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="审批人",
                    ),
                ),
                (
                    "property",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approval_records",
                        to="core.property",
                        verbose_name="物业",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="approvals_requested",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="申请人",
                    ),
                ),
            ],
            options={
                "verbose_name": "高危审批记录",
                "verbose_name_plural": "高危审批记录",
                "ordering": ("-created_at",),
            },
        ),
        migrations.AddIndex(
            model_name="approvalrecord",
            index=models.Index(
                fields=["property", "action_code", "status"], name="idx_approval_prop_action"
            ),
        ),
        migrations.AddIndex(
            model_name="approvalrecord",
            index=models.Index(fields=["entity_type", "entity_id"], name="idx_approval_entity"),
        ),
        migrations.AddField(
            model_name="refund",
            name="approval_record",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="refunds",
                to="billing.approvalrecord",
                verbose_name="关联审批记录",
            ),
        ),
    ]
