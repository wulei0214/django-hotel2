# -*- coding: utf-8 -*-
"""§4.5 报表分析中心回归测试包。

覆盖：
  * 只读 API 权限四态（匿名 401 / 无任职 404 / 缺对应权限码 403 / 有权限 200）：
    - 收入类（revenue / adr）→ hotelapp.finance_access（前台 403，值班经理 200）；
    - 运营类（occupancy / guests / housekeeping）→ hotelapp.view_accommodation
      （前台 200；客房服务员 403——view_room 不能替代）；
  * 区间校验：from>to 400、跨度 >92 天 400、非法日期格式 400、group_by 非法 400；
  * 口径 metadata 存在（property/period/date_basis/status_scope/currency/
    generated_at/source_system）；
  * 构造数据断言计算正确：入住率（含停用房剔除分母）、ADR（ROOM_CHARGE 及
    红字冲销口径，押金/收款不计入）、收入按日/按类型分组、多币种不相加、
    客源（新客/回头客/来源分布）、清洁效率（事件按日计数、他物业隔离）；
  * 空区间返回空序列不报错；
  * /manage/reports/ 页面门禁（匿名 302 / 前台 403 / 值班经理 200 +
    专属模板 + 侧边栏入口）。

口径：全部只读聚合，不新增模型/落库表；金额 Decimal → str。
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.utils import timezone

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.billing.models import Folio, LedgerEntry
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.inventory.models import RoomTypeInventory
from apps.reservations.models import Reservation

# 报表 API 端点（§4.5 首期五张只读报表）
REPORT_ENDPOINTS = (
    "/api/v1/reports/occupancy",
    "/api/v1/reports/adr",
    "/api/v1/reports/revenue",
    "/api/v1/reports/guests",
    "/api/v1/reports/housekeeping",
)
FINANCIAL_ENDPOINTS = ("/api/v1/reports/adr", "/api/v1/reports/revenue")
OPERATIONAL_ENDPOINTS = (
    "/api/v1/reports/occupancy",
    "/api/v1/reports/guests",
    "/api/v1/reports/housekeeping",
)


class ReportCenterTestBase(E2EFlowTestBase):
    """DEFAULT 物业 + 岗位账号 + 报表数据工厂。"""

    maxDiff = None

    def setUp(self):
        super().setUp()
        self.dm = self._make_staff("report_center_duty", "值班经理")
        self.fd = self._make_staff("report_center_front", "前台")
        self.finance = self._make_staff("report_center_finance", "财务")
        self.hk = self._make_staff("report_center_housekeeping", "客房服务员")
        self.client.force_login(self.dm)
        self._ref_seq = 0
        self._guest_seq = 0

    # ------------------------------------------------------------------
    # 数据工厂（直接构造只读聚合的事实数据；不经业务服务——报表不写）
    # ------------------------------------------------------------------
    def _next_ref(self):
        self._ref_seq += 1
        return f"RPT-{uuid.uuid4().hex[:8]}-{self._ref_seq}"

    def _make_inventory(self, stay_date, total=5, sold=0, held=0, out_of_service=0):
        return RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type,
            stay_date=stay_date, total_rooms=total, sold=sold,
            held=held, out_of_service=out_of_service)

    def _make_folio(self):
        self._guest_seq += 1
        guest = Guest.objects.create(
            property=self.property, full_name=f"报表住客{self._guest_seq}",
            phone=f"139{self._guest_seq:08d}")
        day = self.today - timedelta(days=30)
        reservation = Reservation.objects.create(
            property=self.property, booking_no=f"RPT-{uuid.uuid4().hex[:12].upper()}",
            guest=guest, check_in=day, check_out=day + timedelta(days=1))
        return Folio.objects.create(
            property=self.property, reservation=reservation, guest=guest)

    def _make_entry(self, entry_type, amount, business_date, currency="CNY",
                    void_of=None, folio=None):
        entry = LedgerEntry.objects.create(
            property=self.property, folio=folio or self._make_folio(),
            entry_type=entry_type, amount=Decimal(str(amount)),
            currency=currency, business_date=business_date,
            reference=self._next_ref(), void_of=void_of,
            void_reason="口径测试冲销" if void_of else "")
        return entry

    def _make_guest(self, created_days_ago=0):
        self._guest_seq += 1
        guest = Guest.objects.create(
            property=self.property, full_name=f"客源客人{self._guest_seq}",
            phone=f"137{self._guest_seq:08d}")
        if created_days_ago:
            Guest.objects.filter(pk=guest.pk).update(
                created_at=timezone.now() - timedelta(days=created_days_ago))
        return guest

    def _make_reservation(self, guest, source="WEBSITE", created_days_ago=0):
        reservation = Reservation.objects.create(
            property=self.property, booking_no=f"RPT-{uuid.uuid4().hex[:12].upper()}",
            guest=guest, source=source, check_in=self.today,
            check_out=self.today + timedelta(days=1))
        if created_days_ago:
            Reservation.objects.filter(pk=reservation.pk).update(
                created_at=timezone.now() - timedelta(days=created_days_ago))
        return reservation

    def _make_hk_event(self, room_entity_id, days_ago=0, property_obj="default"):
        event = AuditEvent.record(
            action="housekeeping.status_changed", entity_type="Room",
            entity_id=str(room_entity_id), actor=self.dm,
            property_obj=self.property if property_obj == "default" else property_obj,
            after={"housekeeping": "CLEAN"})
        if days_ago:
            AuditEvent.objects.filter(pk=event.pk).update(
                created_at=timezone.now() - timedelta(days=days_ago))
        return event


class ReportPermissionTests(ReportCenterTestBase):
    """权限四态：匿名 / 无任职 / 缺权限码 / 有权限。"""

    def test_reports_require_authentication(self):
        self.client.logout()
        for url in REPORT_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 401, url)
            self.assertEqual(
                response.json()["error"]["code"], "AUTHENTICATION_REQUIRED", url)

    def test_reports_require_property_membership(self):
        """已登录但无任何物业任职：无物业上下文 → 404（与审计中心同口径）。"""
        outsider = User.objects.create_user(
            username="report_no_membership", password="No-Membership-2026!")
        self.client.force_login(outsider)
        for url in REPORT_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404, url)
            self.assertEqual(
                response.json()["error"]["code"], "RESOURCE_NOT_FOUND", url)

    def test_financial_reports_require_finance_access(self):
        """前台（有 view_accommodation 无 finance_access）→ 收入类 403。

        收入/ADR 分子是已过账账本金额，不以运营查看权替代（§4.1 同门禁）。
        """
        self.client.force_login(self.fd)
        for url in FINANCIAL_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(
                response.json()["error"]["code"], "PERMISSION_DENIED", url)

    def test_financial_reports_ok_for_finance_access(self):
        for url in REPORT_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertIn("data", response.json(), url)

    def test_operational_reports_ok_for_front_desk(self):
        """前台（view_accommodation）可看运营类报表；无需财务/审计权限。"""
        self.client.force_login(self.fd)
        for url in OPERATIONAL_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)

    def test_operational_reports_require_view_accommodation(self):
        """客房服务员（仅 view_room/change_room）→ 运营类 403：
        view_room 不替代 view_accommodation，运营报表挂入住运营口径。
        """
        hk = self._make_staff("report_center_housekeeper", "客房服务员")
        self.client.force_login(hk)
        for url in OPERATIONAL_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(
                response.json()["error"]["code"], "PERMISSION_DENIED", url)
        for url in FINANCIAL_ENDPOINTS:
            self.assertEqual(self.client.get(url).status_code, 403, url)


class ReportRangeValidationTests(ReportCenterTestBase):
    """区间校验：格式/from>to/92 天上限/非法 group_by。"""

    def test_from_after_to_rejected(self):
        response = self.client.get(
            "/api/v1/reports/occupancy",
            {"from": str(self.today), "to": str(self.today - timedelta(days=1))})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_range_over_92_days_rejected(self):
        """区间上限 92 天：93 天跨度 → 400（防全表扫描，§4.5 技术要求）。"""
        start = self.today - timedelta(days=120)
        ok = self.client.get("/api/v1/reports/occupancy", {
            "from": str(start), "to": str(start + timedelta(days=92))})
        self.assertEqual(ok.status_code, 200)
        for url in REPORT_ENDPOINTS:
            response = self.client.get(url, {
                "from": str(start), "to": str(start + timedelta(days=93))})
            self.assertEqual(response.status_code, 400, url)
            self.assertEqual(
                response.json()["error"]["code"], "VALIDATION_ERROR", url)

    def test_invalid_date_format_rejected(self):
        for url in REPORT_ENDPOINTS:
            response = self.client.get(url, {"from": "2026/01/01"})
            self.assertEqual(response.status_code, 400, url)
            self.assertEqual(
                response.json()["error"]["code"], "VALIDATION_ERROR", url)

    def test_invalid_group_by_rejected(self):
        response = self.client.get("/api/v1/reports/revenue", {"group_by": "week"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")


class ReportMetadataTests(ReportCenterTestBase):
    """每报表响应含统计口径 metadata（P0-07 模式）。"""

    def test_metadata_present_on_all_reports(self):
        for url in REPORT_ENDPOINTS:
            body = self.client.get(url).json()
            meta = body["data"]["metadata"]
            self.assertEqual(meta["property"]["code"], self.property.code, url)
            self.assertEqual(meta["property"]["name"], self.property.name, url)
            self.assertEqual(meta["currency"], self.property.currency, url)
            self.assertEqual(meta["source_system"], "PMS2", url)
            self.assertIn("generated_at", meta, url)
            self.assertIn("start", meta["period"], url)
            self.assertIn("end", meta["period"], url)
            self.assertTrue(meta["date_basis"], url)
            self.assertTrue(meta["status_scope"], url)

    def test_default_range_is_last_seven_days(self):
        body = self.client.get("/api/v1/reports/occupancy").json()
        period = body["data"]["metadata"]["period"]
        self.assertEqual(period["days"], 7)
        self.assertEqual(period["end"], str(self.today))
        self.assertEqual(period["start"], str(self.today - timedelta(days=6)))


class OccupancyReportTests(ReportCenterTestBase):
    """入住率计算：OCC = Σsold / Σ(total − out_of_service)。"""

    def test_occupancy_calculation_excludes_out_of_service(self):
        day1 = self.today - timedelta(days=3)
        day2 = self.today - timedelta(days=2)
        # day1：总 5、停用 1（分母剔除）→ 可售 4，已售 3 → 75.00%
        self._make_inventory(day1, total=5, sold=3, out_of_service=1)
        # day2：总 7、已售 5、冻结 2（冻结计入分母不计入分子）→ 5/7 = 71.43%
        self._make_inventory(day2, total=7, sold=5, held=2)
        body = self.client.get("/api/v1/reports/occupancy", {
            "from": str(day1), "to": str(day2)}).json()
        series = body["data"]["series"]
        self.assertEqual(len(series), 2)
        self.assertEqual(series[0]["date"], str(day1))
        self.assertEqual(series[0]["sellable_room_nights"], 4)
        self.assertEqual(series[0]["sold_room_nights"], 3)
        self.assertEqual(series[0]["occupancy_pct"], "75.00")
        self.assertEqual(series[1]["occupancy_pct"], "71.43")
        summary = body["data"]["summary"]
        self.assertEqual(summary["sellable_room_nights"], 11)
        self.assertEqual(summary["sold_room_nights"], 8)
        self.assertEqual(summary["occupancy_pct"], "72.73")

    def test_occupancy_zero_sellable_day_yields_null_pct(self):
        day = self.today - timedelta(days=3)
        self._make_inventory(day, total=2, out_of_service=2)
        body = self.client.get("/api/v1/reports/occupancy", {
            "from": str(day), "to": str(day)}).json()
        row = body["data"]["series"][0]
        self.assertEqual(row["sellable_room_nights"], 0)
        self.assertIsNone(row["occupancy_pct"])
        self.assertIsNone(body["data"]["summary"]["occupancy_pct"])


class AdrReportTests(ReportCenterTestBase):
    """ADR 计算：房费收入（ROOM_CHARGE + 红字冲销）/ 已售间夜。"""

    def test_adr_calculation_with_reversal(self):
        day1 = self.today - timedelta(days=3)
        day2 = self.today - timedelta(days=2)
        self._make_inventory(day1, total=5, sold=2)
        self._make_inventory(day2, total=5, sold=2)
        folio = self._make_folio()
        self._make_entry(LedgerEntry.EntryType.ROOM_CHARGE, 300,
                         day1, folio=folio)
        self._make_entry(LedgerEntry.EntryType.ROOM_CHARGE, 300,
                         day2, folio=folio)
        # day2 红字冲销一笔房费：分子带符号合计 300 + (−300) = 300
        original = LedgerEntry.objects.filter(
            property=self.property,
            business_date=day2,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE).first()
        self._make_entry(LedgerEntry.EntryType.REVERSAL, -300, day2,
                         void_of=original, folio=folio)
        # 押金/收款不计入 ADR 分子
        self._make_entry(LedgerEntry.EntryType.DEPOSIT, -500, day1, folio=folio)
        self._make_entry(LedgerEntry.EntryType.PAYMENT, -200, day2, folio=folio)

        body = self.client.get("/api/v1/reports/adr", {
            "from": str(day1), "to": str(day2)}).json()
        data = body["data"]
        series = {row["date"]: row for row in data["series"]}
        self.assertEqual(series[str(day1)]["room_revenue"], "300.00")
        self.assertEqual(series[str(day1)]["adr"], "150.00")   # 300 / 2 间夜
        self.assertEqual(series[str(day2)]["room_revenue"], "0.00")
        self.assertEqual(series[str(day2)]["adr"], "0.00")     # 冲销后 0 / 2 间夜
        summary = data["summary"]
        self.assertEqual(summary["room_revenue"], "300.00")
        self.assertEqual(summary["sold_room_nights"], 4)
        self.assertEqual(summary["adr"], "75.00")              # 300 / 4 间夜


class RevenueReportTests(ReportCenterTestBase):
    """收入报表：按日 / 按类型分组（LedgerEntry 口径）。"""

    def setUp(self):
        super().setUp()
        self.day1 = self.today - timedelta(days=3)
        self.day2 = self.today - timedelta(days=2)
        self.folio = self._make_folio()
        self._make_entry(LedgerEntry.EntryType.ROOM_CHARGE, 300, self.day1,
                         folio=self.folio)
        self._make_entry(LedgerEntry.EntryType.ROOM_CHARGE, 200, self.day1,
                         folio=self.folio)
        self._make_entry(LedgerEntry.EntryType.PAYMENT, -400, self.day1,
                         folio=self.folio)
        self._make_entry(LedgerEntry.EntryType.ROOM_CHARGE, 500, self.day2,
                         folio=self.folio)
        self._make_entry(LedgerEntry.EntryType.PAYMENT, -300, self.day2,
                         folio=self.folio)

    def test_revenue_group_by_day(self):
        body = self.client.get("/api/v1/reports/revenue", {
            "from": str(self.day1), "to": str(self.day2),
            "group_by": "day"}).json()
        series = body["data"]["series"]
        self.assertEqual(len(series), 2)
        row1 = series[0]
        self.assertEqual(row1["business_date"], str(self.day1))
        self.assertEqual(row1["revenue"], "500.00")
        self.assertEqual(row1["expense"], "400.00")
        self.assertEqual(row1["net"], "100.00")
        self.assertEqual(row1["entries"], 3)
        summary = body["data"]["summary"]
        self.assertEqual(summary["total_revenue"], "1000.00")
        self.assertEqual(summary["total_expense"], "700.00")
        self.assertEqual(summary["net"], "300.00")
        self.assertEqual(summary["entries"], 5)

    def test_revenue_group_by_type(self):
        body = self.client.get("/api/v1/reports/revenue", {
            "from": str(self.day1), "to": str(self.day2),
            "group_by": "type"}).json()
        rows = {row["entry_type"]: row for row in body["data"]["series"]}
        self.assertEqual(rows["ROOM_CHARGE"]["revenue"], "1000.00")
        self.assertEqual(rows["ROOM_CHARGE"]["entries"], 3)
        self.assertEqual(rows["PAYMENT"]["expense"], "700.00")
        self.assertEqual(rows["PAYMENT"]["net"], "-700.00")

    def test_revenue_multi_currency_not_summed(self):
        """外币凭证不与本币相加：单列 other_currencies（§4.5 多币种规则）。"""
        self._make_entry(LedgerEntry.EntryType.ROOM_CHARGE, 100, self.day2,
                         currency="USD", folio=self.folio)
        body = self.client.get("/api/v1/reports/revenue", {
            "from": str(self.day1), "to": str(self.day2),
            "group_by": "day"}).json()
        summary = body["data"]["summary"]
        self.assertEqual(summary["currency"], "CNY")
        self.assertEqual(summary["total_revenue"], "1000.00")
        others = summary["other_currencies"]
        self.assertEqual(len(others), 1)
        self.assertEqual(others[0]["currency"], "USD")
        self.assertEqual(others[0]["revenue"], "100.00")
        self.assertEqual(others[0]["net"], "100.00")


class GuestsReportTests(ReportCenterTestBase):
    """客源报表：新客 / 回头客 / 来源分布。"""

    def test_guests_report_calculation(self):
        period_from = self.today - timedelta(days=2)
        period_to = self.today
        # A：期间内建档（新客），历史 2 单（1 单在期间前）→ 回头客
        guest_a = self._make_guest(created_days_ago=1)
        self._make_reservation(guest_a, source="WEBSITE", created_days_ago=10)
        self._make_reservation(guest_a, source="OTA", created_days_ago=1)
        # B：期间内建档（新客），仅 1 单 → 非回头客
        guest_b = self._make_guest(created_days_ago=1)
        self._make_reservation(guest_b, source="WALK_IN", created_days_ago=1)
        # C：期间外建档（非新客），期间内 1 单
        guest_c = self._make_guest(created_days_ago=10)
        self._make_reservation(guest_c, source="OTA", created_days_ago=0)
        # 他物业预订不计入
        other_property = Property.objects.create(code="RPT-OTHER", name="他店")
        other_guest = Guest.objects.create(
            property=other_property, full_name="他店客人", phone="13600000000")
        Reservation.objects.create(
            property=other_property, booking_no="RPT-OTHER-1",
            guest=other_guest, check_in=self.today,
            check_out=self.today + timedelta(days=1))

        body = self.client.get("/api/v1/reports/guests", {
            "from": str(period_from), "to": str(period_to)}).json()
        summary = body["data"]["summary"]
        self.assertEqual(summary["new_guests"], 2)          # A、B
        self.assertEqual(summary["returning_guests"], 1)    # 仅 A（历史 ≥2 单）
        self.assertEqual(summary["reservations"], 3)        # 期间内下单：A/B/C 各 1
        self.assertEqual(summary["unique_guests"], 3)
        by_source = {row["source"]: row for row in body["data"]["by_source"]}
        self.assertEqual(by_source["OTA"]["reservations"], 2)
        self.assertEqual(by_source["OTA"]["source_label"], "渠道预订")
        self.assertEqual(by_source["WALK_IN"]["reservations"], 1)
        self.assertEqual(by_source["WALK_IN"]["source_label"], "散客直入")
        # A 的首单（WEBSITE）创建于期间前：来源分布只统计期间内创建的预订
        self.assertNotIn("WEBSITE", by_source)


class HousekeepingReportTests(ReportCenterTestBase):
    """清洁效率报表：housekeeping.status_changed 事件按自然日计数。"""

    def test_housekeeping_report_counts(self):
        self._make_hk_event(self.room1.pk, days_ago=0)
        self._make_hk_event(self.room1.pk, days_ago=0)
        self._make_hk_event(self.room2.pk, days_ago=1)
        # 他物业事件与其他 action 不计入
        other_property = Property.objects.create(code="RPT-OTHER2", name="他店二")
        self._make_hk_event(self.room1.pk, days_ago=0,
                            property_obj=other_property)
        self._make_hk_event(self.room3.pk, days_ago=0)  # 覆盖为其他 action
        AuditEvent.objects.filter(
            property=self.property, entity_id=str(self.room3.pk)).update(
            action="room.maintenance_changed")

        body = self.client.get("/api/v1/reports/housekeeping").json()
        data = body["data"]
        series = {row["date"]: row["status_changes"] for row in data["series"]}
        self.assertEqual(series[str(self.today)], 2)
        self.assertEqual(series[str(self.today - timedelta(days=1))], 1)
        self.assertEqual(data["summary"]["total_changes"], 3)
        self.assertEqual(data["summary"]["rooms_involved"], 2)
        self.assertEqual(data["summary"]["avg_changes_per_day"], "0.43")


class ReportEmptyDataTests(ReportCenterTestBase):
    """空区间返回空序列与零汇总，不报错。"""

    def test_empty_range_returns_empty_series(self):
        empty_from = self.today - timedelta(days=90)
        empty_to = self.today - timedelta(days=80)
        for url in REPORT_ENDPOINTS:
            response = self.client.get(url, {
                "from": str(empty_from), "to": str(empty_to)})
            self.assertEqual(response.status_code, 200, url)
            data = response.json()["data"]
            # 序列键随报表而异：四张序列报表为 series，客源报表为 by_source
            if "guests" in url:
                self.assertEqual(data["by_source"], [], url)
            else:
                self.assertEqual(data["series"], [], url)
            self.assertIn("summary", data, url)
            self.assertIn("metadata", data, url)

    def test_revenue_empty_series_zero_summary(self):
        empty_from = self.today - timedelta(days=90)
        response = self.client.get("/api/v1/reports/revenue", {
            "from": str(empty_from), "to": str(empty_from), "group_by": "type"})
        summary = response.json()["data"]["summary"]
        self.assertEqual(summary["total_revenue"], "0.00")
        self.assertEqual(summary["total_expense"], "0.00")
        self.assertEqual(summary["net"], "0.00")
        self.assertEqual(summary["entries"], 0)


class ReportPageTests(ReportCenterTestBase):
    """页面门禁：/manage/reports/（MANAGEMENT_CENTERS['reports']）。"""

    def test_reports_page_requires_login(self):
        self.client.logout()
        response = self.client.get("/manage/reports/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_reports_page_forbidden_without_any_report_category_permission(self):
        """没有运营类/财务类报表权限时，报表中心页面必须拒绝访问。"""
        self.client.force_login(self.hk)
        response = self.client.get("/manage/reports/")
        self.assertEqual(response.status_code, 403)

    def test_reports_page_filters_cards_by_category_permissions(self):
        """页面按岗位只显示其有权访问的报表类别，避免页面与 API 门禁错配。"""
        self.client.force_login(self.fd)
        operational = self.client.get("/manage/reports/")
        self.assertEqual(operational.status_code, 200, operational.content)
        self.assertContains(operational, '"view_accommodation": true')
        self.assertContains(operational, '"finance_access": false')
        self.assertContains(operational, "visibleReports")

        self.client.force_login(self.finance)
        financial = self.client.get("/manage/reports/")
        self.assertEqual(financial.status_code, 200, financial.content)
        self.assertContains(financial, '"view_accommodation": false')
        self.assertContains(financial, '"finance_access": true')

    def test_reports_page_ok_with_dedicated_template_and_sidebar(self):
        self.client.force_login(self.dm)
        response = self.client.get("/manage/reports/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pms/report_center.html")
        self.assertContains(response, "报表选择")
        self.assertContains(response, "统计口径说明")
        # 侧边栏入口（财务与日结组新增项）对 finance_access 持有者可见
        self.assertContains(response, "报表分析中心")
