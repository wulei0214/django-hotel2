"""TASK-14 报表源数据复算工具测试（MC11-RPT-01，V3.0 TASK-05 / V3.2 TASK-14）。

覆盖两类断言：
* 端到端一致：真实种子数据（LedgerEntry / RoomTypeInventory）经真实报表视图
  复算 → 全 PASS、退出码 0（含房费红字冲销、主币种过滤、空期间）；
* 差异可检出：用合成报表载荷（mock 报表视图返回值）驱动差异引擎，锁定
  任务书要求的异常场景——报表收入多/少、缺行、多行、ADR 间夜不一致、
  Occupancy 比率不一致、源日历缺失、非法日期、超范围——失败时 JSON 可解析、
  差异携带名称/日期/报表值/源表值、退出码非零。

复算逻辑独立实现（不调用 report_center 内部计算函数）；mock 只替换报表
视图的 HTTP 返回载荷，不触碰复算与比对代码路径。
"""

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry
from apps.core.models import Property
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.inventory.models import RoomType, RoomTypeInventory
from apps.reservations.models import Reservation

# 核对窗口固定营业日（远离"今天"，避免依赖运行时刻）
BD = "2026-06-01"


class _FakeResponse:
    """run_report 只读 status_code 与 content，最小桩即可。"""

    status_code = 200

    def __init__(self, data):
        self.content = json.dumps({"data": data}).encode("utf-8")


def _occ_payload(series, summary=None):
    return {
        "series": series,
        "summary": summary
        or {
            "sellable_room_nights": sum(r.get("sellable_room_nights", 0) for r in series),
            "sold_room_nights": sum(r.get("sold_room_nights", 0) for r in series),
            "occupancy_pct": None,
        },
    }


def _rev_payload(series):
    return {"series": series, "summary": {}}


def _adr_payload(series, summary=None):
    return {"series": series, "summary": summary or {}}


