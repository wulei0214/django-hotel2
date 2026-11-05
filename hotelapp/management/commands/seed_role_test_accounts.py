# -*- coding: utf-8 -*-
"""创建九个业务角色的专用测试账号。

仅允许本机 DEBUG 开发库或明确的专用测试库；拒绝生产命名数据库。
密码通过终端 getpass 输入，不提供固定默认密码、不写入命令行参数。
该命令只管理 test_* 专用账号，不重置历史演示账号。
"""
import getpass
import os
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.core.models import AuditEvent, Property
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
        is_sqlite_memory = "sqlite" in engine and (
            db_name in {"", ":memory:"} or "mode=memory" in db_name or "memorydb" in db_name
        )
        is_local_dev = (
            environment == "dev" and settings.DEBUG
            and db_host in local_hosts
        )
        is_test_database = (
            environment == "test"
            and (is_sqlite_memory or "test" in db_name)
        )
        looks_production = any(marker in db_name for marker in ("prod", "production", "live"))
        if looks_production or not (is_local_dev or is_test_database):
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
                elif user.is_superuser or user.is_staff:
                    raise CommandError(
                        "专用测试账号已具有平台/Django Admin 管理身份，拒绝自动降权或覆盖：%s"
                        % username
                    )
                user.is_active = True
                user.is_staff = False
                user.set_password(password)
                user.save()

                # 认证账号不承载员工岗位授权；清除历史遗留直授组并留痕。
                legacy_groups = list(user.groups.values_list("name", flat=True))
                if legacy_groups:
                    user.groups.clear()
                    AuditEvent.record(
                        action="identity.test_direct_groups_cleared",
                        entity_type="User",
                        entity_id=user.pk,
                        reason="九角色测试账号初始化：岗位授权统一由 StaffRoleAssignment 承载",
                        before={"groups": legacy_groups},
                        after={"groups": []},
                    )

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

                # 仅对专用 test_* 测试账号执行单角色收敛；历史授权只结束，不物理删除。
                now = timezone.now()
                assignments = list(
                    StaffRoleAssignment.objects.filter(
                        membership=membership
                    ).select_related("group")
                )
                if any(assignment.starts_at > now for assignment in assignments):
                    raise CommandError(
                        "测试账号存在未来生效的岗位授权，需人工核对后再初始化：%s" % username
                    )
                for assignment in assignments:
                    if (
                        assignment.group_id != group.id
                        and (assignment.ends_at is None or assignment.ends_at > now)
                    ):
                        old_end = assignment.ends_at
                        assignment.ends_at = max(
                            now, assignment.starts_at + timedelta(microseconds=1)
                        )
                        assignment.save(update_fields=["ends_at"])
                        AuditEvent.record(
                            action="identity.test_role_assignment_closed",
                            entity_type="StaffRoleAssignment",
                            entity_id=assignment.pk,
                            reason="九角色测试账号初始化：结束非目标岗位授权，保留历史授权记录",
                            before={"group": assignment.group.name, "ends_at": str(old_end)},
                            after={"group": assignment.group.name, "ends_at": assignment.ends_at.isoformat()},
                        )
                target_active = any(
                    assignment.group_id == group.id
                    and assignment.starts_at <= now
                    and (assignment.ends_at is None or assignment.ends_at > now)
                    for assignment in assignments
                )
                if not target_active:
                    StaffRoleAssignment.objects.create(
                        membership=membership, group=group,
                        starts_at=now, granted_by=None,
                    )
                self.stdout.write("已准备账号：%s（%s）" % (username, role))

        self.stdout.write(self.style.SUCCESS(
            "九角色测试账号处理完成：新建 %s 个；密码未输出。" % created_count
        ))
