import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q


ROLE_PERMISSION_MODELS = {
    "FRONT_DESK": {
        "client": ("view", "add", "change"),
        "vip": ("view", "add", "change"),
        "order": ("view", "add", "change"),
        "accommodation": ("view", "add", "change"),
        "park": ("view", "add", "change"),
        "food": ("view",),
        "room": ("view",),
    },
    "DUTY_MANAGER": {
        "client": ("view", "add", "change"),
        "vip": ("view", "add", "change"),
        "order": ("view", "add", "change", "delete"),
        "accommodation": ("view", "add", "change", "delete"),
        "park": ("view", "add", "change"),
        "food": ("view",),
        "room": ("view", "change"),
    },
    "FINANCE": {
        "client": ("view",),
        "vip": ("view",),
        "order": ("view",),
        "accommodation": ("view",),
        "food": ("view",),
        "room": ("view",),
        "park": ("view",),
    },
    "PROPERTY_OWNER": {
        "staff": ("view", "add", "change"),
        "client": ("view", "add", "change"),
        "food": ("view", "add", "change"),
        "vip": ("view", "add", "change"),
        "park": ("view", "add", "change"),
        "room": ("view", "add", "change"),
        "order": ("view", "add", "change"),
        "accommodation": ("view", "add", "change"),
    },
    "HOUSEKEEPING": {
        "room": ("view",),
    },
    "NIGHT_AUDIT": {
        "accommodation": ("view",),
        "nightaudit": ("view",),
        "folio": ("view",),
        "financerecord": ("view",),
    },
}


def seed_default_roles_and_legacy_staff(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Property = apps.get_model("core", "Property")
    Staff = apps.get_model("hotelapp", "Staff")
    StaffProfile = apps.get_model("identity", "StaffProfile")
    Membership = apps.get_model("identity", "PropertyStaffMembership")

    property_obj, _ = Property.objects.get_or_create(
        code="DEFAULT",
        defaults={
            "name": "默认酒店",
            "timezone": "Asia/Shanghai",
            "currency": "CNY",
            "status": "ACTIVE",
        },
    )

    for role_name, model_permissions in ROLE_PERMISSION_MODELS.items():
        group, _ = Group.objects.get_or_create(name=role_name)
        for model_name, actions in model_permissions.items():
            content_type, _ = ContentType.objects.get_or_create(
                app_label="hotelapp", model=model_name
            )
            for action in actions:
                codename = f"{action}_{model_name}"
                permission, _ = Permission.objects.get_or_create(
                    content_type=content_type,
                    codename=codename,
                    defaults={"name": f"Can {action} {model_name}"},
                )
                group.permissions.add(permission)

    # 旧 Staff 数据先转成员工档案和默认物业任职关系。
    # 账号归属无法从当前仓库可靠推断，必须由管理员显式绑定。
    for legacy in Staff.objects.all().iterator():
        profile, _ = StaffProfile.objects.get_or_create(
            legacy_staff_id=legacy.pk,
            defaults={
                "full_name": getattr(legacy, "姓名", "") or f"员工{legacy.pk}",
                "phone": "",
                "employment_status": "ACTIVE",
            },
        )
        Membership.objects.get_or_create(
            property_id=property_obj.pk,
            staff_profile_id=profile.pk,
            defaults={
                "employee_no": str(legacy.pk),
                "position_code": getattr(legacy, "职务", "") or "UNASSIGNED",
                "status": "ACTIVE",
            },
        )


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("core", "0001_initial"),
        ("hotelapp", "0009_folio"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffProfile",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("full_name", models.CharField(max_length=80, verbose_name="员工姓名")),
                ("phone", models.CharField(blank=True, max_length=20, verbose_name="手机号")),
                (
                    "employment_status",
                    models.CharField(
                        choices=[("ACTIVE", "在职"), ("INACTIVE", "停用"), ("TERMINATED", "离职")],
                        default="ACTIVE",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "legacy_staff",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pms_staff_profile",
                        to="hotelapp.staff",
                        verbose_name="旧版员工记录",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staff_profile",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="登录账号",
                    ),
                ),
            ],
            options={
                "verbose_name": "员工档案",
                "verbose_name_plural": "员工档案",
                "ordering": ("id",),
            },
        ),
        migrations.CreateModel(
            name="PropertyStaffMembership",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("employee_no", models.CharField(max_length=32, verbose_name="店内工号")),
                ("position_code", models.CharField(default="UNASSIGNED", max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ACTIVE", "在职"),
                            ("SUSPENDED", "暂停"),
                            ("TERMINATED", "已离职"),
                        ],
                        default="ACTIVE",
                        max_length=16,
                    ),
                ),
                ("started_at", models.DateField(default=django.utils.timezone.localdate)),
                ("ended_at", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "property",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="staff_memberships",
                        to="core.property",
                    ),
                ),
                (
                    "staff_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="property_memberships",
                        to="identity.staffprofile",
                    ),
                ),
            ],
            options={
                "verbose_name": "员工酒店任职关系",
                "verbose_name_plural": "员工酒店任职关系",
                "ordering": ("property_id", "employee_no"),
            },
        ),
        migrations.CreateModel(
            name="StaffRoleAssignment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("starts_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("ends_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "granted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="granted_pms_roles",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "group",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="pms_role_assignments",
                        to="auth.group",
                    ),
                ),
                (
                    "membership",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="role_assignments",
                        to="identity.propertystaffmembership",
                    ),
                ),
            ],
            options={
                "verbose_name": "员工岗位授权",
                "verbose_name_plural": "员工岗位授权",
                "ordering": ("membership_id", "group_id", "starts_at"),
            },
        ),
        migrations.AddConstraint(
            model_name="propertystaffmembership",
            constraint=models.UniqueConstraint(
                fields=("property", "employee_no"), name="uq_membership_property_employee_no"
            ),
        ),
        migrations.AddConstraint(
            model_name="propertystaffmembership",
            constraint=models.UniqueConstraint(
                fields=("property", "staff_profile"), name="uq_membership_property_staff"
            ),
        ),
        migrations.AddConstraint(
            model_name="propertystaffmembership",
            constraint=models.CheckConstraint(
                check=Q(ended_at__isnull=True) | Q(ended_at__gte=F("started_at")),
                name="ck_membership_valid_dates",
            ),
        ),
        migrations.AddConstraint(
            model_name="staffroleassignment",
            constraint=models.UniqueConstraint(
                fields=("membership", "group", "starts_at"), name="uq_staff_role_assignment_start"
            ),
        ),
        migrations.AddConstraint(
            model_name="staffroleassignment",
            constraint=models.CheckConstraint(
                check=Q(ends_at__isnull=True) | Q(ends_at__gt=F("starts_at")),
                name="ck_staff_role_assignment_valid_period",
            ),
        ),
        migrations.RunPython(seed_default_roles_and_legacy_staff, migrations.RunPython.noop),
    ]
