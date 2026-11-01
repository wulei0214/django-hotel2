# -*- coding: utf-8 -*-
"""员工数据基线盘点（关联方案 Phase 0，只读不改库）。

导出 Staff（旧员工表）/ StaffProfile / User / Membership / RoleAssignment 的
关联现状与问题清单（重复/孤儿/未绑定），输出 CSV 报告：
    python manage.py audit_employee_data --output employee_audit.csv
"""
import csv

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.core.models import Property
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from hotelapp.models import Staff


class Command(BaseCommand):
    help = "只读盘点员工域数据关联现状（重复/孤儿/未绑定），输出 CSV（不改库）"

    def add_arguments(self, parser):
        parser.add_argument("--output", default="employee_audit.csv",
                            help="输出 CSV 路径（默认 employee_audit.csv）")

    def handle(self, *args, **options):
        User = get_user_model()
        rows = []

        # 旧 Staff 表逐条映射现状
        for s in Staff.objects.all().order_by("员工id"):
            linked_profile = StaffProfile.objects.filter(legacy_staff=s).first()
            rows.append({
                "来源": "旧Staff表",
                "标识": f"Staff#{s.员工id}",
                "姓名": s.姓名,
                "登录账号": "-",
                "档案": f"StaffProfile#{linked_profile.pk}" if linked_profile else "未映射",
                "DEFAULT任职": self._membership_state(linked_profile),
                "问题": "" if linked_profile else "旧员工未映射 StaffProfile（legacy_staff 空缺）",
            })

        # 新档案逐条关联现状
        for p in StaffProfile.objects.all().order_by("id"):
            membership = PropertyStaffMembership.objects.filter(staff_profile=p).first()
            assignments = StaffRoleAssignment.objects.filter(
                membership=membership).count() if membership else 0
            user = p.user
            problems = []
            if membership is None:
                problems.append("无任职关系（孤儿档案）")
            elif membership.property.code != "DEFAULT":
                problems.append(f"任职在非 DEFAULT 物业（{membership.property.code}）")
            if user is None:
                problems.append("未开通登录账号")
            elif not user.is_active:
                problems.append("账号已停用")
            if membership and assignments == 0 and p.employment_status == "ACTIVE":
                problems.append("在职但无任何角色授权")
            legacy_note = f"旧Staff#{p.legacy_staff.员工id}" if p.legacy_staff else "-"
            rows.append({
                "来源": "StaffProfile",
                "标识": f"Profile#{p.pk}",
                "姓名": p.full_name,
                "登录账号": user.username if user else "-",
                "档案": f"Profile#{p.pk}" + (f"（旧映射 {legacy_note}）" if p.legacy_staff else ""),
                "DEFAULT任职": self._membership_state(p),
                "问题": "；".join(problems) or "正常",
            })

        # 一人多档 / 账号多绑检查
        for user in User.objects.filter(staff_profile__isnull=False):
            if StaffProfile.objects.filter(user=user).count() > 1:
                rows.append({"来源": "检查", "标识": f"User#{user.pk}", "姓名": user.username,
                             "登录账号": user.username, "档案": "多档案",
                             "DEFAULT任职": "-", "问题": "同一账号绑定多个 StaffProfile（数据异常）"})
        for s in Staff.objects.all():
            if StaffProfile.objects.filter(legacy_staff=s).count() > 1:
                rows.append({"来源": "检查", "标识": f"Staff#{s.员工id}", "姓名": s.姓名,
                             "登录账号": "-", "档案": "多映射",
                             "DEFAULT任职": "-", "问题": "旧员工被多个档案映射（数据异常）"})

        path = options["output"]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "来源", "标识", "姓名", "登录账号", "档案", "DEFAULT任职", "问题"])
            writer.writeheader()
            writer.writerows(rows)
        problem_rows = [r for r in rows if r["问题"]]
        self.stdout.write(self.style.SUCCESS(
            f"盘点完成：共 {len(rows)} 条记录（问题 {len(problem_rows)} 条）→ {path}"))

    @staticmethod
    def _membership_state(profile):
        if profile is None:
            return "-"
        membership = PropertyStaffMembership.objects.filter(staff_profile=profile).first()
        if membership is None:
            return "无"
        return f"{membership.property.code}/{membership.status}" \
               f"（{membership.employee_no}）"
