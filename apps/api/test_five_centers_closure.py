# -*- coding: utf-8 -*-
"""五大中心闭环补齐回归测试包（基线 2.0 §3.1-§3.5 闭环验收对照）。

对照《PMS2全业务中心闭环补齐开发基线2.0.md》五大中心闭环验收清单，
只补"既有实现缺测试证据"与"本轮补齐端点"的断言，全部走真实 HTTP 路由
与既有领域服务，不直接写业务模型状态（夹具仅允许测试数据排布）：

  * §3.1 员工中心：离职（set_status=TERMINATED）后会话失效、权限即时撤销、
    角色授权终止与全程审计（邀请激活链已有 apps/identity/test_invitations.py）。
  * §3.2 Guest 360：详情聚合（预订/Stay/Folio 账本余额/旧映射 LegacyRecordLink）、
    GET /guests/{id}/timeline 时间线排序、GET /guests/{id}/folios 摘要与权限。
  * §3.3 房态库存：维修停用 → 未来 RoomTypeInventory.out_of_service 同步
    （可用量下降/恢复回升/容量不足 fail-closed）；退房转 DIRTY → 清洁链 →
    净房恢复可售。
  * §3.4 会员中心：POST /memberships/{id}/status 暂停/恢复往返、幂等、
    冲突、等级历史、权限口径。
  * §3.5 预订与入住：POST /billing/shifts/open、/billing/shifts/{id}/close
    （差异说明强校验、幂等、物业隔离）；Folio 余额 vs LedgerEntry 合计 vs
    收款/退款三方对账一致性（结账门禁）。
"""
import json
from decimal import Decimal

from django.db.models import Sum
from django.test import Client, override_settings
from django.utils import timezone

from apps.billing.models import (
    CashierShift,
    Folio,
    LedgerEntry,
    Payment,
    Refund,
)
from apps.core.models import AuditEvent, LegacyRecordLink, Property
from apps.guests.models import Guest, GuestMembership
from apps.guests.services import GuestService
from apps.identity.authorization import AuthorizationService
from apps.identity.models import StaffProfile, StaffRoleAssignment
from apps.inventory.models import Room, RoomTypeInventory
from apps.reservations.models import Reservation, ReservationRoom, Stay

from .test_e2e_flows import E2EFlowTestBase


def _post_json(client, url, payload=None, idempotency_key=""):
    extra = {"HTTP_IDEMPOTENCY_KEY": idempotency_key} if idempotency_key else {}
    return client.post(url, data=json.dumps(payload or {}),
                       content_type="application/json", **extra)


