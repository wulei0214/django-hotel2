# -*- coding: utf-8 -*-
"""为九个业务角色创建统一密码的测试账号（仅测试阶段使用）。

用法：
    python manage.py seed_role_test_accounts                 # 默认密码 Abc123456
    python manage.py seed_role_test_accounts --password xxx  # 自定义统一密码

行为：
  * 九角色各建一个 test_* 账号（幂等，重复执行只重置密码/组）
  * 同时把既有演示账号（front/manager/finance/owner）密码重置为统一密码，
    方便测试阶段记忆——测试结束后请立即改密或禁用
  * 每个账号补齐完整身份链（StaffProfile→Membership(DEFAULT)→RoleAssignment），
    满足 staff_required 任职门禁
  * DJANGO_ENV=prod 时拒绝执行（固定密码业务账号不允许出现在生产）
"""
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.models import Property
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

DEFAULT_PASSWORD = "Abc123456"

# 角色 → 测试账号
ROLE_TEST_ACCOUNTS = [
    ("前台", "test_front"),
    ("值班经理", "test_duty"),
    ("财务", "test_finance"),
    ("店长", "test_owner"),
    ("客房服务员", "test_housekeeping"),
    ("夜审员", "test_nightaudit"),
    ("餐饮服务员", "test_fnb"),
    ("餐饮主管", "test_fnbsup"),
    ("停车安保", "test_parking"),
]

# 既有演示账号（测试期一并统一密码）
LEGACY_DEMO_ACCOUNTS = {
    "front": "前台",
    "manager": "值班经理",
    "finance": "财务",
    "owner": "店长",
}


class Command(BaseCommand):
    help = "创建九角色统一密码测试账号（仅测试阶段；生产环境拒绝执行）"

    def add_arguments(self, parser):
        parser.add_argument("--password", default=DEFAULT_PASSWORD,
                            help="统一测试密码（默认 Abc123456）")

    def handle(self, *args, **options):
        # P1-08（方案 §9.4）：测试账号命令禁止在生产环境执行——
        # 这些账号密码公开且仅靠固定岗位组授权，生产环境创建即成后门
        from django.conf import settings as _s
        if not _s.DEBUG and getattr(_s, "DJANGO_ENV", "dev") not in {"dev", "test"}:
            raise CommandError("seed_role_test_accounts 禁止在生产环境执行（P1-08）。")
        return self._handle(*args, **options)

    def _handle(self, *args, **options):
        if os.environ.get("DJANGO_ENV", "dev").strip().lower() == "prod":
            raise CommandError("生产环境禁止创建固定密码测试账号")
        password = options["password"]
        if len(password) < 6:
            raise CommandError("密码至少 6 位")
        User = get_user_model()
        property_obj = Property.objects.filter(status="ACTIVE").order_by("id").first()
        if property_obj is None:
            raise CommandError("没有可用物业，请先初始化 DEFAULT 物业")

        created = []
        for role, username in ROLE_TEST_ACCOUNTS + [
                (role, username) for username, role in LEGACY_DEMO_ACCOUNTS.items()]:
            group = Group.objects.filter(name=role).first()
            if group is None:
                self.stdout.write(self.style.WARNING(f"跳过 {role}：角色组不存在"))
                continue
            user = User.objects.filter(username=username).first()
            if user is None:
                user = User.objects.create_user(username=username, password=password)
                created.append(username)
            # D-04：测试账号不再持有 is_staff（Admin 仅平台身份）；
            # PMS 权限由角色组 + 身份链承载
            user.is_active = True
            user.set_password(password)
            user.save()
            user.groups.set([group])  # 测试账号 = 单角色，覆盖式设置保证验证口径纯净

            profile, _ = StaffProfile.objects.get_or_create(
                user=user, defaults={"full_name": f"测试-{role}",
                                     "employment_status": "ACTIVE"})
            if not profile.employment_status:
                profile.employment_status = "ACTIVE"
                profile.save(update_fields=("employment_status",))
            membership, _ = PropertyStaffMembership.objects.get_or_create(
                property=property_obj, staff_profile=profile,
                defaults={"employee_no": f"T-{username[-6:].upper()}",
                          "position_code": role, "status": "ACTIVE"})
            StaffRoleAssignment.objects.get_or_create(
                membership=membership, group=group,
                defaults={"starts_at": timezone.now(), "granted_by": None})
            self.stdout.write(f"  ✓ {username:<18} 角色={role}")

        self.stdout.write(self.style.SUCCESS(
            f"完成：新建 {len(created)} 个，全部账号统一密码 {password}"
            "（测试结束后请立即改密或禁用）"))
