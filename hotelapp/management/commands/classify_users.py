# -*- coding: utf-8 -*-
"""AUTH-22（身份方案 2.0 §12）：历史 User 盘点与七分类。

规则版本 CLASSIFICATION_RULES_V2（2026-11-05，依据方案 §12.1）：
分类优先级：SUPERUSER_OR_TECHNICAL → CONFLICT → STAFF_LINKED →
PROFILE_ONLY → UNLINKED_USER；DISABLED / PUBLIC_REGISTERED_SUSPECT
作为异常标记与主分类并存，不覆盖主分类。

安全边界（任务书 §12.2）：
- 默认 dry-run：只统计输出，不写库。
- username 输出为 HMAC 指纹（不泄露明文）；无密码 hash/token/完整 PII。
- 正式处置（--apply-disable 等）必须 --confirm 并写 AuditEvent；
  本命令本期仅实现盘点与报告，不提供任何写操作入口。
"""
import hashlib
import json

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.identity.models import (
    PropertyStaffMembership, StaffProfile, StaffRoleAssignment)

RULES_VERSION = "CLASSIFICATION_RULES_V2"


def _fp(username):
    key = (getattr(settings, "RATE_LIMIT_HMAC_KEY", None)
           or settings.SECRET_KEY).encode("utf-8")
    return hashlib.sha256(key + str(username).encode("utf-8")).hexdigest()[:12]


class Command(BaseCommand):
    help = "历史 User 七分类盘点（AUTH-22）：默认 dry-run 只读报告"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="只读盘点（当前版本恒为只读）")
        parser.add_argument("--output", type=str, default="",
                            help="报告输出文件路径（.json/.csv），留空仅打印")
        parser.add_argument("--format", type=str, default="json",
                            choices=("json", "csv"))

    def handle(self, *args, **options):
        from django.contrib.auth.models import User
        profiles_by_user = {}
        for p in StaffProfile.objects.all():
            profiles_by_user.setdefault(p.user_id, []).append(p)
        memberships_by_profile = {}
        for m in PropertyStaffMembership.objects.select_related("property"):
            memberships_by_profile.setdefault(m.staff_profile_id, []).append(m)
        assignments_by_membership = {}
        for a in StaffRoleAssignment.objects.select_related("group"):
            assignments_by_membership.setdefault(a.membership_id, []).append(a)

        rows = []
        counts = {}
        for u in User.objects.all().order_by("id"):
            profiles = profiles_by_user.get(u.pk, [])
            memberships = []
            for p in profiles:
                memberships += memberships_by_profile.get(p.pk, [])
            now = timezone.now()
            active_memberships = [
                m for p in profiles
                if p.employment_status == StaffProfile.EmploymentStatus.ACTIVE
                for m in memberships_by_profile.get(p.pk, [])
                if m.status == PropertyStaffMembership.Status.ACTIVE
                and m.property.status == "ACTIVE"
            ]
            has_active_membership = bool(active_memberships)
            has_assignment = any(
                assignment.starts_at <= now
                and (assignment.ends_at is None or assignment.ends_at > now)
                for m in active_memberships
                for assignment in assignments_by_membership.get(m.pk, [])
            )
            conflict = len(profiles) > 1
            disabled = not u.is_active

            if u.is_superuser or u.is_staff:
                category = "SUPERUSER_OR_TECHNICAL"
            elif conflict:
                category = "CONFLICT"
            elif not profiles:
                category = "UNLINKED_USER"
            elif has_active_membership and has_assignment:
                category = "STAFF_LINKED"
            else:
                category = "PROFILE_ONLY"

            flags = []
            if disabled:
                flags.append("DISABLED")
            # F-02 检测：User.groups 直授（不经 StaffRoleAssignment 的授权旁路）
            direct_groups = list(u.groups.values_list("name", flat=True))
            if direct_groups:
                flags.append("DIRECT_GROUP_GRANTS:" + ",".join(direct_groups))
            if category == "UNLINKED_USER" and not u.is_superuser:
                # 旧公开注册通道嫌疑（AUTH-01 之前创建的孤儿账号线索）
                flags.append("PUBLIC_REGISTERED_SUSPECT")
            counts[category] = counts.get(category, 0) + 1
            rows.append({
                "user_id": u.pk,
                "username_fp": _fp(u.username),
                "category": category,
                "flags": flags,
                "is_active": u.is_active,
                "is_superuser": u.is_superuser,
                "is_staff": u.is_staff,
                "profile_ids": [p.pk for p in profiles],
                "membership_ids": [m.pk for m in memberships],
                "positions": [m.position_code for m in memberships],
                "assignment_count": sum(
                    len(assignments_by_membership.get(m.pk, []))
                    for m in memberships),
                "direct_group_grants": direct_groups,
                "rules_version": RULES_VERSION,
            })

        summary = {
            "rules_version": RULES_VERSION,
            "total_users": len(rows),
            "counts": counts,
        }
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        out_path = options["output"]
        if out_path:
            if options["format"] == "csv":
                import csv
                with open(out_path, "w", newline="", encoding="utf-8") as fh:
                    writer = csv.DictWriter(fh, fieldnames=[
                        "user_id", "username_fp", "category", "flags",
                        "is_active", "is_superuser", "is_staff", "profile_ids",
                        "membership_ids", "positions", "assignment_count",
                        "direct_group_grants", "rules_version",
                    ])
                    writer.writeheader()
                    writer.writerows(rows)
            else:
                with open(out_path, "w", encoding="utf-8") as fh:
                    json.dump({"summary": summary, "users": rows}, fh,
                              ensure_ascii=False, indent=2)
            self.stdout.write("明细已写入 %s" % out_path)
        self.stdout.write(self.style.SUCCESS(
            "AUTH-22 盘点完成（只读；本期不提供任何写处置入口）"))
