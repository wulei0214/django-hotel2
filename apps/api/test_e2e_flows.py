# -*- coding: utf-8 -*-
"""PMS2 平台治理 Phase 4 端到端回归测试包（实施方案 §9.3-§9.5）。

三条被测链路（全部走 Django test client 的真实 HTTP 路由级请求，
断言除 HTTP 状态外还覆盖数据库副作用与 AuditEvent 审计）：

  * P0-B（§9.3）PaymentDepositRefundReversalE2EFlowTests
      公开预订 → 排房/入住 → 收款/押金 → 退款申请/审批（SoD P1-02）→ 红字冲账。
  * P0-C（§9.4）NightAuditBusinessDateE2EFlowTests
      夜审预览/执行/幂等 → 营业日重开双人审批 → break-glass 全生命周期 →
      夜审后新入住的营业日关账阻挡（§4.2 已收口，详见
      apps/api/test_night_audit_center.py）。
  * P0-D（§9.5）ExceptionFulfillmentE2EFlowTests
      已入住/已确认数据上的取消冲突、No-Show、续住、换房、
      退房结算（快捷退房退役语义）与反结账缺口（F-08）。

与 §9 草稿口径不一致的现有行为（按现有行为断言，差异登记在测试报告中）：
  * 夜审同营业日重复执行：200 + created=false（幂等成功），非 400/409；
  * 重开营业日缺理由：409 BUSINESS_CONFLICT，非 400；
  * 续住新离店日期早于原日期：409 BUSINESS_CONFLICT，非 400；
  * 换房同房请求：200 + changed=false（无副作用），非 400；
  * 夜审后新入住：原缺口（无营业日写入门禁）已按 §4.2 阻挡规则收口——
    营业日 CLOSED 后入住 409（排房不受限），见 test_night_audit_center.py；
  * 冲账（BillingService.reverse_entry）：无 HTTP 入口（F-08），服务级调用验证。
"""
import json
import re
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission, User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.billing.models import (
    ApprovalRecord,
    Folio,
    LedgerEntry,
    Payment,
    Refund,
)
from apps.billing.services import sign_mock_callback
from apps.billing.services_v2 import BillingService
from apps.core.models import (
    AuditEvent,
    BreakGlassRecord,
    BusinessDate,
    NightAuditRun,
    Property,
)
from apps.identity.authorization import AuthorizationService
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.inventory.models import (
    DailyRate,
    RatePlan,
    Room,
    RoomType,
    RoomTypeInventory,
)
from apps.reservations.models import (
    NoShowRecord,
    Reservation,
    ReservationRoom,
    Stay,
    StayRoomSegment,
)

NIGHTLY_PRICE = Decimal("300.00")
NIGHTS = 2


