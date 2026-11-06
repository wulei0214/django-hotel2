# -*- coding: utf-8 -*-
"""§4.2 营业日与夜审中心回归测试包。

覆盖：
  * 只读 API 权限四态（匿名 401 / 无权限 403 / 有权限 200 / 跨物业隔离 404）；
  * 统一分页契约（page/page_size 边界、meta.pagination、稳定排序、状态筛选）；
  * 营业日详情数据正确性（夜审运行历史 / 重开审批 / 过租凭证 / 未到候选 / 异常房态）；
  * break-glass 记录列表与复核状态筛选；
  * 阻挡规则（§4.2 营业日结后的业务写入限制）：营业日 CLOSED 后新入住被拒绝、
    排房不受限、REOPENED 后恢复可入住；
  * 旧 /nightaudit/ 页 302 迁移与新中心页面权限门禁。

读取口径决策：全部只读端点以 hotelapp.night_audit_execute 为门禁（与旧
/nightaudit/ 页 GET 同码）；营业日重开审批（hotelapp.business_date_reopen）
是写审批权限，不作为读取基线。
"""
from datetime import timedelta

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.billing.models import LedgerEntry
from apps.billing.night_audit import NightAuditService
from apps.core.models import (
    BreakGlassRecord, BusinessDate, NightAuditRun, Property,
)
from apps.identity.authorization import AuthorizationService
from apps.reservations.front_desk_services import (
    FrontDeskOperationError, assign_room, check_in_stay,
)
from apps.reservations.models import Reservation, Stay


