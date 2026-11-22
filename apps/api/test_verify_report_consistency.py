"""TASK-05 报表源数据复算工具冒烟测试（MC11-RPT-01，V3.0 任务书第一批）。

锁定 verify_report_consistency 管理命令的端到端可执行性与空期语义：
* 空账本/空库存期间 → 三项核对全 PASS、退出码 0（工具可用、不误报）；
* --business-date 单日模式参数被接受。
真实数据核对（含差异场景）由验收轮在 dev/测试库实际运行并归档日志。
"""

import json
from io import StringIO

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Property
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment


class VerifyReportConsistencyCommandTests(TestCase):
    """verify_report_consistency 命令：可执行 + 空期 PASS。"""

    def _seed_owner(self):
        # 该账号只用于角色归属，从不登录：不设口令，避免出现任何口令字面量。
        owner = User.objects.create_user(username="vc_owner")
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
        profile = StaffProfile.objects.create(
            user=owner,
            full_name="复算店长",
            phone="",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=Property.objects.get(code="DEFAULT"),
            employee_no="VC-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        group, _ = Group.objects.get_or_create(name="店长")
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now()
        )
        return owner

    def test_command_runs_and_passes_on_empty_period(self):
        self._seed_owner()
        out = StringIO()
        call_command("verify_report_consistency", "--days", "1", stdout=out)
        text = out.getvalue()
        self.assertIn("occupancy vs RoomTypeInventory", text)
        self.assertIn("revenue vs LedgerEntry", text)
        self.assertIn("adr 分子 vs ROOM_CHARGE 凭证", text)
        self.assertIn("PASS", text)

    def test_command_json_output_shape(self):
        self._seed_owner()
        out = StringIO()
        call_command(
            "verify_report_consistency",
            "--days",
            "1",
            "--business-date",
            "2026-11-22",
            "--as-json",
            stdout=out,
        )
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["result"], "PASS")
        self.assertEqual(len(summary["checks"]), 3)
        for check in summary["checks"]:
            self.assertTrue(check["passed"], check["detail"])
