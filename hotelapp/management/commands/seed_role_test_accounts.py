# -*- coding: utf-8 -*-
"""创建九个业务角色的专用测试账号。

仅允许 DEBUG=True 且 DJANGO_ENV=dev，或明确 DJANGO_ENV=test。
密码通过终端 getpass 输入，不提供固定默认密码、不写入命令行参数。
该命令只管理 test_* 专用账号，不重置历史演示账号。
"""
import getpass
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.core.models import Property
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)

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


class Command(BaseCommand):
    help = "创建/更新九角色专用测试账号（禁止生产环境执行）"

    def handle(self, *args, **options):
        environment = str(
            getattr(settings, "DJANGO_ENV", None)
            or os.environ.get("DJANGO_ENV", "")
        ).strip().lower()

        db = connection.settings_dict
        engine = str(db.get("ENGINE", "")).lower()
        db_name = str(db.get("NAME", "")).lower()
        db_host = str(db.get("HOST", "")).lower()
        local_hosts = {"", "localhost", "127.0.0.1", "::1"}
        is_sqlite_memory = "sqlite" in engine and db_name in {"", ":memory:"}
        is_local_dev = (
            environment == "dev" and settings.DEBUG
            and db_host in local_hosts
        )
        is_test_database = (
            environment == "test"
            and (is_sqlite_memory or "test" in db_name)
        )
        if not (is_local_dev or is_test_database):
            raise CommandError(
                "拒绝执行：仅允许 DEBUG=True、DJANGO_ENV=dev 且数据库主机为本机，"
                "或 DJANGO_ENV=test 且数据库名称明确包含 test（SQLite 内存库除外）。"
            )

        password = getpass.getpass("请输入九角色测试账号统一密码（不回显）：")
        confirmation = getpass.getpass("请再次输入密码确认：")
        if not password or password != confirmation:
            raise CommandError("密码为空或两次输入不一致。")

        User = get_user_model()
        probe_user = User(username="pms-role-test-password-validation")
        try:
            validate_password(password, user=probe_user)
        except Exception as exc:
            raise CommandError("密码未通过 Django 密码强度校验：%s" % exc)

        property_obj = Property.objects.filter(status="ACTIVE").order_by("id").first()
        if property_obj is None:
            raise CommandError("没有可用物业，拒绝创建测试员工任职。")

        created_count = 0
        with transaction.atomic():
            for role, username in ROLE_TEST_ACCOUNTS:
                group = Group.objects.filter(name=role).first()
                if group is None:
                    raise CommandError("角色组不存在，已回滚：%s" % role)

                user, created = User.objects.get_or_create(username=username)
                if created:
                    created_count += 1
                user.is_active = True
                user.is_staff = False
                user.set_password(password)
                user.save()

                # 认证账号不承载员工岗位授权；清除历史遗留直授组。
                user.groups.clear()

                profile, _ = StaffProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        "full_name": "测试-%s" % role,
                        "employment_status": StaffProfile.EmploymentStatus.ACTIVE,
                    },
                )
                if profile.employment_status != StaffProfile.EmploymentStatus.ACTIVE:
                    profile.employment_status = StaffProfile.EmploymentStatus.ACTIVE
                    profile.save(update_fields=["employment_status", "updated_at"])

                membership, _ = PropertyStaffMembership.objects.get_or_create(
                    property=property_obj,
                    staff_profile=profile,
                    defaults={
                        "employee_no": "T-%s" % username.upper()[-20:],
                        "position_code": role,
                        "status": PropertyStaffMembership.Status.ACTIVE,
                    },
                )
                if membership.status != PropertyStaffMembership.Status.ACTIVE:
                    membership.status = PropertyStaffMembership.Status.ACTIVE
                    membership.save(update_fields=["status", "updated_at"])

                # 仅对专用 test_* 测试账号执行单角色收敛。
                StaffRoleAssignment.objects.filter(
                    membership=membership
                ).exclude(group=group).delete()
                StaffRoleAssignment.objects.get_or_create(
                    membership=membership,
                    group=group,
                    defaults={"starts_at": timezone.now(), "granted_by": None},
                )
                self.stdout.write("已准备账号：%s（%s）" % (username, role))

        self.stdout.write(self.style.SUCCESS(
            "九角色测试账号处理完成：新建 %s 个；密码未输出。" % created_count
        ))
