# -*- coding: utf-8 -*-
"""安全收敛本地/专用测试环境的历史账号授权残留。

只做可逆处置：
- 移除普通账号上的 User.groups 直授（保留 StaffRoleAssignment）；
- 对没有 StaffProfile 的普通账号执行停用，不物理删除；
- 不删除员工档案、任职、岗位授权、订单、账务、审批或审计数据。

默认只预览。执行必须明确数据库名并提供已完成备份文件。
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.core.models import AuditEvent
from apps.identity.models import StaffProfile


class Command(BaseCommand):
    help = "预览/安全收敛本地测试账号残留（不删除业务和审计数据）"

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="只预览，不写库（默认）")
        mode.add_argument("--execute", action="store_true", help="执行可逆账号收敛")
        parser.add_argument("--confirm-database", default="", help="必须与当前数据库名完全一致")
        parser.add_argument("--backup-file", default="", help="已完成数据库备份文件的绝对/相对路径")

    def _check_target(self):
        env = str(getattr(settings, "DJANGO_ENV", "")).strip().lower()
        db = connection.settings_dict
        engine = str(db.get("ENGINE", "")).lower()
        name = str(db.get("NAME", ""))
        host = str(db.get("HOST", "")).lower()
        local_hosts = {"", "localhost", "127.0.0.1", "::1"}
        sqlite_memory = (
            "sqlite" in engine and (
                name in {"", ":memory:"} or "mode=memory" in name or "memorydb" in name
            )
        )
        local_dev = env == "dev" and settings.DEBUG and host in local_hosts
        dedicated_test = env == "test" and (sqlite_memory or "test" in name.lower())
        if not (local_dev or dedicated_test):
            raise CommandError(
                "拒绝执行：仅允许本机 DEBUG 开发数据库或明确标识的专用测试数据库。"
            )
        return name

    def handle(self, *args, **options):
        db_name = self._check_target()
        User = get_user_model()
        users = list(User.objects.all().order_by("pk"))
        profiles = set(
            StaffProfile.objects.exclude(user_id__isnull=True)
            .values_list("user_id", flat=True)
        )
        planned = []
        for user in users:
            if user.is_superuser or user.is_staff:
                continue
            group_names = list(user.groups.values_list("name", flat=True))
            orphan = user.pk not in profiles
            if group_names or (orphan and user.is_active):
                planned.append({
                    "user": user,
                    "groups": group_names,
                    "disable": orphan and user.is_active,
                })

        self.stdout.write("数据库：%s" % db_name)
        self.stdout.write("扫描账号：%s" % len(users))
        self.stdout.write("待移除 User.groups 直授：%s 个账号" % sum(bool(x["groups"]) for x in planned))
        self.stdout.write("待停用无员工档案账号：%s 个账号" % sum(x["disable"] for x in planned))
        self.stdout.write("保留：超级管理员/平台 staff、StaffProfile、任职、StaffRoleAssignment、业务与审计历史。")

        if not options["execute"]:
            self.stdout.write(self.style.WARNING("当前为只读预览；未修改任何数据库记录。"))
            return

        if not db_name or options["confirm_database"] != db_name:
            raise CommandError("--confirm-database 必须非空且与当前数据库名称完全一致。")
        backup = Path(options["backup_file"]).expanduser()
        if not options["backup_file"] or not backup.is_file() or backup.stat().st_size <= 0:
            raise CommandError("执行前必须提供存在且非空的数据库备份文件 --backup-file。")

        changed_groups = 0
        disabled_users = 0
        with transaction.atomic():
            for item in planned:
                user = item["user"]
                if item["groups"]:
                    user.groups.clear()
                    changed_groups += 1
                    AuditEvent.record(
                        action="identity.legacy_direct_groups_cleared",
                        entity_type="User",
                        entity_id=user.pk,
                        reason="F-02 本地/测试数据治理：移除 User.groups 直授，保留 StaffRoleAssignment",
                        before={"groups": item["groups"]},
                        after={"groups": []},
                    )
                if item["disable"]:
                    user.is_active = False
                    user.save(update_fields=["is_active"])
                    disabled_users += 1
                    AuditEvent.record(
                        action="identity.orphan_account_disabled",
                        entity_type="User",
                        entity_id=user.pk,
                        reason="F-02 本地/测试数据治理：账号无 StaffProfile，停用而非物理删除",
                        before={"is_active": True},
                        after={"is_active": False},
                    )

        self.stdout.write(self.style.SUCCESS(
            "收敛完成：移除直授账号 %s 个；停用孤儿账号 %s 个；未删除任何历史业务或审计数据。"
            % (changed_groups, disabled_users)
        ))