class VerifyReportConsistencyCommandTests(TestCase):
    """verify_report_consistency：端到端一致 + 差异可检出 + 参数门禁。"""

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

    # ------------------------------------------------------------------
    # 种子数据工厂
    # ------------------------------------------------------------------
    def _seed_inventory(self, stay_date=BD, sold=2, total=5, oos=0):
        room_type = RoomType.objects.create(
            property=self.property,
            code=f"VC-RT-{uuid.uuid4().hex[:8]}",
            name="复算房型",
            capacity=2,
        )
        return RoomTypeInventory.objects.create(
            property=self.property,
            room_type=room_type,
            stay_date=stay_date,
            total_rooms=total,
            sold=sold,
            out_of_service=oos,
        )

    def _seed_folio(self):
        guest = Guest.objects.create(property=self.property, full_name="复算住客")
        day = timezone.localdate()
        reservation = Reservation.objects.create(
            property=self.property,
            booking_no=f"VC{uuid.uuid4().hex[:12].upper()}",
            guest=guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=day,
            check_out=day + timedelta(days=1),
            total_amount=Decimal("600.00"),
        )
        return Folio.objects.create(property=self.property, reservation=reservation, guest=guest)

    def _seed_entry(self, folio, entry_type, amount, business_date=BD, currency="CNY", **extra):
        return LedgerEntry.objects.create(
            property=self.property,
            folio=folio,
            entry_type=entry_type,
            amount=Decimal(amount),
            currency=currency,
            business_date=business_date,
            reference=f"VC-{uuid.uuid4().hex[:16]}",
            description="复算工具测试凭证",
            **extra,
        )

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self._seed_owner()

    def _run(self, *args):
        out = StringIO()
        call_command("verify_report_consistency", *args, stdout=out)
        return out.getvalue()

    def _run_json(self, *args):
        out = StringIO()
        call_command("verify_report_consistency", "--as-json", *args, stdout=out)
        return json.loads(out.getvalue())

    def _run_expect_fail(self, *args):
        """失败场景：SystemExit(1) 且 JSON 可解析、差异字段齐全。"""
        out = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("verify_report_consistency", "--as-json", *args, stdout=out)
        self.assertEqual(ctx.exception.code, 1)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["result"], "FAIL")
        failed = [c for c in summary["checks"] if not c["passed"]]
        self.assertTrue(failed)
        for check in failed:
            for diff in check["diffs"]:
                self.assertTrue(diff["name"], diff)
                self.assertTrue(diff["date"], diff)
                self.assertTrue(diff["detail"], diff)
        return summary

    # ------------------------------------------------------------------
    # A. 端到端一致（真实数据链 → PASS）
    # ------------------------------------------------------------------
    def test_command_runs_and_passes_on_empty_period(self):
        text = self._run("--days", "1")
        self.assertIn("occupancy vs RoomTypeInventory", text)
        self.assertIn("revenue vs LedgerEntry", text)
        self.assertIn("adr 分子/分母/比率 vs 源账本与库存", text)
        self.assertIn("PASS", text)

    def test_command_json_output_shape(self):
        summary = self._run_json("--business-date", BD)
        self.assertEqual(summary["result"], "PASS")
        self.assertEqual(len(summary["checks"]), 3)
        for check in summary["checks"]:
            self.assertTrue(check["passed"], check["detail"])

    def test_real_data_pipeline_passes_with_reversal_and_multi_currency(self):
        """真实链路：房费+红字冲销+收款+外币凭证并存 → 复算仍全 PASS。

        红字冲销：ROOM_CHARGE +580 与其 REVERSAL -580 带符号相抵，收入=支出=580；
        主币种过滤：USD 凭证不进入主序列（报表与复算同口径排除）。
        """
        self._seed_inventory(sold=2, total=5)
        folio = self._seed_folio()
        charge = self._seed_entry(folio, "ROOM_CHARGE", "580.00")
        self._seed_entry(
            folio,
            "REVERSAL",
            "-580.00",
            void_of=charge,
            void_reason="复算测试：红字冲销原房费凭证",
            legacy_workflow=True,
        )
        self._seed_entry(folio, "PAYMENT", "-600.00")
        # 外币凭证：主币种过滤后不参与比对
        self._seed_entry(folio, "ADJUSTMENT", "-12.34", currency="USD")
        summary = self._run_json("--business-date", BD)
        self.assertEqual(summary["result"], "PASS", summary)
        # 锁定冲销进入复算口径：收入（正凭证）= 580，含 REVERSAL 后净额为 0
        revenue_check = next(c for c in summary["checks"] if c["name"].startswith("revenue"))
        self.assertTrue(revenue_check["passed"])

    def test_occupancy_rate_zero_denominator_is_consistent(self):
        """分母 0（无库存日历）时报表与复算同样输出空/None 口径 → PASS。"""
        folio = self._seed_folio()
        self._seed_entry(folio, "ROOM_CHARGE", "100.00")
        summary = self._run_json("--business-date", BD)
        self.assertEqual(summary["result"], "PASS", summary)

    # ------------------------------------------------------------------
    # B. 差异可检出（合成报表载荷 → FAIL + 结构化差异）
    # ------------------------------------------------------------------
    def test_report_revenue_greater_than_source_fails(self):
        """场景 1：报表收入比源账本多。"""
        folio = self._seed_folio()
        self._seed_entry(folio, "ROOM_CHARGE", "100.00")
        payload = _rev_payload(
            [
                {
                    "business_date": BD,
                    "revenue": "999.00",
                    "expense": "0.00",
                    "net": "999.00",
                    "business_date_label": None,
                    "entries": 1,
                }
            ]
        )
        with mock.patch(
            "apps.api.report_center.revenue_report", return_value=_FakeResponse(payload)
        ):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("revenue"))
        names = {d["name"] for d in check["diffs"]}
        self.assertIn("revenue_income_mismatch", names)
        diff = next(d for d in check["diffs"] if d["name"] == "revenue_income_mismatch")
        self.assertEqual(diff["report_value"], "999.00")
        self.assertEqual(diff["source_value"], "100.00")

    def test_report_revenue_less_than_source_fails(self):
        """场景 2：报表收入比源账本少（少记一张正凭证）。"""
        folio = self._seed_folio()
        self._seed_entry(folio, "ROOM_CHARGE", "100.00")
        self._seed_entry(folio, "ADJUSTMENT", "40.00")
        payload = _rev_payload(
            [
                {
                    "business_date": BD,
                    "revenue": "100.00",
                    "expense": "0.00",
                    "net": "100.00",
                    "business_date_label": None,
                    "entries": 1,
                }
            ]
        )
        with mock.patch(
            "apps.api.report_center.revenue_report", return_value=_FakeResponse(payload)
        ):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("revenue"))
        names = {d["name"] for d in check["diffs"]}
        self.assertIn("revenue_income_mismatch", names)
        self.assertEqual(
            next(d for d in check["diffs"] if d["name"] == "revenue_income_mismatch")[
                "source_value"
            ],
            "140.00",
        )

    def test_report_missing_source_ledger_date_fails(self):
        """场景 3/4：报表缺源账本日期行；报表有行但源账本无凭证。"""
        folio = self._seed_folio()
        self._seed_entry(folio, "ROOM_CHARGE", "100.00")
        # 报表缺行：当日有凭证但 series 为空
        with mock.patch(
            "apps.api.report_center.revenue_report", return_value=_FakeResponse(_rev_payload([]))
        ):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("revenue"))
        self.assertIn("revenue_report_missing_row", {d["name"] for d in check["diffs"]})
        # 报表多行：源账本无凭证的日期出现在 series
        ghost = _rev_payload(
            [
                {
                    "business_date": "2026-06-02",
                    "revenue": "50.00",
                    "expense": "0.00",
                    "net": "50.00",
                    "business_date_label": None,
                    "entries": 1,
                }
            ]
        )
        with mock.patch("apps.api.report_center.revenue_report", return_value=_FakeResponse(ghost)):
            summary = self._run_expect_fail("--business-date", "2026-06-02")
        check = next(c for c in summary["checks"] if c["name"].startswith("revenue"))
        diff = next(d for d in check["diffs"] if d["name"] == "revenue_report_extra_row")
        self.assertEqual(diff["date"], "2026-06-02")

    def test_room_charge_reversal_flows_into_adr(self):
        """场景 5：房费红字冲销后 ADR 分子归零，复算与报表一致才 PASS。"""
        self._seed_inventory(sold=1)
        folio = self._seed_folio()
        charge = self._seed_entry(folio, "ROOM_CHARGE", "300.00")
        self._seed_entry(
            folio,
            "REVERSAL",
            "-300.00",
            void_of=charge,
            void_reason="复算测试：整笔房费冲销",
            legacy_workflow=True,
        )
        summary = self._run_json("--business-date", BD)
        adr_check = next(c for c in summary["checks"] if c["name"].startswith("adr"))
        self.assertTrue(adr_check["passed"], adr_check["detail"])

    def test_adr_sold_room_nights_mismatch_fails(self):
        """场景 6：ADR 分母（已售间夜）不一致必须报差异，不能只看 room_revenue。"""
        self._seed_inventory(sold=2)
        folio = self._seed_folio()
        self._seed_entry(folio, "ROOM_CHARGE", "600.00")
        payload = _adr_payload(
            [{"date": BD, "room_revenue": "600.00", "sold_room_nights": 5, "adr": "120.00"}]
        )
        with mock.patch("apps.api.report_center.adr_report", return_value=_FakeResponse(payload)):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("adr"))
        names = {d["name"] for d in check["diffs"]}
        self.assertIn("adr_sold_room_nights_mismatch", names)
        self.assertIn("adr_value_mismatch", names)
        diff = next(d for d in check["diffs"] if d["name"] == "adr_sold_room_nights_mismatch")
        self.assertEqual(diff["report_value"], "5")
        self.assertEqual(diff["source_value"], "2")

    def test_occupancy_rate_mismatch_fails(self):
        """场景 7：Occupancy 比率不一致（数量一致、比率算错）必须报差异。"""
        self._seed_inventory(sold=2, total=5)
        payload = _occ_payload(
            [
                {
                    "date": BD,
                    "sold_room_nights": 2,
                    "sellable_room_nights": 5,
                    "occupancy_pct": "88.00",
                    "total_rooms": 5,
                    "out_of_service": 0,
                    "held": 0,
                }
            ]
        )
        with mock.patch(
            "apps.api.report_center.occupancy_report", return_value=_FakeResponse(payload)
        ):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("occupancy"))
        diff = next(d for d in check["diffs"] if d["name"] == "occupancy_rate_mismatch")
        self.assertEqual(diff["report_value"], "88.00")
        self.assertEqual(diff["source_value"], "40.00")

    def test_source_inventory_calendar_gap_detected(self):
        """场景 8：源库存日历缺失日出现在报表 → 报表多行差异（不静默 PASS）。"""
        self._seed_inventory()  # 只有 BD 有库存；BD+1 无任何库存行
        payload = _occ_payload(
            [
                {
                    "date": BD,
                    "sold_room_nights": 2,
                    "sellable_room_nights": 5,
                    "occupancy_pct": "40.00",
                    "total_rooms": 5,
                    "out_of_service": 0,
                    "held": 0,
                },
                {
                    "date": "2026-06-02",
                    "sold_room_nights": 3,
                    "sellable_room_nights": 5,
                    "occupancy_pct": "60.00",
                    "total_rooms": 5,
                    "out_of_service": 0,
                    "held": 0,
                },
            ]
        )
        with mock.patch(
            "apps.api.report_center.occupancy_report", return_value=_FakeResponse(payload)
        ):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("occupancy"))
        diff = next(d for d in check["diffs"] if d["name"] == "occupancy_report_extra_row")
        self.assertEqual(diff["date"], "2026-06-02")

    def test_base_currency_filter_excludes_foreign_entries(self):
        """场景 9：主币种过滤——外币凭证不因漏算触发差异（与报表同口径排除）。"""
        folio = self._seed_folio()
        self._seed_entry(folio, "ADJUSTMENT", "-55.00", currency="USD")
        summary = self._run_json("--business-date", BD)
        self.assertEqual(summary["result"], "PASS", summary)

    # ------------------------------------------------------------------
    # D. V4.0 TASK-04 补齐：RPT-04 / RPT-06 / RPT-08 / RPT-10
    # ------------------------------------------------------------------

    def test_rpt04_report_extra_row_when_source_missing(self):
        """RPT-04：报表存在、源数据缺失 → 报表多行差异（不得静默 PASS）。

        RPT-03 覆盖"源有报表缺"，本用例覆盖反向：源账本当日无凭证，
        报表却输出该日收入行——必须报 revenue_report_extra_row。
        """
        # 不种子任何 LedgerEntry：源账本为空，报表却报当日有收入
        ghost = _rev_payload(
            [
                {
                    "business_date": BD,
                    "revenue": "120.00",
                    "expense": "0.00",
                    "net": "120.00",
                    "business_date_label": None,
                    "entries": 1,
                }
            ]
        )
        with mock.patch("apps.api.report_center.revenue_report", return_value=_FakeResponse(ghost)):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("revenue"))
        diff = next(d for d in check["diffs"] if d["name"] == "revenue_report_extra_row")
        self.assertEqual(diff["date"], BD)
        # 源账本零凭证：source_value 按设计为 None（不是 0.00），detail 说明口径
        self.assertIsNone(diff["source_value"])
        self.assertIn("源账本零凭证", diff["detail"])

    def test_rpt06_adr_room_revenue_numerator_mismatch(self):
        """RPT-06：ADR 分子（房费收入）与源账本 ROOM_CHARGE 不一致必须报差异。

        分母（已售间夜）取正确值，只让分子偏移，锁死只比对比率或分母的
        伪通过路径。
        """
        self._seed_inventory(sold=2)
        folio = self._seed_folio()
        self._seed_entry(folio, "ROOM_CHARGE", "600.00")
        # 源：分子 600 / 分母 2 = 300；报表谎报分子 800（比率随之 400）
        payload = _adr_payload(
            [{"date": BD, "room_revenue": "800.00", "sold_room_nights": 2, "adr": "400.00"}]
        )
        with mock.patch("apps.api.report_center.adr_report", return_value=_FakeResponse(payload)):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("adr"))
        names = {d["name"] for d in check["diffs"]}
        self.assertIn("adr_room_revenue_mismatch", names)
        diff = next(d for d in check["diffs"] if d["name"] == "adr_room_revenue_mismatch")
        self.assertEqual(diff["report_value"], "800.00")
        self.assertEqual(diff["source_value"], "600.00")

    def test_rpt08_adr_final_value_mismatch(self):
        """RPT-08：分子与分母都正确、但最终 ADR 值算错必须报 adr_value_mismatch。

        防止"只校验分子分母、不校验最终值"的漏检。
        """
        self._seed_inventory(sold=2)
        folio = self._seed_folio()
        self._seed_entry(folio, "ROOM_CHARGE", "600.00")
        # 分子 600 / 分母 2 都与源一致，仅 adr 最终值谎报 999
        payload = _adr_payload(
            [{"date": BD, "room_revenue": "600.00", "sold_room_nights": 2, "adr": "999.00"}]
        )
        with mock.patch("apps.api.report_center.adr_report", return_value=_FakeResponse(payload)):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("adr"))
        names = {d["name"] for d in check["diffs"]}
        self.assertIn("adr_value_mismatch", names)
        # 分子/分母未偏移，不得出现对应差异（差异必须精确定位）
        self.assertNotIn("adr_room_revenue_mismatch", names)
        self.assertNotIn("adr_sold_room_nights_mismatch", names)
        diff = next(d for d in check["diffs"] if d["name"] == "adr_value_mismatch")
        self.assertEqual(diff["report_value"], "999.00")
        self.assertEqual(diff["source_value"], "300.00")

    def test_rpt10_occupancy_sold_room_nights_mismatch(self):
        """RPT-10：Occupancy 已售房量与源 RoomTypeInventory.sold 不一致必须报差异。

        可售房量（分母）与比率都由报表给的 sold 推出、彼此自洽，只有 sold
        与源表不一致——锁死"只校验比率自洽性"的伪通过路径。
        """
        self._seed_inventory(sold=2, total=5)
        payload = _occ_payload(
            [
                {
                    "date": BD,
                    "sold_room_nights": 4,  # 源为 2
                    "sellable_room_nights": 5,
                    "occupancy_pct": "80.00",  # 4/5，与谎报 sold 自洽
                    "total_rooms": 5,
                    "out_of_service": 0,
                    "held": 0,
                }
            ]
        )
        with mock.patch(
            "apps.api.report_center.occupancy_report", return_value=_FakeResponse(payload)
        ):
            summary = self._run_expect_fail("--business-date", BD)
        check = next(c for c in summary["checks"] if c["name"].startswith("occupancy"))
        names = {d["name"] for d in check["diffs"]}
        self.assertIn("occupancy_sold_mismatch", names)
        diff = next(d for d in check["diffs"] if d["name"] == "occupancy_sold_mismatch")
        self.assertEqual(diff["report_value"], "4")
        self.assertEqual(diff["source_value"], "2")

    # ------------------------------------------------------------------
    # C. 参数门禁（场景 10/11/12）
    # ------------------------------------------------------------------
    def test_empty_range_days_one_still_runs(self):
        """场景 10：空日期区间（1 天、无数据）→ PASS 不报错。"""
        summary = self._run_json("--days", "1")
        self.assertEqual(summary["result"], "PASS")

    def test_invalid_business_date_rejected(self):
        """场景 11：非法日期 → CommandError，不产出伪造 PASS。"""
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            self._run("--business-date", "2026-13-40")

    def test_range_over_maximum_rejected(self):
        """场景 12：超过最大统计范围 → CommandError。"""
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            self._run("--days", "93")