@override_settings(
    DEBUG=True,
    DJANGO_ENV="test",
    PAYMENT_WEBHOOK_SECRET="e2e-webhook-secret",
)
class E2EFlowTestBase(TestCase):
    """公共基础设施：DEFAULT 物业 + 库存日历 + 员工账号工厂 + HTTP 助手。"""

    maxDiff = None

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self._setup_inventory()
        self._seq = 0

    # ------------------------------------------------------------------
    # 基础数据
    # ------------------------------------------------------------------
    def _setup_inventory(self):
        self.room_type = RoomType.objects.create(
            property=self.property,
            code="E2E-KING",
            name="E2E 测试大床房",
            capacity=2,
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property,
            code="E2E-BAR",
            name="E2E 测试门市价",
            valid_from=self.today - timedelta(days=1),
        )
        self.inventory_dates = [self.today + timedelta(days=offset) for offset in range(4)]
        for day in self.inventory_dates:
            DailyRate.objects.create(
                property=self.property,
                rate_plan=self.rate_plan,
                room_type=self.room_type,
                stay_date=day,
                price=NIGHTLY_PRICE,
            )
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.room_type,
                stay_date=day,
                total_rooms=5,
            )
        self.room1 = self._make_room("E2E-8201", floor="8")
        self.room2 = self._make_room("E2E-8202", floor="8")
        self.room3 = self._make_room("E2E-8203", floor="8")

    def _make_room(self, number, floor="8"):
        return Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number=number,
            floor=floor,
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )

    # ------------------------------------------------------------------
    # 员工账号工厂（对齐 hotelapp.tests._make_user 的身份链）
    # ------------------------------------------------------------------
    def _make_staff(self, username, group_name):
        """User + StaffProfile + DEFAULT Membership + RoleAssignment，不用 is_staff。"""
        user = User.objects.create_user(username=username, password=f"{username}-pass123")
        profile = StaffProfile.objects.create(
            user=user, full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.property,
            employee_no=f"E2E-{username}",
            status=PropertyStaffMembership.Status.ACTIVE)
        group = Group.objects.get(name=group_name)
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=timezone.now() - timedelta(minutes=1))
        return user

    def _make_finance_view_only(self, username):
        """仅持有 hotelapp.finance_access 的账号（P1-02 权限分离断言用）。"""
        group, _ = Group.objects.get_or_create(name="E2E-仅财务查看")
        permission = Permission.objects.get(
            content_type__app_label="hotelapp", codename="finance_access")
        group.permissions.add(permission)
        user = User.objects.create_user(username=username, password=f"{username}-pass123")
        profile = StaffProfile.objects.create(
            user=user, full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.property,
            employee_no=f"E2E-{username}",
            status=PropertyStaffMembership.Status.ACTIVE)
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=timezone.now() - timedelta(minutes=1))
        return user

    # ------------------------------------------------------------------
    # HTTP 助手
    # ------------------------------------------------------------------
    def _post_json(self, url, payload=None, idempotency_key=""):
        extra = {"HTTP_IDEMPOTENCY_KEY": idempotency_key} if idempotency_key else {}
        return self.client.post(
            url, data=json.dumps(payload or {}), content_type="application/json", **extra)

    def _next_phone(self):
        self._seq += 1
        return f"138{self._seq:08d}"

    def _create_reservation(self, nights=NIGHTS):
        """公开预订：POST /api/v1/reservations（幂等键 + access_token 一次性返回）。"""
        self.client.logout()
        check_in = self.today
        check_out = self.today + timedelta(days=nights)
        response = self.client.post(
            "/api/v1/reservations",
            data=json.dumps({
                "full_name": "E2E 测试住客",
                "phone": self._next_phone(),
                "room_type_id": self.room_type.pk,
                "rate_plan_id": self.rate_plan.pk,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "rooms": 1,
                "adults": 1,
                "children": 0,
            }),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY=f"e2e-create-{uuid.uuid4().hex[:16]}",
        )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body.get("access_token"), "创建响应必须一次性返回访问令牌")
        return body["booking_no"], body["access_token"]

    def _confirm_by_mock_payment(self, booking_no, token):
        """模拟支付确认：支付意图 + 签名回调，预订 PENDING → CONFIRMED。"""
        intent = self.client.post(
            "/api/v1/payments/intents",
            data=json.dumps({"booking_no": booking_no, "token": token}),
            content_type="application/json",
        )
        self.assertEqual(intent.status_code, 201, intent.content)
        payload = {
            "provider_ref": intent.json()["provider_ref"],
            "amount": intent.json()["amount"],
            "status": "SUCCESS",
        }
        raw_body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        callback = self.client.post(
            "/api/v1/payments/callback",
            data=raw_body,
            content_type="application/json",
            HTTP_X_SIGNATURE=sign_mock_callback(raw_body),
        )
        self.assertEqual(callback.status_code, 200, callback.content)
        self.assertEqual(callback.json()["reservation_status"], "CONFIRMED")

    def _line(self, booking_no):
        return ReservationRoom.objects.get(reservation__booking_no=booking_no)

    def _assign(self, line, room):
        response = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/assign",
            {"room_id": room.pk})
        self.assertEqual(response.status_code, 201, response.content)
        return response

    def _check_in(self, line):
        response = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/check-in")
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]["stay_id"]

    def _assign_and_check_in(self, line, room):
        self._assign(line, room)
        stay_id = self._check_in(line)
        # F-03 六个前台核心动作的 DB 审计（排房/入住在链路助手上统一断言）
        self.assertTrue(self._audit_exists(
            "frontdesk.room_assigned", entity_id=line.pk,
            property_obj=self.property))
        self.assertTrue(self._audit_exists(
            "frontdesk.checked_in", entity_id=stay_id, property_obj=self.property))
        return stay_id

    def _booking_checked_in(self, staff_user=None):
        """公开预订 → 模拟支付确认 → 排房 → 入住，返回 (booking_no, token, line, stay_id)。

        注意：公开面模拟支付总是支付订单全额，确认后账单带等额收款贷方
        （余额 = -total_amount，应收余额 0）。前台人工收款/退款链路需要
        应收余额，用 _booking_with_outstanding 先续住追加房费。
        """
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        line = self._line(booking_no)
        self.client.force_login(staff_user or self.fd)
        stay_id = self._assign_and_check_in(line, self.room1)
        return booking_no, token, line, stay_id

    def _booking_with_outstanding(self, staff_user=None):
        """一晚预订全额预付入住后续住一晚：应收余额 = 新增晚数的实时日价。

        返回 (booking_no, token, line, stay_id)；此时前台可人工收款。
        """
        booking_no, token = self._create_reservation(nights=1)
        self._confirm_by_mock_payment(booking_no, token)
        line = self._line(booking_no)
        self.client.force_login(staff_user or self.fd)
        stay_id = self._assign_and_check_in(line, self.room1)
        extended = self._post_json(
            f"/api/v1/front-desk/stays/{stay_id}/extend",
            {"new_check_out": (self.today + timedelta(days=2)).isoformat(),
             "reason": "客人续住一晚，到店补收房费"})
        self.assertEqual(extended.status_code, 200, extended.content)
        return booking_no, token, line, stay_id

    def _open_cashier_shift(self, key):
        """现金交易前显式开班；班次归属当前登录收银员。"""
        return self._post_json(
            "/api/v1/billing/shifts/open", {"opening_cash": "0.00"},
            idempotency_key=key)

    def _collect_payment(self, booking_no, amount, key):
        """前台人工收款（当前登录账号须为前台写权限账号）。"""
        opened = self._open_cashier_shift(f"shift-{key}")
        if opened.status_code not in (200, 201):
            return opened
        return self._post_json(
            f"/api/v1/front-desk/reservations/{booking_no}/payments",
            {"amount": amount, "method": "CASH"},
            idempotency_key=key)

    def _folio(self, booking_no):
        return Folio.objects.get(reservation__booking_no=booking_no)

    @staticmethod
    def _folio_balance(folio):
        return sum(
            (entry.amount for entry in folio.entries.all()), Decimal("0.00"))

    @staticmethod
    def _audit_exists(action, entity_id=None, property_obj=None):
        condition = {"action": action}
        if entity_id is not None:
            condition["entity_id"] = str(entity_id)
        if property_obj is not None:
            condition["property_id"] = property_obj.pk
        return AuditEvent.objects.filter(**condition).exists()


class SessionCsrfTemplateTokenTests(E2EFlowTestBase):
    """Session CSRF 模式下，PMS2 页面内令牌必须能完成 POST。"""

    @override_settings(CSRF_USE_SESSIONS=True)
    def test_notification_page_token_is_accepted_and_wrong_token_is_rejected(self):
        staff = self._make_staff("session_csrf_owner", "店长")
        client = Client(enforce_csrf_checks=True)
        client.force_login(staff)

        page = client.get("/manage/notifications/")
        self.assertEqual(page.status_code, 200, page.content)
        match = re.search(
            rb'name="csrfmiddlewaretoken" value="([^"]+)"',
            page.content)
        self.assertIsNotNone(match, "通知页面必须渲染 Session CSRF 隐藏字段")
        token = match.group(1).decode("ascii")

        accepted = client.post(
            "/api/v1/notifications/read-all",
            data="{}", content_type="application/json",
            HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 200, accepted.content)

        rejected = client.post(
            "/api/v1/notifications/refresh",
            data="{}", content_type="application/json",
            HTTP_X_CSRFTOKEN="invalid-csrf-token")
        self.assertEqual(rejected.status_code, 403)