class NightAuditCenterReadApiTests(E2EFlowTestBase):
    """营业日中心只读 API：权限、分页、筛选、详情数据正确性。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("na_center_frontdesk", "前台")
        self.dm = self._make_staff("na_center_duty", "值班经理")
        self.client.force_login(self.dm)

    def _run_audit(self):
        response = self._post_json("/api/v1/night-audit/run")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    # ------------------------------------------------------------------
    # 1) 权限四态：匿名 401 / 无权限 403 / 有权限 200 / 跨物业 404
    # ------------------------------------------------------------------
    READ_ENDPOINTS = (
        "/api/v1/night-audit/business-dates",
        "/api/v1/night-audit/business-dates/current",
        "/api/v1/night-audit/break-glass",
        "/api/v1/night-audit/runs",
    )

    def test_read_endpoints_require_authentication(self):
        self.client.logout()
        for url in self.READ_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 401, url)
            self.assertEqual(response.json()["error"]["code"],
                             "AUTHENTICATION_REQUIRED", url)

    def test_read_endpoints_require_night_audit_execute_permission(self):
        """读取口径决策：hotelapp.night_audit_execute 为读门禁（前台 403）。"""
        self.client.force_login(self.fd)
        self.assertFalse(AuthorizationService.has_permission(
            self.fd, self.property, "hotelapp.night_audit_execute"))
        for url in self.READ_ENDPOINTS:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response.json()["error"]["code"],
                             "PERMISSION_DENIED", url)

    def test_business_dates_list_pagination_stable_order_and_isolation(self):
        other_property = Property.objects.create(
            code="NA-OTHER", name="其他物业")
        BusinessDate.objects.create(
            property=other_property,
            business_date=self.today - timedelta(days=9),
            status=BusinessDate.Status.CLOSED)
        closed_dates = [
            self.today - timedelta(days=2), self.today - timedelta(days=1)]
        for day in closed_dates:
            BusinessDate.objects.create(
                property=self.property, business_date=day,
                status=BusinessDate.Status.CLOSED)
        current = BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.OPEN)

        response = self.client.get("/api/v1/night-audit/business-dates")
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["meta"]["pagination"],
                         {"page": 1, "page_size": 20, "total": 3,
                          "has_next": False, "has_previous": False})
        # 稳定排序 -business_date,-id：当前日在前；跨物业数据不出现
        self.assertEqual([item["business_date"] for item in payload["data"]],
                         [self.today.isoformat(),
                          (self.today - timedelta(days=1)).isoformat(),
                          (self.today - timedelta(days=2)).isoformat()])
        self.assertTrue(all(item["id"] != other_property.business_dates.first().pk
                            for item in payload["data"]))
        # 分页 page_size=1：第 2 页（共 3 页），前后页均存在
        paged = self.client.get(
            "/api/v1/night-audit/business-dates?page=2&page_size=1").json()
        self.assertEqual(paged["meta"]["pagination"]["total"], 3)
        self.assertEqual(paged["meta"]["pagination"]["page"], 2)
        self.assertTrue(paged["meta"]["pagination"]["has_previous"])
        self.assertTrue(paged["meta"]["pagination"]["has_next"])
        self.assertEqual(len(paged["data"]), 1)
        # 非法分页参数 → 400 VALIDATION_ERROR（统一契约）
        bad = self.client.get("/api/v1/night-audit/business-dates?page_size=101")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "VALIDATION_ERROR")
        return current

    def test_business_dates_list_supports_more_than_one_hundred_closed_dates(self):
        """重开日期选择器依赖分页读取；101 条时第 2 页必须可访问。"""
        for offset in range(1, 102):
            BusinessDate.objects.create(
                property=self.property,
                business_date=self.today - timedelta(days=offset),
                status=BusinessDate.Status.CLOSED,
            )
        first = self.client.get(
            "/api/v1/night-audit/business-dates?status=CLOSED&page=1&page_size=100")
        self.assertEqual(first.status_code, 200, first.content)
        first_data = first.json()
        self.assertEqual(len(first_data["data"]), 100)
        self.assertTrue(first_data["meta"]["pagination"]["has_next"])
        second = self.client.get(
            "/api/v1/night-audit/business-dates?status=CLOSED&page=2&page_size=100")
        self.assertEqual(second.status_code, 200, second.content)
        second_data = second.json()
        self.assertEqual(len(second_data["data"]), 1)
        self.assertFalse(second_data["meta"]["pagination"]["has_next"])

    def test_business_dates_list_status_filter_and_invalid_status(self):
        BusinessDate.objects.create(
            property=self.property, business_date=self.today - timedelta(days=1),
            status=BusinessDate.Status.CLOSED)
        BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.OPEN)

        closed = self.client.get(
            "/api/v1/night-audit/business-dates?status=CLOSED").json()
        self.assertEqual(closed["meta"]["pagination"]["total"], 1)
        self.assertEqual(closed["data"][0]["status"], BusinessDate.Status.CLOSED)
        opened = self.client.get(
            "/api/v1/night-audit/business-dates?status=OPEN").json()
        self.assertEqual(opened["meta"]["pagination"]["total"], 1)
        self.assertEqual(opened["data"][0]["status"], BusinessDate.Status.OPEN)

        invalid = self.client.get(
            "/api/v1/night-audit/business-dates?status=WHATEVER")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "VALIDATION_ERROR")

    def test_business_dates_list_attaches_latest_run(self):
        run_data, _, _ = self._run_audit_with_stay()
        listing = self.client.get("/api/v1/night-audit/business-dates").json()
        self.assertEqual(listing["meta"]["pagination"]["total"], 1)
        latest = listing["data"][0]["latest_run"]
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], run_data["run_id"])
        self.assertEqual(latest["status"], NightAuditRun.RunStatus.SUCCESS)

    def test_business_date_current_endpoint(self):
        missing = self.client.get(
            "/api/v1/night-audit/business-dates/current").json()["data"]
        self.assertFalse(missing["exists"])
        self.assertEqual(missing["status"], BusinessDate.Status.OPEN)
        self.assertEqual(missing["business_date"], self.today.isoformat())

        bd = BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.OPEN)
        current = self.client.get(
            "/api/v1/night-audit/business-dates/current").json()["data"]
        self.assertTrue(current["exists"])
        self.assertEqual(current["id"], bd.pk)
        self.assertEqual(current["status"], BusinessDate.Status.OPEN)

    # ------------------------------------------------------------------
    # 2) 详情数据正确性：运行历史 / 过租凭证 / 未到候选 / 异常房态 / 审批
    # ------------------------------------------------------------------
    def _run_audit_with_stay(self):
        """在住一单 + 夜审执行，返回 (run_data, booking_no, stay_id)。"""
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        line = self._line(booking_no)
        self.client.force_login(self.fd)
        stay_id = self._assign_and_check_in(line, self.room1)
        self.client.force_login(self.dm)
        return self._run_audit(), booking_no, stay_id

    def test_preview_collects_all_blockers_and_run_refuses_to_execute(self):
        """夜审预览完整收集房价快照缺失；存在阻断时不得写入账本。"""
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        line = self._line(booking_no)
        self.client.force_login(self.fd)
        self._assign_and_check_in(line, self.room1)
        line.nightly_rates = []
        line.save(update_fields=("nightly_rates",))
        self.client.force_login(self.dm)

        preview = self.client.get("/api/v1/night-audit/preview")
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()["data"]
        self.assertFalse(data["can_run"])
        self.assertEqual(data["blocker_count"], 1)
        self.assertEqual(data["blockers"][0]["code"], "NIGHTLY_RATE_MISSING")
        self.assertEqual(data["blockers"][0]["severity"], "critical")
        self.assertIn(booking_no, data["blockers"][0]["message"])

        before = LedgerEntry.objects.count()
        run_response = self._post_json("/api/v1/night-audit/run")
        self.assertEqual(run_response.status_code, 409, run_response.content)
        after = LedgerEntry.objects.count()
        self.assertEqual(after, before)
        failed = NightAuditRun.objects.filter(
            business_date__property=self.property,
            status=NightAuditRun.RunStatus.FAILED).count()
        self.assertEqual(failed, 1)

    def test_business_date_detail_after_run(self):
        run_data, booking_no, _ = self._run_audit_with_stay()
        bd = BusinessDate.objects.get(property=self.property,
                                      business_date=self.today)
        response = self.client.get(
            f"/api/v1/night-audit/business-dates/{bd.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], BusinessDate.Status.CLOSED)
        self.assertEqual(data["closed_by_id"], self.dm.pk)
        # 夜审运行历史（含耗时与摘要）
        self.assertEqual(len(data["runs"]), 1)
        run = data["runs"][0]
        self.assertEqual(run["id"], run_data["run_id"])
        self.assertEqual(run["status"], NightAuditRun.RunStatus.SUCCESS)
        self.assertIsNotNone(run["duration_seconds"])
        self.assertEqual(run["summary"]["charge_total"], "300.00")
        self.assertEqual(data["latest_success_summary"]["charge_count"], 1)
        # 过租凭证汇总：夜审房费借方 300.00 + 模拟支付贷方 -600.00（两晚全额，同营业日）
        currency = data["ledger"]["currencies"].get("CNY")
        self.assertIsNotNone(currency)
        self.assertEqual(currency["entry_count"], 2)
        self.assertEqual(currency["charge_total"], "300.00")
        self.assertEqual(currency["credit_total"], "600.00")
        # 审批记录为空；异常房态汇总覆盖本物业 3 间测试房
        self.assertEqual(data["approvals"], [])
        self.assertEqual(data["room_status_summary"]["total_rooms"], 3)

    def test_business_date_detail_404_for_missing_and_cross_property(self):
        missing = self.client.get("/api/v1/night-audit/business-dates/999999")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"], "RESOURCE_NOT_FOUND")

        other_property = Property.objects.create(
            code="NA-OTHER2", name="其他物业二")
        other_bd = BusinessDate.objects.create(
            property=other_property,
            business_date=self.today - timedelta(days=1),
            status=BusinessDate.Status.CLOSED)
        cross = self.client.get(
            f"/api/v1/night-audit/business-dates/{other_bd.pk}")
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(cross.json()["error"]["code"], "RESOURCE_NOT_FOUND")

    # ------------------------------------------------------------------
    # 3) 夜审历史 runs：列表筛选、参数校验、运行详情
    # ------------------------------------------------------------------
    def test_runs_list_filters_and_run_detail(self):
        run_data, _, _ = self._run_audit_with_stay()
        bd = BusinessDate.objects.get(property=self.property,
                                      business_date=self.today)

        listing = self.client.get("/api/v1/night-audit/runs").json()
        self.assertEqual(listing["meta"]["pagination"]["total"], 1)
        self.assertEqual(listing["data"][0]["id"], run_data["run_id"])

        success = self.client.get(
            "/api/v1/night-audit/runs?status=SUCCESS").json()
        self.assertEqual(success["meta"]["pagination"]["total"], 1)
        failed = self.client.get("/api/v1/night-audit/runs?status=FAILED").json()
        self.assertEqual(failed["meta"]["pagination"]["total"], 0)
        by_date = self.client.get(
            f"/api/v1/night-audit/runs?business_date_id={bd.pk}").json()
        self.assertEqual(by_date["meta"]["pagination"]["total"], 1)

        bad_status = self.client.get("/api/v1/night-audit/runs?status=XXX")
        self.assertEqual(bad_status.status_code, 400)
        bad_bd = self.client.get("/api/v1/night-audit/runs?business_date_id=abc")
        self.assertEqual(bad_bd.status_code, 400)

        detail = self.client.get(
            f"/api/v1/night-audit/runs/{run_data['run_id']}").json()["data"]
        self.assertEqual(detail["status"], NightAuditRun.RunStatus.SUCCESS)
        self.assertIn("stay_count", detail["summary"])
        missing = self.client.get("/api/v1/night-audit/runs/999999")
        self.assertEqual(missing.status_code, 404)

    # ------------------------------------------------------------------
    # 4) break-glass 列表与复核状态筛选
    # ------------------------------------------------------------------
    def test_break_glass_list_filter_and_review_fields(self):
        # 让值班经理成为物业内唯一持 business_date_reopen 的有效账号
        from django.contrib.auth.models import Group, Permission, User
        run_data, _, _ = self._run_audit_with_stay()
        bd = BusinessDate.objects.get(property=self.property,
                                      business_date=self.today)
        User.objects.exclude(pk__in=[self.dm.pk, self.fd.pk]).update(is_active=False)
        granted = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass",
            {"password": "na_center_duty-pass123",
             "reason": "夜审后发现重大账务差错需要紧急重开修正"})
        self.assertEqual(granted.status_code, 201, granted.content)
        record_id = granted.json()["data"]["break_glass_id"]

        all_rows = self.client.get("/api/v1/night-audit/break-glass").json()
        self.assertEqual(all_rows["meta"]["pagination"]["total"], 1)
        row = all_rows["data"][0]
        self.assertEqual(row["id"], record_id)
        self.assertEqual(row["status"], BreakGlassRecord.Status.GRANTED)
        self.assertFalse(row["review_pending"])
        self.assertFalse(row["is_expired_grant"])

        # 复核状态筛选：REVIEWED 为 0；非法值 400
        reviewed = self.client.get(
            "/api/v1/night-audit/break-glass?status=REVIEWED").json()
        self.assertEqual(reviewed["meta"]["pagination"]["total"], 0)
        invalid = self.client.get("/api/v1/night-audit/break-glass?status=XXX")
        self.assertEqual(invalid.status_code, 400)

        # 执行后 USED → review_pending=True
        executed = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass/{record_id}/execute")
        self.assertEqual(executed.status_code, 200, executed.content)
        used = self.client.get(
            "/api/v1/night-audit/break-glass?status=USED").json()
        self.assertEqual(used["meta"]["pagination"]["total"], 1)
        self.assertTrue(used["data"][0]["review_pending"])

        # 另一名授权经理复核 → REVIEWED
        reviewer = self._make_staff("na_center_reviewer", "店长")
        review_permission = Permission.objects.get(
            content_type__app_label="core", codename="breakglass_review")
        Group.objects.get(name="店长").permissions.add(review_permission)
        self.client.force_login(reviewer)
        review = self._post_json(
            f"/api/v1/night-audit/break-glass/{record_id}/review",
            {"decision": "CONFIRMED", "reason": "已核对紧急重开原因与事后账务修正凭证"})
        self.assertEqual(review.status_code, 200, review.content)
        reviewed_rows = self.client.get(
            "/api/v1/night-audit/break-glass?review_decision=CONFIRMED").json()
        self.assertEqual(reviewed_rows["meta"]["pagination"]["total"], 1)
        self.assertEqual(reviewed_rows["data"][0]["status"],
                         BreakGlassRecord.Status.REVIEWED)
        self.assertFalse(reviewed_rows["data"][0]["review_pending"])


class NightAuditBlockingRuleTests(E2EFlowTestBase):
    """§4.2 阻挡规则：营业日 CLOSED 后拒绝新入住；排房不受限；REOPENED 恢复。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("na_block_frontdesk", "前台")
        self.dm = self._make_staff("na_block_duty", "值班经理")
        self.client.force_login(self.dm)

    def _run_audit(self):
        response = self._post_json("/api/v1/night-audit/run")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    def _new_confirmed_booking(self):
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        return booking_no, self._line(booking_no)

    def test_check_in_service_blocked_after_business_date_closed(self):
        """服务级：营业日关账后 check_in_stay 抛 FrontDeskOperationError。"""
        self._run_audit()
        booking_no, line = self._new_confirmed_booking()
        assigned_line, created = assign_room(
            property_obj=self.property, reservation_room_id=line.pk,
            room_id=self.room2.pk, assigned_by=self.fd)
        self.assertTrue(created)
        with self.assertRaises(FrontDeskOperationError) as ctx:
            check_in_stay(
                property_obj=self.property,
                reservation_room_id=assigned_line.pk, checked_in_by=self.fd)
        self.assertIn("已完成夜审关账", str(ctx.exception))
        self.assertFalse(Stay.objects.filter(reservation_room=assigned_line).exists())
        self.assertEqual(Reservation.objects.get(
            booking_no=booking_no).status, Reservation.Status.CONFIRMED)

    def test_check_in_api_blocked_but_assign_allowed_after_close(self):
        """API 级：夜审关账后排房仍可（次日到店预备），入住 409。"""
        self._run_audit()
        booking_no, line = self._new_confirmed_booking()
        self.client.force_login(self.fd)
        assign = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/assign",
            {"room_id": self.room2.pk})
        self.assertEqual(assign.status_code, 201, assign.content)

        blocked = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/check-in")
        self.assertEqual(blocked.status_code, 409, blocked.content)
        self.assertEqual(blocked.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertIn("已完成夜审关账", blocked.json()["error"]["message"])
        self.assertFalse(Stay.objects.filter(reservation_room=line).exists())

    def test_reopened_business_date_allows_check_in_again(self):
        """重开审批通过（REOPENED）后恢复入住；阻挡只针对 CLOSED。"""
        self._run_audit()
        bd = BusinessDate.objects.get(property=self.property,
                                      business_date=self.today)
        request_resp = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/reopen-request",
            {"reason": "夜审后发现漏记房费需要补记凭证"})
        self.assertEqual(request_resp.status_code, 201, request_resp.content)
        approval_id = request_resp.json()["data"]["approval_id"]

        owner = self._make_staff("na_block_owner", "店长")
        self.client.force_login(owner)
        approve = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/approvals/"
            f"{approval_id}/approve")
        self.assertEqual(approve.status_code, 200, approve.content)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.REOPENED)

        booking_no, line = self._new_confirmed_booking()
        self.client.force_login(self.fd)
        stay_id = self._assign_and_check_in(line, self.room3)
        self.assertEqual(Stay.objects.get(pk=stay_id).status, Stay.Status.IN_HOUSE)