def _service_guest(property_obj, full_name="闭环测试住客", phone="13911112222"):
    """测试夹具：经 GuestService 建档（不直接写模型）。"""
    return GuestService.create_guest(
        property_obj=property_obj, full_name=full_name, phone=phone)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class EmployeeTerminationClosureTests(E2EFlowTestBase):
    """§3.1 闭环：离职 → 会话失效 → 权限即时撤销 → 审计留存。"""

    def setUp(self):
        super().setUp()
        self.manager = self._make_staff("closure_tc_manager", "店长")
        self.target = self._make_staff("closure_tc_target", "前台")

    def _terminate_target(self, reason="合同到期离职"):
        profile = StaffProfile.objects.get(user=self.target)
        self.client.force_login(self.manager)
        response = self.client.post(
            f"/manage/staff/actions/{profile.pk}/set_status/",
            {"status": "TERMINATED", "reason": reason})
        self.assertEqual(response.status_code, 200, response.content)
        return profile

    def test_terminate_kills_session_revokes_permissions_and_audits(self):
        # 目标员工真实登录（非 force_login），持有可用会话与前台权限
        target_client = Client()
        logged_in = target_client.post("/login/", {
            "username": "closure_tc_target",
            "password": "closure_tc_target-pass123",
        })
        self.assertEqual(logged_in.status_code, 302)
        overview = target_client.get("/api/v1/front-desk/overview")
        self.assertEqual(overview.status_code, 200, overview.content)
        self.assertTrue(AuthorizationService.has_permission(
            self.target, self.property, "hotelapp.view_client"))

        active_assignments = StaffRoleAssignment.objects.filter(
            membership__staff_profile__user=self.target,
            ends_at__isnull=True)
        self.assertEqual(active_assignments.count(), 1,
                         "前置：目标员工应有 1 条长期有效岗位授权")

        profile = self._terminate_target()

        # 1) 档案/任职/账号：TERMINATED + 任职终止 + 账号停用
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status,
                         StaffProfile.EmploymentStatus.TERMINATED)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active, "离职必须同步停用登录账号")
        self.assertEqual(
            StaffRoleAssignment.objects.filter(
                membership__staff_profile=profile,
                ends_at__isnull=False).count(),
            1, "离职必须终止全部有效岗位授权（保留历史）")

        # 2) 权限即时撤销：AuthorizationService 口径为 False
        self.assertFalse(AuthorizationService.has_permission(
            self.target, self.property, "hotelapp.view_client"))
        self.assertFalse(AuthorizationService.has_permission(
            self.target, self.property, "hotelapp.change_accommodation"))

        # 3) 旧会话失效：同一 Cookie 再访问员工面 → 401（无半授权状态）
        stale = target_client.get("/api/v1/front-desk/overview")
        self.assertEqual(stale.status_code, 401, stale.content)
        self.assertEqual(
            stale.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

        # 4) 全程审计：identity.employment_status 记录离职事实
        event = AuditEvent.objects.filter(
            action="identity.employment_status",
            entity_type="StaffProfile",
            entity_id=str(profile.pk)).order_by("-id").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.after_json.get("status"), "TERMINATED")

    def test_terminated_employee_cannot_login_again(self):
        self._terminate_target(reason="离职测试")
        login = Client().post("/login/", {
            "username": "closure_tc_target",
            "password": "closure_tc_target-pass123"})
        self.assertEqual(login.status_code, 200)
        self.assertContains(
            login, "账号或密码不正确，或账号暂不可用",
            msg_prefix="离职账号再次登录必须统一拒绝文案: ")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class Guest360ClosureTests(E2EFlowTestBase):
    """§3.2 闭环：360 详情聚合 + timeline 时间线 + folios 摘要 + 旧映射可追溯。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("closure_g_frontdesk", "前台")
        self.viewer = self._make_finance_view_only("closure_g_noview")

    def _guest_of(self, booking_no):
        return Reservation.objects.get(booking_no=booking_no).guest

    def test_guest_detail_aggregates_reservations_stays_folios_and_legacy_links(self):
        booking_no, token, line, stay_id = self._booking_checked_in(self.fd)
        guest = self._guest_of(booking_no)
        LegacyRecordLink.objects.create(
            property=self.property, source_system="HOTELAPP_V1",
            source_model="Client", source_pk="900001",
            target_model="Guest", target_pk=str(guest.pk),
            mapping_status=LegacyRecordLink.MappingStatus.LINKED,
            match_method="人工确认")

        response = self.client.get(f"/api/v1/guests/{guest.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        # 预订摘要
        self.assertEqual(len(data["reservations"]), 1)
        self.assertEqual(data["reservations"][0]["booking_no"], booking_no)
        # Stay 摘要（在住）
        self.assertEqual(len(data["stays"]), 1)
        self.assertEqual(data["stays"][0]["id"], stay_id)
        self.assertEqual(data["stays"][0]["status"], Stay.Status.IN_HOUSE)
        self.assertIsNotNone(data["stays"][0]["actual_check_in"])
        # Folio 摘要（账本口径余额）
        self.assertEqual(len(data["folios"]), 1)
        folio = Folio.objects.get(reservation__booking_no=booking_no)
        expected_balance = sum(
            (e.amount for e in folio.entries.all()), Decimal("0.00"))
        self.assertEqual(Decimal(data["folios"][0]["balance"]), expected_balance)
        # 旧 Client 映射可追溯
        self.assertEqual(len(data["legacy_links"]), 1)
        self.assertEqual(data["legacy_links"][0]["source_model"], "Client")
        self.assertEqual(data["legacy_links"][0]["source_pk"], "900001")
        self.assertEqual(data["legacy_links"][0]["mapping_status"], "LINKED")

    def test_guest_timeline_orders_facts_and_includes_audit(self):
        booking_no, token, line, stay_id = self._booking_checked_in(self.fd)
        guest = self._guest_of(booking_no)
        self.client.force_login(self.fd)
        # 客人档案更新（写 guest.update 审计，entity_type=Guest）
        updated = _post_json(
            self.client, f"/api/v1/guests/{guest.pk}/update",
            {"notes": "客人要求高楼层安静房间"})
        self.assertEqual(updated.status_code, 200, updated.content)

        response = self.client.get(f"/api/v1/guests/{guest.pk}/timeline")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        kinds = [item["kind"] for item in data["items"]]
        # 六类事实中至少出现：预订、入住、账务、审计
        self.assertIn("reservation", kinds)
        self.assertIn("stay.check_in", kinds)
        self.assertIn("ledger_entry", kinds)
        self.assertIn("audit", kinds)
        # 审计事件包含档案更新动作；不返回 before/after 原文
        audit_items = [i for i in data["items"] if i["kind"] == "audit"]
        self.assertTrue(any(
            i["summary"]["action"] == "guest.update" for i in audit_items))
        for item in data["items"]:
            self.assertIn("time", item)
            self.assertIn("summary", item)
        # 时间倒序（同一时刻按业务序稳定输出）
        times = [item["time"] for item in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_guest_folios_endpoint_returns_ledger_balance_with_pagination(self):
        booking_no, token, line, stay_id = self._booking_with_outstanding(self.fd)
        guest = self._guest_of(booking_no)
        response = self.client.get(f"/api/v1/guests/{guest.pk}/folios")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn("meta", body)
        rows = body["data"]
        self.assertEqual(len(rows), 1)
        folio = Folio.objects.get(reservation__booking_no=booking_no)
        expected_balance = sum(
            (e.amount for e in folio.entries.all()), Decimal("0.00"))
        self.assertEqual(Decimal(rows[0]["balance"]), expected_balance)

    def test_guest_360_read_endpoints_permission_matrix(self):
        booking_no, token, line, stay_id = self._booking_checked_in(self.fd)
        guest = self._guest_of(booking_no)
        url = f"/api/v1/guests/{guest.pk}"
        # 匿名 → 403（AuthorizationService 默认拒绝；与既有 guest 端点同口径）
        self.client.logout()
        for path in (url, f"{url}/timeline", f"{url}/folios"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, path)
        # 有任职但无 view_client（仅 finance_access）→ 403
        self.client.force_login(self.viewer)
        for path in (url, f"{url}/timeline", f"{url}/folios"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403, path)
            self.assertEqual(
                response.json()["error"]["code"], "PERMISSION_DENIED", path)
        # 跨物业客人 → 404（不泄露存在性）
        other_property = Property.objects.create(code="CLOSURE-OTHER",
                                                 name="对照测试他店")
        other_guest = Guest.objects.create(
            property=other_property, full_name="他店住客", phone="13900000001")
        self.client.force_login(self.fd)
        for path in (f"/api/v1/guests/{other_guest.pk}",
                     f"/api/v1/guests/{other_guest.pk}/timeline",
                     f"/api/v1/guests/{other_guest.pk}/folios"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 404, path)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class MembershipStatusClosureTests(E2EFlowTestBase):
    """§3.4 闭环：会员暂停/恢复（POST /memberships/{id}/status）+ 等级历史。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("closure_m_frontdesk", "前台")
        self.fin = self._make_staff("closure_m_finance", "财务")
        self.client.force_login(self.fd)
        self.guest = _service_guest(self.property, phone="13911112222")
        response = _post_json(self.client, "/api/v1/memberships/create", {
            "guest_id": self.guest.pk, "member_no": "CLOSURE-M-0001",
            "level_code": "STANDARD"})
        self.assertEqual(response.status_code, 201, response.content)
        self.membership_id = response.json()["data"]["id"]

    def _status(self, action, reason="业务原因：客人申请暂停会员权益"):
        return _post_json(
            self.client, f"/api/v1/memberships/{self.membership_id}/status",
            {"action": action, "reason": reason})

    def test_suspend_restore_roundtrip_with_audit_and_idempotency(self):
        # 暂停：ACTIVE → SUSPENDED
        suspended = self._status("suspend")
        self.assertEqual(suspended.status_code, 200, suspended.content)
        data = suspended.json()["data"]
        self.assertTrue(data["changed"])
        self.assertEqual(data["status"], GuestMembership.Status.SUSPENDED)
        membership = GuestMembership.objects.get(pk=self.membership_id)
        self.assertEqual(membership.status, GuestMembership.Status.SUSPENDED)
        self.assertTrue(AuditEvent.objects.filter(
            action="membership.suspend",
            entity_type="GuestMembership",
            entity_id=str(self.membership_id)).exists())
        # 幂等：重复暂停 changed=false
        again = self._status("suspend", reason="重复暂停请求")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["data"]["changed"])
        # 恢复：SUSPENDED → ACTIVE
        restored = self._status("restore")
        self.assertEqual(restored.status_code, 200, restored.content)
        self.assertEqual(restored.json()["data"]["status"],
                         GuestMembership.Status.ACTIVE)
        membership.refresh_from_db()
        self.assertEqual(membership.status, GuestMembership.Status.ACTIVE)
        self.assertTrue(AuditEvent.objects.filter(
            action="membership.restore",
            entity_type="GuestMembership",
            entity_id=str(self.membership_id)).exists())

    def test_restore_conflicts_when_guest_has_other_active_membership(self):
        suspended = self._status("suspend")
        self.assertEqual(suspended.status_code, 200)
        # 暂停后同客人可再建有效会员（有效唯一键语义）
        second = _post_json(self.client, "/api/v1/memberships/create", {
            "guest_id": self.guest.pk, "member_no": "CLOSURE-M-0002",
            "level_code": "GOLD"})
        self.assertEqual(second.status_code, 201, second.content)
        # 恢复旧会员 → 与新有效会员冲突（人工处理，不静默覆盖）
        conflict = self._status("restore")
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertEqual(conflict.json()["error"]["code"], "BUSINESS_CONFLICT")
        membership = GuestMembership.objects.get(pk=self.membership_id)
        self.assertEqual(membership.status, GuestMembership.Status.SUSPENDED)

    def test_status_requires_reason_and_known_action(self):
        missing_reason = _post_json(
            self.client, f"/api/v1/memberships/{self.membership_id}/status",
            {"action": "suspend"})
        self.assertEqual(missing_reason.status_code, 409)
        self.assertEqual(
            missing_reason.json()["error"]["code"], "BUSINESS_CONFLICT")
        unknown = _post_json(
            self.client, f"/api/v1/memberships/{self.membership_id}/status",
            {"action": "freeze", "reason": "未知动作"})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"]["code"], "VALIDATION_ERROR")

    def test_status_requires_change_vip_permission(self):
        self.client.force_login(self.fin)  # 财务仅 view_vip
        response = self._status("suspend")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_level_change_writes_history_visible_in_detail(self):
        changed = _post_json(
            self.client, f"/api/v1/memberships/{self.membership_id}/level",
            {"level_code": "GOLD", "reason": "累计房晚达标升级"})
        self.assertEqual(changed.status_code, 200, changed.content)
        detail = self.client.get(f"/api/v1/memberships/{self.membership_id}")
        self.assertEqual(detail.status_code, 200)
        history = detail.json()["data"]["level_history"]
        self.assertTrue(history)
        self.assertEqual(history[0]["from_level"], "STANDARD")
        self.assertEqual(history[0]["to_level"], "GOLD")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class HousekeepingInventoryClosureTests(E2EFlowTestBase):
    """§3.3 闭环：维修停用同步库存、退房转 DIRTY、清洁链恢复可售。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("closure_hk_frontdesk", "前台")
        self.hk = self._make_staff("closure_hk_cleaner", "客房服务员")
        self.dm = self._make_staff("closure_hk_duty", "值班经理")

    def _inventory_rows(self):
        return list(RoomTypeInventory.objects.filter(
            property=self.property, room_type=self.room_type).order_by("stay_date"))

    def _maintain(self, room, target, reason):
        self.client.force_login(self.dm)
        return self.client.post(
            f"/api/v1/housekeeping/rooms/{room.pk}/maintenance",
            data=json.dumps({"status": target, "reason": reason}),
            content_type="application/json")

    def test_maintenance_out_of_order_syncs_future_inventory_out_of_service(self):
        room = self.room1
        response = self._maintain(room, "OUT_OF_ORDER", "空调漏水停用维修")
        self.assertEqual(response.status_code, 200, response.content)
        rows = self._inventory_rows()
        self.assertTrue(rows)
        for stock in rows:
            self.assertEqual(stock.out_of_service, 1,
                             f"{stock.stay_date} 库存应同步 +1 停用")
            self.assertEqual(stock.available_count, stock.total_rooms - 1)
        event = AuditEvent.objects.filter(
            action="maintenance.status_changed",
            entity_id=str(room.pk)).order_by("-id").first()
        self.assertEqual(
            event.after_json.get("inventory_out_of_service_rows_synced"),
            len(rows))
        room.refresh_from_db()
        self.assertFalse(room.is_sellable_now)

    def test_maintenance_restore_returns_inventory_out_of_service(self):
        room = self.room1
        register = self._maintain(room, "OUT_OF_SERVICE", "电路检修停用")
        self.assertEqual(register.status_code, 200, register.content)
        restore = self._maintain(room, "NORMAL", "检修完成验收恢复")
        self.assertEqual(restore.status_code, 200, restore.content)
        for stock in self._inventory_rows():
            self.assertEqual(stock.out_of_service, 0,
                             f"{stock.stay_date} 恢复后应回 0")

    def test_maintenance_fails_closed_when_inventory_exhausted(self):
        room = self.room1
        # 造一个"满房 + 1 间 Hold"的库存日（夹具直接排布库存行）
        stock = self._inventory_rows()[0]
        RoomTypeInventory.objects.filter(pk=stock.pk).update(
            total_rooms=1, held=1, out_of_service=0)
        response = self._maintain(room, "OUT_OF_ORDER", "漏水需停用维修")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["code"], "BUSINESS_CONFLICT")
        room.refresh_from_db()
        self.assertEqual(room.maintenance_status,
                         Room.MaintenanceStatus.NORMAL,
                         "fail-closed：库存不足时房间状态不得变更")
        stock.refresh_from_db()
        self.assertEqual(stock.out_of_service, 0,
                         "fail-closed：库存计数不得部分写入")

    def test_checkout_marks_dirty_then_cleaning_chain_restores_sellable(self):
        booking_no, token, line, stay_id = self._booking_checked_in(self.fd)
        line.refresh_from_db()  # 助手返回的是排房前取出的实例
        room = Room.objects.get(pk=line.assigned_room_id)
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.CLEAN)
        # 结清余额后退房
        balance = sum((e.amount for e in self._folio(booking_no).entries.all()),
                      Decimal("0.00"))
        if balance > 0:
            paid = self._collect_payment(
                booking_no, str(balance), f"closure-co-{booking_no}")
            self.assertEqual(paid.status_code, 201, paid.content)
        checkout = _post_json(
            self.client, f"/api/v1/front-desk/stays/{stay_id}/check-out")
        self.assertEqual(checkout.status_code, 200, checkout.content)
        room.refresh_from_db()
        # 退房自动转 DIRTY（§3.3 闭环锚点）
        self.assertEqual(room.occupancy_status, Room.OccupancyStatus.VACANT)
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.DIRTY)
        self.assertFalse(room.is_sellable_now)
        # 清洁链：客房服务员流转 → 值班经理净房放行 → 恢复可售
        self.client.force_login(self.hk)
        for target in ("CLEANING", "INSPECTING"):
            step = self.client.post(
                f"/api/v1/housekeeping/rooms/{room.pk}/status",
                data=json.dumps({"status": target}),
                content_type="application/json")
            self.assertEqual(step.status_code, 200, step.content)
        self.client.force_login(self.dm)
        release = self.client.post(
            f"/api/v1/housekeeping/rooms/{room.pk}/status",
            data=json.dumps({"status": "CLEAN"}),
            content_type="application/json")
        self.assertEqual(release.status_code, 200, release.content)
        room.refresh_from_db()
        self.assertEqual(room.housekeeping_status,
                         Room.HousekeepingStatus.CLEAN)
        # is_sellable_now 是 cached_property：取新实例核对（此前访问过旧值）
        room = Room.objects.get(pk=room.pk)
        self.assertTrue(room.is_sellable_now,
                        "净房放行后必须恢复可售口径")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class CashierShiftClosureTests(E2EFlowTestBase):
    """§3.5 闭环：收银班次开班/关班 API（BillingService 唯一写入口）。"""

    def setUp(self):
        super().setUp()
        self.fin = self._make_staff("closure_cs_finance", "财务")
        # 前台无 hotelapp.finance_access，但可操作本人收银班次。
        self.nofin = self._make_staff("closure_cs_front", "前台")
        self.fd = self.nofin

    def test_open_shift_creates_and_replay_is_idempotent(self):
        self.client.force_login(self.fin)
        first = _post_json(self.client, "/api/v1/billing/shifts/open",
                           {"opening_cash": "200.00"})
        self.assertEqual(first.status_code, 201, first.content)
        data = first.json()["data"]
        self.assertTrue(data["created"])
        self.assertEqual(data["status"], CashierShift.ShiftStatus.OPEN)
        self.assertEqual(Decimal(data["opening_cash"]), Decimal("200.00"))
        self.assertTrue(AuditEvent.objects.filter(
            action="billing.shift_open",
            entity_type="CashierShift",
            entity_id=str(data["id"])).exists())
        # 同一收银员重复开班 → 幂等返回原班次
        replay = _post_json(self.client, "/api/v1/billing/shifts/open",
                            {"opening_cash": "200.00"})
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertFalse(replay.json()["data"]["created"])
        self.assertEqual(replay.json()["data"]["id"], data["id"])

    def test_close_shift_records_variance_and_requires_reason(self):
        self.client.force_login(self.fin)
        opened = _post_json(self.client, "/api/v1/billing/shifts/open",
                            {"opening_cash": "100.00"})
        shift_id = opened.json()["data"]["id"]
        # 差异非零且无说明 → 400，班次保持 OPEN
        denied = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/close",
            {"actual_cash": "80.00"})
        self.assertEqual(denied.status_code, 400, denied.content)
        self.assertEqual(denied.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(
            CashierShift.objects.get(pk=shift_id).status,
            CashierShift.ShiftStatus.OPEN)
        # 补充差异说明后关班成功
        closed = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/close",
            {"actual_cash": "80.00", "variance_reason": "现金短缺 20 元待查"})
        self.assertEqual(closed.status_code, 200, closed.content)
        data = closed.json()["data"]
        self.assertEqual(data["status"], CashierShift.ShiftStatus.CLOSED)
        self.assertEqual(Decimal(data["variance"]), Decimal("-20.00"))
        self.assertTrue(AuditEvent.objects.filter(
            action="billing.shift_close",
            entity_type="CashierShift",
            entity_id=str(shift_id)).exists())
        # 重复关班 → 400
        again = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/close",
            {"actual_cash": "80.00", "variance_reason": "重复关班"})
        self.assertEqual(again.status_code, 400)

    def test_expected_cash_uses_cash_payments_refunds_and_cash_movements(self):
        from apps.reservations.models import Reservation
        booking_no, _, _, _ = self._booking_with_outstanding()
        reservation = Reservation.objects.get(booking_no=booking_no)
        self.client.force_login(self.fin)
        opened = _post_json(
            self.client, "/api/v1/billing/shifts/open",
            {"opening_cash": "100.00"})
        self.assertEqual(opened.status_code, 201, opened.content)
        shift_id = opened.json()["data"]["id"]

        collected = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "100.00",
             "method": "CASH", "reference": "closure-cash-payment-001"})
        self.assertEqual(collected.status_code, 201, collected.content)
        payment = Payment.objects.get(pk=collected.json()["data"]["payment_id"])
        self.assertEqual(payment.cashier_shift_id, shift_id)

        cash_in = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/movements",
            {"direction": "IN", "amount": "5.00", "reason": "补入备用现金",
             "idempotency_key": "closure-cash-in-001"})
        self.assertEqual(cash_in.status_code, 201, cash_in.content)
        cash_out = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/movements",
            {"direction": "OUT", "amount": "10.00", "reason": "现金支出",
             "idempotency_key": "closure-cash-out-001"})
        self.assertEqual(cash_out.status_code, 201, cash_out.content)

        closed = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/close",
            {"actual_cash": "195.00"})
        self.assertEqual(closed.status_code, 200, closed.content)
        data = closed.json()["data"]
        self.assertEqual(Decimal(data["expected_cash"]), Decimal("195.00"))
        self.assertEqual(Decimal(data["variance"]), Decimal("0.00"))
        shift = CashierShift.objects.get(pk=shift_id)
        self.assertEqual(shift.closed_by_id, self.fin.pk)

        # 关班后不能继续登记现金支出或把新收款归入已关闭班次。
        rejected = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/movements",
            {"direction": "OUT", "amount": "1.00", "reason": "关班后不得追加",
             "idempotency_key": "closure-cash-out-after-close"})
        self.assertEqual(rejected.status_code, 400)

    def test_shift_endpoints_use_cashier_permission_and_property_scope(self):
        # 前台没有 finance_access，但拥有本人班次开闭专项权。
        self.client.force_login(self.nofin)
        response = _post_json(self.client, "/api/v1/billing/shifts/open",
                              {"opening_cash": "0.00"})
        self.assertEqual(response.status_code, 201, response.content)
        shift_id = response.json()["data"]["id"]
        closed = _post_json(
            self.client, f"/api/v1/billing/shifts/{shift_id}/close",
            {"actual_cash": "0.00"})
        self.assertEqual(closed.status_code, 200, closed.content)

        # 跨物业班次仍然默认拒绝 → 404
        self.client.force_login(self.fin)
        other_property = Property.objects.create(code="CLOSURE-SHIFT-OTHER",
                                                 name="班次隔离测试他店")
        other_shift = CashierShift.objects.create(
            property=other_property, cashier=self.fin,
            opening_cash=Decimal("50.00"))
        response = _post_json(
            self.client, f"/api/v1/billing/shifts/{other_shift.pk}/close",
            {"actual_cash": "50.00"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")
        other_shift.refresh_from_db()
        self.assertEqual(other_shift.status, CashierShift.ShiftStatus.OPEN)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ReconciliationConsistencyTests(E2EFlowTestBase):
    """§3.5 闭环：Folio 余额 vs LedgerEntry 合计 vs 收款/退款三方对账一致。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("closure_rc_frontdesk", "前台")
        self.owner = self._make_staff("closure_rc_owner", "店长")
        # 财务账号：billing/folios 读面需 hotelapp.finance_access
        self.fin = self._make_staff("closure_rc_finance", "财务")

    @staticmethod
    def _ledger_sum(folio):
        return sum((e.amount for e in folio.entries.all()), Decimal("0.00"))

    def _api_folio_balance(self, booking_no):
        """以财务账号读账单余额（finance_access 门禁）；不影响调用方会话。"""
        finance_client = Client()
        finance_client.force_login(self.fin)
        response = finance_client.get("/api/v1/billing/folios",
                                      {"booking_no": booking_no})
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["data"]
        self.assertEqual(len(rows), 1)
        return Decimal(rows[0]["balance"])

    def _refund_hundred(self, booking_no):
        """前台申请退 100 → 店长审批执行（双人分离）；返回后恢复前台会话。"""
        folio = self._folio(booking_no)
        payment = Payment.objects.filter(
            folio=folio, status=Payment.Status.SUCCEEDED).order_by("-id").first()
        self.assertIsNotNone(payment)
        request_resp = _post_json(
            self.client,
            f"/api/v1/front-desk/payments/{payment.pk}/refunds",
            {"amount": "100.00", "reason": "客人多付房费，协商退款 100 元"},
            idempotency_key=f"closure-refund-{booking_no}")
        self.assertEqual(request_resp.status_code, 201, request_resp.content)
        refund_id = request_resp.json()["data"]["refund_id"]
        self.client.force_login(self.owner)
        approved = _post_json(
            self.client, f"/api/v1/front-desk/refunds/{refund_id}/approve",
            {"confirm_refund_paid": True})
        self.assertEqual(approved.status_code, 200, approved.content)
        self.client.force_login(self.fd)
        return refund_id

    def test_folio_balance_matches_ledger_and_cash_flow_after_refund_cycle(self):
        # 预付一晚入住 + 续住一晚（应收 300）→ 收 200 → 退 100
        booking_no, token, line, stay_id = self._booking_with_outstanding(self.fd)
        collected = self._collect_payment(
            booking_no, "200.00", f"closure-rc1-{booking_no}")
        self.assertEqual(collected.status_code, 201, collected.content)
        self._refund_hundred(booking_no)

        folio = self._folio(booking_no)
        entries = list(folio.entries.all())
        ledger_sum = self._ledger_sum(folio)
        api_balance = self._api_folio_balance(booking_no)

        # 一致 1：API 账本口径余额 == LedgerEntry 带符号合计
        self.assertEqual(api_balance, ledger_sum)

        def cash_flow_totals(f):
            # 实收现金流入：SUCCEEDED 及其后退款衍生状态（部分退款/全额退款
            # 的资金仍已到账；失败/未付意图不计——基线 §4.5 实收口径）
            delivered = (Payment.Status.SUCCEEDED, Payment.Status.REFUND_PENDING,
                         Payment.Status.PARTIALLY_REFUNDED,
                         Payment.Status.REFUNDED)
            paid = Payment.objects.filter(
                folio=f, status__in=delivered).aggregate(
                total=Sum("amount"))["total"] or Decimal("0.00")
            refunded = Refund.objects.filter(
                payment__folio=f, status=Refund.Status.SUCCEEDED).aggregate(
                total=Sum("amount"))["total"] or Decimal("0.00")
            charges_posted = sum(
                (e.amount for e in f.entries.all()
                 if e.entry_type == LedgerEntry.EntryType.ROOM_CHARGE),
                Decimal("0.00"))
            return paid, refunded, charges_posted

        paid, refunded, charges_posted = cash_flow_totals(folio)
        # 一致 2：业务现金流恒等式
        #   账本余额 = 已入账房费(ROOM_CHARGE) - 成功收款 + 已执行退款
        #   （此时房费尚未夜审/退房入账 → charges_posted = 0）
        self.assertEqual(charges_posted, Decimal("0.00"))
        self.assertEqual(ledger_sum, charges_posted - paid + refunded)
        # 一致 3：账本正负分项自洽（正项合计 - 负项合计 == 余额）
        positive = sum((e.amount for e in entries if e.amount > 0),
                       Decimal("0.00"))
        negative = sum((-e.amount for e in entries if e.amount < 0),
                       Decimal("0.00"))
        self.assertEqual(ledger_sum, positive - negative)
        # 未结清（退房未入账的房费 + 退款后余额）→ 退房被账本门禁阻断
        blocked = _post_json(
            self.client, f"/api/v1/front-desk/stays/{stay_id}/check-out")
        self.assertEqual(blocked.status_code, 409, blocked.content)
        # 退房房费入账后应收 = ReservationRoom 成交合计 - 已收 + 已退；
        # 按独立事实源结清（不读账本推算，避免循环口径）
        reservation_total = ReservationRoom.objects.filter(
            property=self.property,
            reservation=folio.reservation).aggregate(
            total=Sum("amount"))["total"] or Decimal("0.00")
        folio.refresh_from_db()
        paid, refunded, _ = cash_flow_totals(folio)
        outstanding = reservation_total - paid + refunded
        self.assertGreater(outstanding, Decimal("0.00"))
        final_pay = self._collect_payment(
            booking_no, str(outstanding), f"closure-rc2-{booking_no}")
        self.assertEqual(final_pay.status_code, 201, final_pay.content)
        checkout = _post_json(
            self.client, f"/api/v1/front-desk/stays/{stay_id}/check-out")
        self.assertEqual(checkout.status_code, 200, checkout.content)
        folio.refresh_from_db()
        self.assertEqual(folio.status, Folio.Status.CLOSED)
        # 结账后三方归零：账本 == 房费 - 全部收款 + 全部退款 == 0
        paid, refunded, charges_posted = cash_flow_totals(folio)
        self.assertEqual(charges_posted, reservation_total)
        self.assertEqual(
            self._ledger_sum(folio), charges_posted - paid + refunded)
        self.assertEqual(self._ledger_sum(folio), Decimal("0.00"))
        self.assertEqual(self._api_folio_balance(booking_no), Decimal("0.00"))

    def test_refund_amount_capped_at_payment_prevents_ledger_drift(self):
        booking_no, token, line, stay_id = self._booking_with_outstanding(self.fd)
        collected = self._collect_payment(
            booking_no, "100.00", f"closure-rc3-{booking_no}")
        self.assertEqual(collected.status_code, 201, collected.content)
        folio = self._folio(booking_no)
        payment = Payment.objects.filter(
            folio=folio, status=Payment.Status.SUCCEEDED).order_by("-id").first()
        # 超额退款申请 → 拒绝且无账本副作用
        before_sum = self._ledger_sum(folio)
        over = _post_json(
            self.client,
            f"/api/v1/front-desk/payments/{payment.pk}/refunds",
            {"amount": "999.00", "reason": "超额退款必须被拒绝"},
            idempotency_key=f"closure-rc3-over-{booking_no}")
        self.assertEqual(over.status_code, 409, over.content)
        folio.refresh_from_db()
        self.assertEqual(self._ledger_sum(folio), before_sum)



class ManagementCenterPaginationPageTests(E2EFlowTestBase):
    """共用管理中心分页控件：页码跳转和每页数量选择。"""

    def test_generic_management_center_exposes_page_size_and_page_jump(self):
        owner = self._make_staff("pms_pager_owner", "店长")
        self.client.force_login(owner)
        response = self.client.get("/manage/rooms/")
        self.assertEqual(response.status_code, 200, response.content)
        content = response.content.decode("utf-8")
        self.assertIn('id="pms-page-size"', content)
        self.assertIn('id="pms-goto-page"', content)
        self.assertIn('id="pms-goto-btn"', content)
        self.assertIn('page_size:String(currentPageSize)', content)
        self.assertIn("gotoPageInput.setCustomValidity", content)