class FrontDeskDrawerStatusDtoTests(E2EFlowTestBase):
    """前台详情抽屉：预订确认状态与实际入住状态必须分别表达。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("drawer_status_frontdesk", "前台")

    def test_frontdesk_summary_aggregates_open_folios_and_excludes_closed_folios(self):
        booking_no, _, _, _ = self._booking_checked_in()
        reservation = Reservation.objects.get(booking_no=booking_no)
        master = Folio.objects.get(
            reservation=reservation, folio_type=Folio.FolioType.MASTER)

        guest_folio = Folio.objects.create(
            property=self.property, reservation=reservation,
            folio_type=Folio.FolioType.GUEST, guest=reservation.guest,
            status=Folio.Status.OPEN, currency=reservation.currency)
        LedgerEntry.objects.create(
            property=self.property, folio=guest_folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=Decimal("100.00"), currency=reservation.currency,
            business_date=self.today, reference="e2e-extra-open-folio-charge")

        closed_folio = Folio.objects.create(
            property=self.property, reservation=reservation,
            folio_type=Folio.FolioType.COMPANY, guest=reservation.guest,
            status=Folio.Status.CLOSED, currency=reservation.currency)
        LedgerEntry.objects.create(
            property=self.property, folio=closed_folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=Decimal("999.00"), currency=reservation.currency,
            business_date=self.today, reference="e2e-closed-folio-charge")

        self.client.force_login(self.fd)
        response = self.client.get(
            f"/api/v1/front-desk/reservations/{booking_no}")
        self.assertEqual(response.status_code, 200, response.content)
        summary = response.json()["data"]["folio_summary"]
        self.assertEqual(summary["folio_id"], master.pk)
        self.assertEqual(summary["folio_count"], 3)
        self.assertEqual(summary["open_folio_count"], 2)
        self.assertEqual(Decimal(summary["charges"]), Decimal("100.00"))
        self.assertEqual(Decimal(summary["credits"]), reservation.total_amount)
        self.assertEqual(
            Decimal(summary["balance"]),
            Decimal("100.00") - reservation.total_amount)

    def test_confirmed_reservation_shows_pending_stay_until_check_in(self):
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        line = self._line(booking_no)
        self.client.force_login(self.fd)

        url = f"/api/v1/front-desk/reservations/{booking_no}"
        before = self.client.get(url)
        self.assertEqual(before.status_code, 200, before.content)
        before_room = before.json()["data"]["rooms"][0]
        self.assertEqual(before.json()["data"]["status"], Reservation.Status.CONFIRMED)
        self.assertIsNone(before_room["stay"])
        self.assertEqual(before_room["stay_status_label"], "待办理入住")

        self._assign_and_check_in(line, self.room1)
        after = self.client.get(url)
        self.assertEqual(after.status_code, 200, after.content)
        after_room = after.json()["data"]["rooms"][0]
        self.assertEqual(after_room["stay"]["status"], Stay.Status.IN_HOUSE)
        self.assertNotEqual(after_room["stay_status_label"], "待办理入住")


class PaymentDepositRefundReversalE2EFlowTests(E2EFlowTestBase):
    """P0-B（§9.3）：收款 / 押金 / 退款双权分离 / 红字冲账全链路。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("e2e_b_frontdesk", "前台")
        self.owner = self._make_staff("e2e_b_owner", "店长")
        self.fin = self._make_staff("e2e_b_finance", "财务")

    # ------------------------------------------------------------------
    # 1) 收款：Payment/LedgerEntry 落库 + Folio 余额变化 + 幂等重放
    # ------------------------------------------------------------------
    def test_collect_payment_creates_payment_ledger_and_replay_is_idempotent(self):
        booking_no, _, _, stay_id = self._booking_with_outstanding()
        self.client.force_login(self.fd)
        folio = self._folio(booking_no)
        # 预付一晚（模拟支付 -300 贷方），续住一晚后应收 300
        self.assertEqual(self._folio_balance(folio), Decimal("-300.00"))

        response = self._collect_payment(booking_no, "300.00", "e2e-pay-001")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body["created"])
        data = body["data"]
        self.assertEqual(data["status"], Payment.Status.SUCCEEDED)
        self.assertEqual(data["currency"], "CNY")

        payment = Payment.objects.get(pk=data["payment_id"])
        self.assertEqual(payment.provider, "frontdesk_manual")
        self.assertEqual(payment.method, "CASH")
        self.assertEqual(payment.amount, Decimal("300.00"))
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(payment.folio_id, folio.pk)
        self.assertIsNotNone(payment.paid_at)

        entry = LedgerEntry.objects.get(reference=f"payment:{payment.provider_ref}")
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.PAYMENT)
        self.assertEqual(entry.amount, Decimal("-300.00"))
        self.assertEqual(entry.currency, "CNY")
        self.assertEqual(entry.folio_id, folio.pk)
        folio = self._folio(booking_no)
        self.assertEqual(self._folio_balance(folio), Decimal("-600.00"))
        self.assertTrue(self._audit_exists(
            "billing.collect", entity_id=payment.pk, property_obj=self.property))

        replay = self._collect_payment(booking_no, "300.00", "e2e-pay-001")
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertFalse(replay.json()["created"])
        manual_payments = Payment.objects.filter(
            reservation__booking_no=booking_no, provider="frontdesk_manual")
        self.assertEqual(manual_payments.count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=folio,
                reference=f"payment:FD{self.property.pk}-e2e-pay-001").count(), 1)
        # 预付（mock）+ 人工收款各一条 PAYMENT 贷方凭证
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=folio, entry_type=LedgerEntry.EntryType.PAYMENT).count(), 2)

    # ------------------------------------------------------------------
    # 2) 押金：record_deposit 语义（押金凭证 + reservation.deposit_amount）
    # ------------------------------------------------------------------
    def test_receive_deposit_records_deposit_semantics_and_caps_at_total(self):
        booking_no, _, _, _ = self._booking_checked_in()
        self.client.force_login(self.fd)
        reservation = Reservation.objects.get(booking_no=booking_no)
        self.assertEqual(reservation.deposit_amount, Decimal("0.00"))
        self.assertIn(self._open_cashier_shift("e2e-shift-deposit-001").status_code,
                      (200, 201))

        response = self._post_json(
            f"/api/v1/front-desk/reservations/{booking_no}/deposits",
            {"amount": "200.00", "method": "CASH"},
            idempotency_key="e2e-dep-001")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body["created"])
        data = body["data"]
        self.assertEqual(data["payment_kind"], "DEPOSIT")

        payment = Payment.objects.get(pk=data["payment_id"])
        self.assertEqual(payment.provider, "frontdesk_deposit")
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        self.assertEqual(payment.amount, Decimal("200.00"))
        entry = LedgerEntry.objects.get(reference=f"deposit:{payment.provider_ref}")
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.DEPOSIT)
        self.assertEqual(entry.amount, Decimal("-200.00"))
        reservation.refresh_from_db()
        self.assertEqual(reservation.deposit_amount, Decimal("200.00"))
        self.assertTrue(self._audit_exists(
            "billing.deposit_collect", entity_id=payment.pk,
            property_obj=self.property))

        replay = self._post_json(
            f"/api/v1/front-desk/reservations/{booking_no}/deposits",
            {"amount": "200.00", "method": "CASH"},
            idempotency_key="e2e-dep-001")
        self.assertEqual(replay.status_code, 200)
        self.assertFalse(replay.json()["created"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.deposit_amount, Decimal("200.00"))

        # 累计押金不得超过预订房费总额（200 + 401 > 600）
        over = self._post_json(
            f"/api/v1/front-desk/reservations/{booking_no}/deposits",
            {"amount": "401.00", "method": "CASH"},
            idempotency_key="e2e-dep-002")
        self.assertEqual(over.status_code, 409, over.content)
        self.assertEqual(over.json()["error"]["code"], "BUSINESS_CONFLICT")
        reservation.refresh_from_db()
        self.assertEqual(reservation.deposit_amount, Decimal("200.00"))
        self.assertEqual(Payment.objects.filter(
            reservation__booking_no=booking_no,
            provider="frontdesk_deposit").count(), 1)

    # ------------------------------------------------------------------
    # 3) 退款链路：前台申请挂起 → 店长（refund_approve）审批完成
    # ------------------------------------------------------------------
    def test_refund_request_pends_for_approval_then_owner_completes(self):
        booking_no, _, _, _ = self._booking_with_outstanding()
        self.client.force_login(self.fd)
        payment = self._collect_payment(booking_no, "300.00", "e2e-pay-rf1").json()["data"]
        payment_id = payment["payment_id"]
        self.assertTrue(AuthorizationService.has_permission(
            self.fd, self.property, "billing.refund_request"))
        self.assertTrue(AuthorizationService.has_permission(
            self.owner, self.property, "billing.refund_approve"))

        request_resp = self._post_json(
            f"/api/v1/front-desk/payments/{payment_id}/refunds",
            {"amount": "100.00", "reason": "客人多付一晚房费，协商退款"},
            idempotency_key="e2e-rf-001")
        self.assertEqual(request_resp.status_code, 201, request_resp.content)
        refund_id = request_resp.json()["data"]["refund_id"]

        refund = Refund.objects.get(pk=refund_id)
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(refund.amount, Decimal("100.00"))
        self.assertEqual(refund.requested_by_id, self.fd.pk)
        approval = ApprovalRecord.objects.get(
            entity_type="Refund", entity_id=str(refund_id))
        self.assertEqual(approval.action_code, "billing.refund")
        self.assertEqual(approval.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(approval.requested_by_id, self.fd.pk)
        self.assertTrue(self._audit_exists(
            "billing.refund_request", entity_id=refund_id,
            property_obj=self.property))
        self.assertEqual(Reservation.objects.get(
            pk=refund.payment.reservation_id).deposit_amount, Decimal("0.00"))

        # 前台只有申请权没有审批权 → 403，状态不迁移
        denied = self._post_json(
            f"/api/v1/front-desk/refunds/{refund_id}/approve",
            {"confirm_refund_paid": True})
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertEqual(denied.json()["error"]["code"], "PERMISSION_DENIED")
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.PENDING)

        # 店长仅审批，不得写退款凭证；财务作为独立执行人完成退款。
        self.client.force_login(self.owner)
        approve = self._post_json(
            f"/api/v1/front-desk/refunds/{refund_id}/approve", {})
        self.assertEqual(approve.status_code, 200, approve.content)
        self.assertTrue(approve.json()["changed"])
        refund.refresh_from_db()
        approval.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertEqual(refund.approved_by_id, self.owner.pk)
        self.assertIsNone(refund.executed_by_id)
        self.assertEqual(approval.status, ApprovalRecord.Status.APPROVED)
        self.assertEqual(approval.approved_by_id, self.owner.pk)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        self.client.force_login(self.fin)
        self.assertIn(self._open_cashier_shift("e2e-shift-refund-exec-001").status_code,
                      (200, 201))
        execute_payload = {
            "confirm_refund_paid": True,
            "execution_reference": "E2E-MANUAL-REFUND-001",
            "idempotency_key": "e2e-refund-exec-001",
        }
        execute = self._post_json(
            f"/api/v1/billing/refunds/{refund_id}/execute", execute_payload)
        self.assertEqual(execute.status_code, 201, execute.content)
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.SUCCEEDED)
        self.assertEqual(refund.approved_by_id, self.owner.pk)
        self.assertEqual(refund.executed_by_id, self.fin.pk)
        self.assertIsNotNone(refund.executed_at)
        self.assertIsNotNone(refund.execution_shift_id)

        refund_entry = LedgerEntry.objects.get(
            reference=f"refund:{refund.provider_ref}")
        self.assertEqual(refund_entry.entry_type, LedgerEntry.EntryType.REFUND)
        self.assertEqual(refund_entry.amount, Decimal("100.00"))
        payment_obj = Payment.objects.get(pk=payment_id)
        self.assertEqual(payment_obj.status, Payment.Status.PARTIALLY_REFUNDED)
        folio = self._folio(booking_no)
        self.assertEqual(self._folio_balance(folio), Decimal("-500.00"))
        self.assertTrue(self._audit_exists(
            "approval.approve", entity_id=refund_id, property_obj=self.property))
        self.assertTrue(self._audit_exists(
            "billing.refund_executed", entity_id=refund_id,
            property_obj=self.property))

        # 同一执行幂等键重放：不得重复追加退款凭证。
        repeat = self._post_json(
            f"/api/v1/billing/refunds/{refund_id}/execute", execute_payload)
        self.assertEqual(repeat.status_code, 200, repeat.content)
        self.assertEqual(LedgerEntry.objects.filter(
            entry_type=LedgerEntry.EntryType.REFUND, folio=folio).count(), 1)

    # ------------------------------------------------------------------
    # 4) 退款申请超额 → 409 且无副作用
    # ------------------------------------------------------------------
    def test_refund_request_over_payment_amount_conflicts_without_side_effects(self):
        booking_no, _, _, _ = self._booking_with_outstanding()
        self.client.force_login(self.fd)
        payment = self._collect_payment(booking_no, "300.00", "e2e-pay-rf2").json()["data"]

        response = self._post_json(
            f"/api/v1/front-desk/payments/{payment['payment_id']}/refunds",
            {"amount": "700.00", "reason": "超额退款必须被拒绝"},
            idempotency_key="e2e-rf-002")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertEqual(Refund.objects.count(), 0)
        self.assertEqual(ApprovalRecord.objects.filter(
            action_code="billing.refund").count(), 0)
        self.assertFalse(self._audit_exists("billing.refund_request"))

    # ------------------------------------------------------------------
    # 5) 权限分离（P1-02）：finance_access 不隐含 refund_approve
    # ------------------------------------------------------------------
    def test_refund_approve_denied_for_finance_access_without_approve_permission(self):
        booking_no, _, _, _ = self._booking_with_outstanding()
        self.client.force_login(self.fd)
        payment = self._collect_payment(booking_no, "300.00", "e2e-pay-rf3").json()["data"]
        refund = self._post_json(
            f"/api/v1/front-desk/payments/{payment['payment_id']}/refunds",
            {"amount": "50.00", "reason": "小额零头退款"},
            idempotency_key="e2e-rf-003").json()["data"]

        viewer = self._make_finance_view_only("e2e_b_finview")
        self.assertTrue(AuthorizationService.has_permission(
            viewer, self.property, "hotelapp.finance_access"))
        self.assertFalse(AuthorizationService.has_permission(
            viewer, self.property, "billing.refund_approve"))

        self.client.force_login(viewer)
        response = self._post_json(
            f"/api/v1/front-desk/refunds/{refund['refund_id']}/approve",
            {"confirm_refund_paid": True})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

        # DB 副作用必须为零：退款/审批都停留在 PENDING，无退款凭证、无完成审计
        refund_obj = Refund.objects.get(pk=refund["refund_id"])
        self.assertEqual(refund_obj.status, Refund.Status.PENDING)
        approval = ApprovalRecord.objects.get(
            entity_type="Refund", entity_id=str(refund["refund_id"]))
        self.assertEqual(approval.status, ApprovalRecord.Status.PENDING)
        self.assertFalse(LedgerEntry.objects.filter(
            folio=self._folio(booking_no),
            entry_type=LedgerEntry.EntryType.REFUND).exists())
        self.assertFalse(self._audit_exists("billing.refund_completed"))

    # ------------------------------------------------------------------
    # 6) 冲账：红字对 + 审计 + 幂等（F-08：暂无 HTTP 入口，服务级验证）
    # ------------------------------------------------------------------
    def test_reverse_entry_posts_paired_reversal_and_audit(self):
        booking_no, _, _, _ = self._booking_with_outstanding()
        self.client.force_login(self.fd)
        self._collect_payment(booking_no, "300.00", "e2e-pay-rev")
        folio = self._folio(booking_no)
        self.assertEqual(self._folio_balance(folio), Decimal("-600.00"))
        original = LedgerEntry.objects.get(
            reference=f"payment:FD{self.property.pk}-e2e-pay-rev")
        self.client.logout()

        reversal, created = BillingService.reverse_entry(
            entry_id=original.pk,
            reason="收款金额录入错误，红字冲销重收",
            reversed_by=self.fin)
        self.assertTrue(created)
        self.assertEqual(reversal.entry_type, LedgerEntry.EntryType.REVERSAL)
        self.assertEqual(reversal.amount, -original.amount)
        self.assertEqual(
            reversal.reference, f"reversal:{original.reference}",
            "冲销凭证以固定引用与原凭证配对")
        self.assertEqual(reversal.folio_id, folio.pk)
        original.refresh_from_db()
        self.assertEqual(original.amount, Decimal("-300.00"), "原凭证不可修改")
        folio = self._folio(booking_no)
        self.assertEqual(self._folio_balance(folio), Decimal("-300.00"))
        self.assertTrue(self._audit_exists(
            "billing.reverse_entry", entity_id=original.pk,
            property_obj=self.property))

        # 同一原凭证重复冲销幂等：返回既有红字凭证
        again, created_again = BillingService.reverse_entry(
            entry_id=original.pk,
            reason="收款金额录入错误，红字冲销重收",
            reversed_by=self.fin)
        self.assertFalse(created_again)
        self.assertEqual(again.pk, reversal.pk)

        # 红字凭证本身不可再冲销
        with self.assertRaises(ValueError):
            BillingService.reverse_entry(
                entry_id=reversal.pk, reason="试图冲销红字", reversed_by=self.fin)