class NightAuditCenterPageTests(E2EFlowTestBase):
    """营业日中心页面：旧页 302 迁移、登录与权限门禁。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("na_page_frontdesk", "前台")
        self.dm = self._make_staff("na_page_duty", "值班经理")

    def test_old_night_audit_page_redirects_to_center(self):
        """旧 /nightaudit/（staff_required 门禁）302 → /manage/night-audit/；
        细粒度权限在新页复核。"""
        self.client.force_login(self.fd)
        response = self.client.get("/nightaudit/")
        self.assertEqual(response.status_code, 302)
        self.assertURLEqual(response.url, "/manage/night-audit/")

    def test_center_page_requires_login_and_permission(self):
        response = self.client.get("/manage/night-audit/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

        self.client.force_login(self.fd)
        forbidden = self.client.get("/manage/night-audit/")
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.dm)
        allowed = self.client.get("/manage/night-audit/")
        self.assertEqual(allowed.status_code, 200)
        content = allowed.content.decode("utf-8")
        self.assertIn("营业日与夜审中心", content)
        self.assertIn("/api/v1/night-audit/business-dates", content)

    def test_reopen_select_loads_all_pages_of_closed_business_dates(self):
        self.client.force_login(self.dm)
        response = self.client.get("/manage/night-audit/")
        self.assertEqual(response.status_code, 200, response.content)
        content = response.content.decode("utf-8")
        self.assertIn('let hasNext = true', content)
        self.assertIn('page_size=100', content)
        self.assertIn('const maxRows = 5000', content)
        for control_id in ("na-bd-goto", "na-runs-goto", "na-bg-goto",
                           "na-bd-page-size", "na-runs-page-size", "na-bg-page-size"):
            self.assertIn('id="' + control_id + '"', content)

    def test_center_page_registered_in_management_centers(self):
        from apps.api.pages import MANAGEMENT_CENTERS
        config = MANAGEMENT_CENTERS["night-audit"]
        self.assertEqual(config["permission"], "hotelapp.night_audit_execute")
        self.assertEqual(config["template"], "pms/night_audit_center.html")
        self.assertEqual(config["endpoint"], "/api/v1/night-audit/business-dates")
