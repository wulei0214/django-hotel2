from django.conf import settings
from django.db import migrations, models
from django.db.models import Q
import django.db.models.deletion


def create_refund_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    content_type, _ = ContentType.objects.get_or_create(app_label="billing", model="refund")
    request_permission, _ = Permission.objects.get_or_create(
        content_type=content_type, codename="refund_request",
        defaults={"name": "可以登记退款申请"},
    )
    approve_permission, _ = Permission.objects.get_or_create(
        content_type=content_type, codename="refund_approve",
        defaults={"name": "可以审批退款申请"},
    )
    for name in ("前台", "财务"):
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(request_permission)
    for name in ("值班经理", "店长"):
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(approve_permission)


def remove_refund_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type = ContentType.objects.filter(app_label="billing", model="refund").first()
    if content_type:
        Permission.objects.filter(content_type=content_type, codename__in=("refund_request", "refund_approve")).delete()


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("billing", "0002_alter_folio_closed_at_alter_folio_currency_and_more"),
        # 保证九个业务角色组已由历史统一授权迁移建立，再分配退款权限。
        ("hotelapp", "0016_unify_authorization"),
    ]

    operations = [
        migrations.CreateModel(
            name="Refund",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, verbose_name="退款金额")),
                ("currency", models.CharField(default="CNY", max_length=3, verbose_name="币种")),
                ("reason", models.CharField(max_length=240, verbose_name="退款原因")),
                ("idempotency_key", models.CharField(max_length=64, verbose_name="幂等键")),
                ("provider_ref", models.CharField(max_length=80, unique=True, verbose_name="退款登记编号")),
                ("status", models.CharField(choices=[("PENDING", "待审批"), ("SUCCEEDED", "已确认完成"), ("REJECTED", "已驳回")], default="PENDING", max_length=16, verbose_name="退款状态")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="申请时间")),
                ("processed_at", models.DateTimeField(blank=True, null=True, verbose_name="处理时间")),
                ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="approved_refunds", to=settings.AUTH_USER_MODEL, verbose_name="审批人")),
                ("payment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="refunds", to="billing.payment", verbose_name="原支付流水")),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="refunds", to="core.property", verbose_name="所属酒店")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="requested_refunds", to=settings.AUTH_USER_MODEL, verbose_name="申请人")),
            ],
            options={"verbose_name": "退款申请", "verbose_name_plural": "退款申请", "ordering": ("-created_at",), "permissions": (("refund_request", "可以登记退款申请"), ("refund_approve", "可以审批退款申请"))},
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.UniqueConstraint(fields=("property", "idempotency_key"), name="uq_refund_property_idempotency"),
        ),
        migrations.AddConstraint(
            model_name="refund",
            constraint=models.CheckConstraint(check=Q(amount__gt=0), name="ck_refund_amount_positive"),
        ),
        migrations.RunPython(create_refund_permissions, remove_refund_permissions),
    ]