class NightAuditBusinessDateE2EFlowTests(E2EFlowTestBase):
    """P0-C（§9.4）：夜审预览/执行/幂等、营业日重开双人审批、break-glass。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("e2e_c_frontdesk", "前台")
        self.dm = self._make_staff("e2e_c_duty", "值班经理")
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        line = self._line(booking_no)
        self.client.force_login(self.fd)
        self.stay_id = self._assign_and_check_in(line, self.room1)
        self.stay = Stay.objects.get(pk=self.stay_id)
        self.booking_no = booking_no

    def _run_night_audit(self, date_str=None):
        payload = {"date": date_str} if date_str else {}
        return self._post_json("/api/v1/night-audit/run", payload)

    # ------------------------------------------------------------------
    # 1) 无 night_audit_execute 权限 → 403，且无任何营业日/夜审记录
    # ------------------------------------------------------------------
    def test_night_audit_endpoints_require_night_audit_execute_permission(self):
        self.client.force_login(self.fd)
        self.assertFalse(AuthorizationService.has_permission(
            self.fd, self.property, "hotelapp.night_audit_execute"))

        preview = self.client.get("/api/v1/night-audit/preview")
        self.assertEqual(preview.status_code, 403, preview.content)
        self.assertEqual(preview.json()["error"]["code"], "PERMISSION_DENIED")

        run = self._run_night_audit()
        self.assertEqual(run.status_code, 403, run.content)
        self.assertEqual(run.json()["error"]["code"], "PERMISSION_DENIED")

        self.assertEqual(BusinessDate.objects.filter(property=self.property).count(), 0)
        self.assertEqual(NightAuditRun.objects.count(), 0)
        self.assertFalse(self._audit_exists("night_audit.run"))

    # ------------------------------------------------------------------
    # 2) 值班经理执行：房费过租 + 营业日关账 + 审计 + 同日重放幂等
    # ------------------------------------------------------------------
    def test_duty_manager_run_posts_room_charge_and_replay_is_idempotent(self):
        self.client.force_login(self.dm)
        self.assertTrue(AuthorizationService.has_permission(
            self.dm, self.property, "hotelapp.night_audit_execute"))

        preview = self.client.get("/api/v1/night-audit/preview")
        self.assertEqual(preview.status_code, 200, preview.content)
        preview_data = preview.json()["data"]
        self.assertEqual(preview_data["stay_count"], 1)
        self.assertEqual(preview_data["charge_total"], "300.00")
        self.assertEqual(preview_data["business_date_status"], BusinessDate.Status.OPEN)

        run = self._run_night_audit()
        self.assertEqual(run.status_code, 201, run.content)
        self.assertTrue(run.json()["data"]["created"])
        self.assertEqual(run.json()["data"]["status"], NightAuditRun.RunStatus.SUCCESS)

        bd = BusinessDate.objects.get(property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)
        self.assertEqual(bd.closed_by_id, self.dm.pk)
        self.assertIsNotNone(bd.closed_at)
        run_record = NightAuditRun.objects.get(pk=run.json()["data"]["run_id"])
        self.assertEqual(run_record.status, NightAuditRun.RunStatus.SUCCESS)
        self.assertEqual(run_record.executed_by_id, self.dm.pk)

        charge_reference = (
            f"night-audit:{self.property.pk}:{self.today.isoformat()}:stay:{self.stay.pk}")
        charge = LedgerEntry.objects.get(reference=charge_reference)
        self.assertEqual(charge.entry_type, LedgerEntry.EntryType.ROOM_CHARGE)
        self.assertEqual(charge.amount, Decimal("300.00"))
        self.assertEqual(charge.stay_id, self.stay.pk)
        folio = self._folio(self.booking_no)
        self.assertEqual(self._folio_balance(folio), Decimal("-300.00"))
        self.assertTrue(self._audit_exists(
            "night_audit.run", entity_id=bd.pk, property_obj=self.property))

        # 同营业日重复执行：现有实现为幂等成功（200 + created=false），不回 400/409
        replay = self._run_night_audit()
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertFalse(replay.json()["data"]["created"])
        self.assertEqual(
            NightAuditRun.objects.filter(
                business_date=bd, status=NightAuditRun.RunStatus.SUCCESS).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE).count(), 1)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

    # ------------------------------------------------------------------
    # 3) 重开营业日：缺理由拒绝 → 申请 → 申请人自批拒绝 → 店长批准 → 重开可再审
    # ------------------------------------------------------------------
    def test_reopen_requires_reason_then_owner_approval_and_self_approval_denied(self):
        self.client.force_login(self.dm)
        run = self._run_night_audit()
        self.assertEqual(run.status_code, 201, run.content)
        bd = BusinessDate.objects.get(property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

        owner = self._make_staff("e2e_c_owner", "店长")

        # 缺理由：现有实现返回 409 BUSINESS_CONFLICT（§9.4 草稿预期 400，差异已登记）
        no_reason = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/reopen-request", {})
        self.assertEqual(no_reason.status_code, 409, no_reason.content)
        self.assertEqual(no_reason.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertEqual(ApprovalRecord.objects.filter(
            action_code="night_audit.reopen").count(), 0)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

        request_resp = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/reopen-request",
            {"reason": "夜审后发现漏记房费需要补记凭证"})
        self.assertEqual(request_resp.status_code, 201, request_resp.content)
        approval_id = request_resp.json()["data"]["approval_id"]
        approval = ApprovalRecord.objects.get(pk=approval_id)
        self.assertEqual(approval.action_code, "night_audit.reopen")
        self.assertEqual(approval.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(approval.requested_by_id, self.dm.pk)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

        # 申请人不能自批（Q-05 SoD）→ 409，营业日保持关账
        self_approve = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/approvals/{approval_id}/approve")
        self.assertEqual(self_approve.status_code, 409, self_approve.content)
        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalRecord.Status.PENDING)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

        # 店长批准 → REOPENED + 审计
        self.client.force_login(owner)
        approve = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/approvals/{approval_id}/approve")
        self.assertEqual(approve.status_code, 200, approve.content)
        self.assertEqual(approve.json()["data"]["status"], BusinessDate.Status.REOPENED)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.REOPENED)
        self.assertEqual(bd.reopened_by_id, owner.pk)
        self.assertEqual(bd.reopen_reason, "夜审后发现漏记房费需要补记凭证")
        approval.refresh_from_db()
        self.assertEqual(approval.status, ApprovalRecord.Status.APPROVED)
        self.assertTrue(self._audit_exists(
            "night_audit.reopen", entity_id=bd.pk, property_obj=self.property))

        # 重开后的营业日可以再次夜审；过租凭证按引用幂等，不重复入账
        self.client.force_login(self.dm)
        rerun = self._run_night_audit()
        self.assertEqual(rerun.status_code, 201, rerun.content)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=self._folio(self.booking_no),
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE).count(), 1)

    # ------------------------------------------------------------------
    # 4) break-glass：申请（二次密码）→ 申请人执行 → 独立复核
    # ------------------------------------------------------------------
    def test_break_glass_request_execute_review_full_lifecycle(self):
        # 让值班经理成为物业内唯一持 business_date_reopen 的有效账号
        User.objects.exclude(pk__in=[self.dm.pk, self.fd.pk]).update(is_active=False)
        self.client.force_login(self.dm)
        run = self._run_night_audit()
        self.assertEqual(run.status_code, 201, run.content)
        bd = BusinessDate.objects.get(property=self.property, business_date=self.today)

        wrong_password = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass",
            {"password": "not-the-password",
             "reason": "夜审后发现重大账务差错需要紧急重开修正"})
        self.assertEqual(wrong_password.status_code, 409, wrong_password.content)
        self.assertEqual(BreakGlassRecord.objects.count(), 0)

        granted = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass",
            {"password": "e2e_c_duty-pass123",
             "reason": "夜审后发现重大账务差错需要紧急重开修正"})
        self.assertEqual(granted.status_code, 201, granted.content)
        record_id = granted.json()["data"]["break_glass_id"]
        record = BreakGlassRecord.objects.get(pk=record_id)
        self.assertEqual(record.status, BreakGlassRecord.Status.GRANTED)
        self.assertEqual(record.requested_by_id, self.dm.pk)
        self.assertEqual(
            record.expires_at - record.password_verified_at, timedelta(minutes=10))
        self.assertTrue(self._audit_exists(
            "night_audit.break_glass_requested", entity_id=record_id,
            property_obj=self.property))

        # 非申请人不能执行
        reviewer = self._make_staff("e2e_c_reviewer", "店长")
        review_permission = Permission.objects.get(
            content_type__app_label="core", codename="breakglass_review")
        Group.objects.get(name="店长").permissions.add(review_permission)
        self.client.force_login(reviewer)
        wrong_actor = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass/{record_id}/execute")
        self.assertEqual(wrong_actor.status_code, 409, wrong_actor.content)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

        # 申请人限时单次执行 → REOPENED
        self.client.force_login(self.dm)
        execute = self._post_json(
            f"/api/v1/night-audit/business-dates/{bd.pk}/break-glass/{record_id}/execute")
        self.assertEqual(execute.status_code, 200, execute.content)
        self.assertEqual(execute.json()["data"]["status"], BusinessDate.Status.REOPENED)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.REOPENED)
        self.assertEqual(bd.reopened_by_id, self.dm.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, BreakGlassRecord.Status.USED)
        self.assertTrue(self._audit_exists(
            "night_audit.break_glass_executed", entity_id=bd.pk,
            property_obj=self.property))

        # 操作人本人不能复核 → 409
        self_review = self._post_json(
            f"/api/v1/night-audit/break-glass/{record_id}/review",
            {"decision": "CONFIRMED", "reason": "本人复核不符合独立复核要求"})
        self.assertEqual(self_review.status_code, 409, self_review.content)
        record.refresh_from_db()
        self.assertEqual(record.status, BreakGlassRecord.Status.USED)

        # 另一获授权经理复核 → REVIEWED + 审计
        self.client.force_login(reviewer)
        review = self._post_json(
            f"/api/v1/night-audit/break-glass/{record_id}/review",
            {"decision": "CONFIRMED",
             "reason": "已核对紧急重开原因与事后账务修正凭证"})
        self.assertEqual(review.status_code, 200, review.content)
        self.assertEqual(review.json()["data"]["status"], BreakGlassRecord.Status.REVIEWED)
        self.assertEqual(
            review.json()["data"]["review_decision"],
            BreakGlassRecord.ReviewDecision.CONFIRMED)
        record.refresh_from_db()
        self.assertEqual(record.status, BreakGlassRecord.Status.REVIEWED)
        self.assertEqual(record.reviewed_by_id, reviewer.pk)
        self.assertTrue(self._audit_exists(
            "night_audit.break_glass_reviewed", entity_id=record_id,
            property_obj=self.property))

    # ------------------------------------------------------------------
    # 5) 夜审后新入住：营业日关账后入住被阻挡（§4.2 阻挡规则，P0-C 缺口已收口）
    # ------------------------------------------------------------------
    def test_new_check_in_after_night_audit_is_blocked(self):
        self.client.force_login(self.dm)
        run = self._run_night_audit()
        self.assertEqual(run.status_code, 201, run.content)
        bd = BusinessDate.objects.get(property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

        # 夜审关账后办理全新入住：入住服务入口拒绝（409 BUSINESS_CONFLICT）；
        # 排房不写营业日账务，仍允许为次日到店提前排房。
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        line = self._line(booking_no)
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
        self.assertEqual(Reservation.objects.get(
            booking_no=booking_no).status, Reservation.Status.CONFIRMED)
        bd.refresh_from_db()
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)


class ExceptionFulfillmentE2EFlowTests(E2EFlowTestBase):
    """P0-D（§9.5）：取消冲突、No-Show、续住、换房、退房结算与反结账缺口。"""

    def setUp(self):
        super().setUp()
        self.fd = self._make_staff("e2e_d_frontdesk", "前台")
        self.dm = self._make_staff("e2e_d_duty", "值班经理")

    def _inventory_row(self, day):
        return RoomTypeInventory.objects.get(
            property=self.property, room_type=self.room_type, stay_date=day)

    # ------------------------------------------------------------------
    # 1) 已入住订单自助取消 → 409 状态冲突，库存与订单状态零变更
    # ------------------------------------------------------------------
    def test_cancel_checked_in_reservation_conflicts(self):
        booking_no, token, _, _ = self._booking_checked_in()
        self.client.logout()
        self.assertEqual(Reservation.objects.get(
            booking_no=booking_no).status, Reservation.Status.CHECKED_IN)

        response = self._post_json(
            f"/api/v1/reservations/{booking_no}/cancel", {"token": token})
        self.assertEqual(response.status_code, 409, response.content)

        reservation = Reservation.objects.get(booking_no=booking_no)
        self.assertEqual(reservation.status, Reservation.Status.CHECKED_IN)
        self.assertTrue(self._audit_exists(
            "guest.booking_cancelled", entity_id=booking_no[:32]))
        # 库存不变（模拟支付已将 hold 转为 sold；取消冲突不得释放或回退）
        self.assertEqual(self._inventory_row(self.today).held, 0)
        self.assertEqual(self._inventory_row(self.today).sold, 1)
        self.assertTrue(Stay.objects.filter(
            reservation_room=self._line(booking_no),
            status=Stay.Status.IN_HOUSE).exists())

    # ------------------------------------------------------------------
    # 2) No-Show：已到店未入住明细 → 成功 + 库存释放 + 整单 NO_SHOW
    # ------------------------------------------------------------------
    def test_no_show_on_arrived_unchecked_in_line_releases_inventory(self):
        booking_no, token = self._create_reservation()
        self._confirm_by_mock_payment(booking_no, token)
        self.assertEqual(self._inventory_row(self.today).sold, 1)
        self.assertEqual(self._inventory_row(self.today + timedelta(days=1)).sold, 1)
        line = self._line(booking_no)
        self.assertIsNone(line.assigned_room)

        self.client.force_login(self.dm)
        response = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/no-show",
            {"reason": "客人电话确认不再到店"})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["created"])
        self.assertEqual(response.json()["data"]["released_nights"], 2)

        record = NoShowRecord.objects.get(reservation_room=line)
        self.assertEqual(record.reason, "客人电话确认不再到店")
        self.assertEqual(record.processed_by_id, self.dm.pk)
        self.assertEqual(record.released_nights, 2)
        self.assertEqual(self._inventory_row(self.today).sold, 0, "库存必须释放")
        self.assertEqual(self._inventory_row(self.today + timedelta(days=1)).sold, 0)
        reservation = Reservation.objects.get(booking_no=booking_no)
        self.assertEqual(reservation.status, Reservation.Status.NO_SHOW)
        self.assertTrue(self._audit_exists(
            "frontdesk.no_show_processed", entity_id=line.pk,
            property_obj=self.property))

        # 重复处理幂等：200 + created=false，库存不再多释放
        repeat = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/no-show",
            {"reason": "客人电话确认不再到店"})
        self.assertEqual(repeat.status_code, 200)
        self.assertFalse(repeat.json()["created"])
        self.assertEqual(self._inventory_row(self.today).sold, 0)

    # ------------------------------------------------------------------
    # 3) No-Show 拒绝：PENDING 未确认订单与已入住明细均 409
    # ------------------------------------------------------------------
    def test_no_show_rejected_for_pending_and_checked_in_lines(self):
        # PENDING（未支付确认）订单不能处理 No-Show
        pending_no, _ = self._create_reservation()
        pending_line = self._line(pending_no)
        self.client.force_login(self.dm)
        pending_resp = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{pending_line.pk}/no-show",
            {"reason": "订单尚未确认"})
        self.assertEqual(pending_resp.status_code, 409, pending_resp.content)

        # 已入住明细不能补做 No-Show
        booking_no, _, line, _ = self._booking_checked_in()
        self.client.force_login(self.dm)
        checked_in_resp = self._post_json(
            f"/api/v1/front-desk/reservation-rooms/{line.pk}/no-show",
            {"reason": "客人已在住"})
        self.assertEqual(checked_in_resp.status_code, 409, checked_in_resp.content)

        self.assertEqual(NoShowRecord.objects.count(), 0)
        self.assertTrue(Stay.objects.filter(
            reservation_room=line, status=Stay.Status.IN_HOUSE).exists())
        self.assertEqual(Reservation.objects.get(
            booking_no=booking_no).status, Reservation.Status.CHECKED_IN)
        self.assertFalse(self._audit_exists("frontdesk.no_show_processed"))

    # ------------------------------------------------------------------
    # 4) 续住：追加晚数/金额/库存；早于原离店日期 → 409（草稿预期 400，已登记）
    # ------------------------------------------------------------------
    def test_extend_stay_appends_amount_and_inventory_and_rejects_earlier_date(self):
        booking_no, _, line, stay_id = self._booking_checked_in()
        self.client.force_login(self.fd)
        self.assertEqual(line.amount, Decimal("600.00"))

        response = self._post_json(
            f"/api/v1/front-desk/stays/{stay_id}/extend",
            {"new_check_out": (self.today + timedelta(days=3)).isoformat(),
             "reason": "客人要求多住一晚"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["extension_amount"], "300.00")

        line.refresh_from_db()
        reservation = Reservation.objects.get(booking_no=booking_no)
        self.assertEqual(line.check_out, self.today + timedelta(days=3))
        self.assertEqual(line.amount, Decimal("900.00"))
        self.assertEqual(reservation.check_out, self.today + timedelta(days=3))
        self.assertEqual(reservation.total_amount, Decimal("900.00"))
        self.assertEqual(self._inventory_row(self.today + timedelta(days=2)).sold, 1)
        self.assertTrue(self._audit_exists(
            "frontdesk.stay_extended", entity_id=stay_id,
            property_obj=self.property))

        earlier = self._post_json(
            f"/api/v1/front-desk/stays/{stay_id}/extend",
            {"new_check_out": (self.today + timedelta(days=1)).isoformat(),
             "reason": "试图缩短住宿"})
        self.assertEqual(earlier.status_code, 409, earlier.content)
        line.refresh_from_db()
        self.assertEqual(line.check_out, self.today + timedelta(days=3))
        self.assertEqual(line.amount, Decimal("900.00"))
        self.assertEqual(self._inventory_row(self.today + timedelta(days=2)).sold, 1)

    # ------------------------------------------------------------------
    # 5) 换房：房段历史 + 双房间房态变化；同房请求无副作用（草稿预期 400，已登记）
    # ------------------------------------------------------------------
    def test_change_room_records_segment_history_and_updates_both_rooms(self):
        booking_no, _, line, stay_id = self._booking_checked_in()
        self.client.force_login(self.fd)

        response = self._post_json(
            f"/api/v1/front-desk/stays/{stay_id}/change-room",
            {"room_id": self.room2.pk, "reason": "空调噪音投诉换房"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["changed"])

        stay = Stay.objects.get(pk=stay_id)
        self.assertEqual(stay.room_id, self.room2.pk)
        segments = list(StayRoomSegment.objects.filter(stay=stay).order_by("id"))
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].room_id, self.room1.pk)
        self.assertIsNotNone(segments[0].ended_at)
        self.assertEqual(segments[0].move_reason, "空调噪音投诉换房")
        self.assertEqual(segments[1].room_id, self.room2.pk)
        self.assertIsNone(segments[1].ended_at)
        self.room1.refresh_from_db()
        self.room2.refresh_from_db()
        self.assertEqual(self.room1.occupancy_status, Room.OccupancyStatus.VACANT)
        self.assertEqual(self.room1.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        self.assertEqual(self.room2.occupancy_status, Room.OccupancyStatus.OCCUPIED)
        line.refresh_from_db()
        self.assertEqual(line.assigned_room_id, self.room2.pk)
        self.assertTrue(self._audit_exists(
            "frontdesk.stay_room_changed", entity_id=stay_id,
            property_obj=self.property))

        # 同房换房：现有实现返回 200 + changed=false 且无副作用
        same = self._post_json(
            f"/api/v1/front-desk/stays/{stay_id}/change-room",
            {"room_id": self.room2.pk, "reason": "重复提交同房请求"})
        self.assertEqual(same.status_code, 200, same.content)
        self.assertFalse(same.json()["changed"])
        self.assertEqual(
            StayRoomSegment.objects.filter(stay=stay).count(), 2)
        self.room2.refresh_from_db()
        self.assertEqual(self.room2.occupancy_status, Room.OccupancyStatus.OCCUPIED)

    # ------------------------------------------------------------------
    # 6) 快捷退房旧入口 410；PMS2 统一退房结清 Folio + Stay 状态
    # ------------------------------------------------------------------
    def test_express_checkout_retired_and_pms2_checkout_settles_folio(self):
        booking_no, _, line, stay_id = self._booking_checked_in()
        self.client.force_login(self.fd)
        self._collect_payment(booking_no, "600.00", "e2e-pay-co")

        legacy = self._post_json("/express_checkout/", {"id": stay_id})
        self.assertEqual(legacy.status_code, 410, legacy.content)
        stay = Stay.objects.get(pk=stay_id)
        self.assertEqual(stay.status, Stay.Status.IN_HOUSE, "退役入口不得改状态")

        response = self._post_json(f"/api/v1/front-desk/stays/{stay_id}/check-out")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["changed"])

        stay.refresh_from_db()
        folio = self._folio(booking_no)
        reservation = Reservation.objects.get(booking_no=booking_no)
        self.room1.refresh_from_db()
        self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)
        self.assertIsNotNone(stay.actual_check_out)
        self.assertEqual(folio.status, Folio.Status.CLOSED)
        self.assertEqual(reservation.status, Reservation.Status.CHECKED_OUT)
        self.assertEqual(self.room1.occupancy_status, Room.OccupancyStatus.VACANT)
        self.assertEqual(self.room1.housekeeping_status, Room.HousekeepingStatus.DIRTY)
        charge = LedgerEntry.objects.get(reference=f"stay-room-charge:{stay_id}")
        self.assertEqual(charge.entry_type, LedgerEntry.EntryType.ROOM_CHARGE)
        self.assertEqual(charge.amount, Decimal("600.00"))
        self.assertEqual(self._folio_balance(folio), Decimal("0.00"))
        self.assertTrue(self._audit_exists(
            "frontdesk.checked_out", entity_id=stay_id, property_obj=self.property))

        # 重复退房幂等
        repeat = self._post_json(f"/api/v1/front-desk/stays/{stay_id}/check-out")
        self.assertEqual(repeat.status_code, 200)
        self.assertFalse(repeat.json()["changed"])

    # ------------------------------------------------------------------
    # 7) 反结账：PMS2 无对应 API（F-08 缺口确认），旧路由 410 且状态不变
    # ------------------------------------------------------------------
    def test_uncheckout_gap_no_pms2_api_and_legacy_route_retired(self):
        booking_no, _, line, stay_id = self._booking_checked_in()
        self.client.force_login(self.fd)
        self._collect_payment(booking_no, "600.00", "e2e-pay-uc")
        self.assertEqual(
            self._post_json(f"/api/v1/front-desk/stays/{stay_id}/check-out").status_code,
            200)

        self.client.force_login(self.dm)
        legacy = self._post_json("/uncheckout/", {"id": stay_id})
        self.assertEqual(legacy.status_code, 410, legacy.content)

        # PMS2 API 面没有反结账路由（缺口 F-08：hotelapp.reverse_checkout 无检查点）
        missing = self._post_json(f"/api/v1/front-desk/stays/{stay_id}/uncheckout")
        self.assertEqual(missing.status_code, 404, missing.content)

        stay = Stay.objects.get(pk=stay_id)
        self.assertEqual(stay.status, Stay.Status.CHECKED_OUT)
        self.assertEqual(self._folio(booking_no).status, Folio.Status.CLOSED)
        self.assertEqual(Reservation.objects.get(
            booking_no=booking_no).status, Reservation.Status.CHECKED_OUT)
