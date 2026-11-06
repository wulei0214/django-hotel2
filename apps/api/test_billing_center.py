# -*- coding: utf-8 -*-
"""财务与账单中心 API 测试包（基线 2.0 §4.1）。

覆盖口径：
  * 读取端点权限四态：匿名 401 / 无任职 403 / 无 finance_access 403 / 有权限 200；
  * 分页契约边界（api_contract.paginate：非法 page/page_size → 400）；
  * 物业隔离默认拒绝：跨物业对象列表不可见、详情/写操作一律 404；
  * 写操作成功路径的 DB 副作用：Payment / LedgerEntry / Refund / ApprovalRecord
    落库 + AuditEvent 审计（billing.collect / billing.refund_request /
    billing.reverse_entry）；
  * 幂等：收款 reference、退款幂等键、冲销 reference 幂等（同凭证只冲一次）；
  * 权限不足写 403（refund_request / reverse_entry 专项权 + finance_access 门禁）；
  * 财务中心页面 /manage/finance/ 注册与门禁。

所有写操作全部走 BillingService（本测试不直接构造账务写入路径之外的模型状态）。
"""
import json
import uuid
from decimal import Decimal

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import (
    ApprovalRecord,
    CashierShift,
    Folio,
    LedgerEntry,
    Payment,
    Refund,
)
from apps.billing.services_v2 import BillingService
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.reservations.models import Reservation, ReservationRoom


def _post_json(client, url, payload=None, idempotency_key=""):
    extra = {}
    if idempotency_key:
        extra["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
    return client.post(url, data=json.dumps(payload or {}),
                       content_type="application/json", **extra)


class BillingCenterTestBase(TestCase):
    """DEFAULT 物业 + 三类账号（财务/前台无财务权/无任职）+ 账务测试数据工厂。"""

    maxDiff = None

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self.finance = self._make_staff(
            "fin_center_user", "财务",
            extra_codenames=["reverse_entry"])
        self.no_finance = self._make_staff(
            "fin_center_frontdesk", "前台", finance_access=False)
        self.staff_writer = self._make_staff(
            "fin_center_writer", "财务",
            extra_codenames=["reverse_entry"])
        self.no_membership_user = User.objects.create_user(
            username="fin_center_outsider", password="outsider-pass-123")
        self._seq = 0

    # ------------------------------------------------------------------
    # 账号工厂（对齐 test_e2e_flows.E2EFlowTestBase._make_staff 身份链）
    # ------------------------------------------------------------------
    def _make_staff(self, username, group_name, extra_codenames=(),
                    finance_access=True):
        group, _ = Group.objects.get_or_create(name=f"BC-{group_name}-{username}")
        wanted = set(extra_codenames)
        if finance_access:
            wanted.add("finance_access")
        if wanted:
            group.permissions.add(*Permission.objects.filter(
                codename__in=wanted,
                content_type__app_label__in=("hotelapp", "billing")))
        if group_name == "财务":
            group.permissions.add(*Permission.objects.filter(
                content_type__app_label="billing", codename="refund_request"))
        user = User.objects.create_user(username=username,
                                        password=f"{username}-pass123")
        profile = StaffProfile.objects.create(
            user=user, full_name=username,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE)
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile, property=self.property,
            employee_no=f"BC-{username}",
            status=PropertyStaffMembership.Status.ACTIVE)
        StaffRoleAssignment.objects.create(
            membership=membership, group=group,
            starts_at=timezone.now() - timezone.timedelta(minutes=1))
        return user

    def _grant(self, user, codename, app_label="billing"):
        """给已有任职账号补授专项权限码（直接挂到其唯一角色组）。"""
        assignment = StaffRoleAssignment.objects.filter(
            membership__staff_profile__user=user).first()
        perm = Permission.objects.get(
            content_type__app_label=app_label, codename=codename)
        assignment.group.permissions.add(perm)


    # ------------------------------------------------------------------
    # 账务数据工厂：带应收余额的入住账单（余额 = 300.00 CNY）
    # ------------------------------------------------------------------
    def _room_type(self):
        from apps.inventory.models import RoomType
        if not hasattr(self, "_room_type_cache"):
            self._room_type_cache = RoomType.objects.create(
                property=self.property, code="BC-KING", name="账单中心测试大床房",
                capacity=2)
        return self._room_type_cache

    def _rate_plan(self):
        from apps.inventory.models import RatePlan
        if not hasattr(self, "_rate_plan_cache"):
            self._rate_plan_cache = RatePlan.objects.create(
                property=self.property, code="BC-BAR", name="账单中心测试门市价",
                valid_from=self.today - timezone.timedelta(days=1))
        return self._rate_plan_cache

    def _booking_with_outstanding(self, amount=Decimal("300.00")):
        self._seq += 1
        seq = self._seq
        room_type = self._room_type()
        rate_plan = self._rate_plan()
        guest = Guest.objects.create(
            property=self.property, full_name=f"账单中心住客{seq:03d}",
            phone=f"139{seq:08d}")
        reservation = Reservation.objects.create(
            property=self.property, guest=guest,
            booking_no=f"BC{seq:08d}",
            status=Reservation.Status.CHECKED_IN,
            check_in=self.today, check_out=self.today + timezone.timedelta(days=2),
            total_amount=amount, currency="CNY")
        ReservationRoom.objects.create(
            property=self.property, reservation=reservation,
            room_type=room_type, rate_plan=rate_plan,
            check_in=self.today, check_out=self.today + timezone.timedelta(days=2),
            nightly_rates={str(self.today): float(amount)},
            amount=amount)
        folio = Folio.objects.create(
            property=self.property, reservation=reservation,
            folio_type=Folio.FolioType.MASTER, guest=guest,
            status=Folio.Status.OPEN, currency="CNY")
        # 测试中的现金收款必须属于已显式开立的收银班次。
        BillingService.open_shift(
            property_obj=self.property, cashier=self.finance,
            opening_cash=Decimal("0.00"))
        BillingService.post_entry(
            property_obj=self.property, folio=folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE, amount=amount,
            reference=f"bc-room-{seq}", description="房费测试凭证",
            posted_by=self.finance)
        return reservation, folio

    # ------------------------------------------------------------------
    # 断言助手
    # ------------------------------------------------------------------
    def _audit_exists(self, action, entity_id=None, property_obj=None):
        condition = {"action": action}
        if entity_id is not None:
            condition["entity_id"] = str(entity_id)
        if property_obj is not None:
            condition["property_id"] = property_obj.pk
        return AuditEvent.objects.filter(**condition).exists()

    def _login(self, user):
        self.client.force_login(user)

    def _logout(self):
        self.client.logout()

    READ_ENDPOINTS = (
        "/api/v1/billing/folios",
        "/api/v1/billing/ledger",
        "/api/v1/billing/payments",
        "/api/v1/billing/refunds",
        "/api/v1/billing/shifts",
        "/api/v1/billing/summary",
    )


class BillingCenterReadPermissionTests(BillingCenterTestBase):
    """读取端点权限四态 + 分页边界 + 筛选 + 物业隔离。"""

    def test_read_endpoints_permission_four_states(self):
        """全部读端点：匿名 401 / 无任职 403 / 无 finance_access 403 / 财务 200。"""
        for url in self.READ_ENDPOINTS:
            # 1) 匿名
            self._logout()
            response = self.client.get(url)
            self.assertEqual(response.status_code, 401, url)
            self.assertEqual(response.json()["error"]["code"],
                             "AUTHENTICATION_REQUIRED", url)
            # 2) 已登录但无 DEFAULT 有效任职
            self._login(self.no_membership_user)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED", url)
            # 3) 有任职但无 finance_access（前台岗）
            self._login(self.no_finance)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, url)
            self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED", url)
            # 4) 财务岗
            self._login(self.finance)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            body = response.json()
            self.assertIn("data", body)
            self.assertIn("meta", body)

    def test_folio_list_filters_status_booking_and_query(self):
        r1, f1 = self._booking_with_outstanding()
        r2, f2 = self._booking_with_outstanding()
        f2.status = Folio.Status.CLOSED
        f2.save(update_fields=("status",))
        self._login(self.finance)

        data = self.client.get("/api/v1/billing/folios").json()["data"]
        self.assertEqual({row["id"] for row in data}, {f1.pk, f2.pk})

        data = self.client.get(
            "/api/v1/billing/folios?status=OPEN").json()["data"]
        self.assertEqual([row["id"] for row in data], [f1.pk])

        data = self.client.get(
            f"/api/v1/billing/folios?booking_no={r1.booking_no}").json()["data"]
        self.assertEqual([row["id"] for row in data], [f1.pk])
        self.assertEqual(data[0]["booking_no"], r1.booking_no)

        data = self.client.get(
            "/api/v1/billing/folios?q=住客001").json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["guest_name"], "账单中心住客001")

        bad = self.client.get("/api/v1/billing/folios?status=BOGUS")
        self.assertEqual(bad.status_code, 400)

    def test_folio_list_balance_summary_and_money_strings(self):
        """余额口径：LedgerEntry 带符号汇总；meta 汇总未结余额；金额为 str。"""
        r1, f1 = self._booking_with_outstanding(Decimal("300.00"))
        r2, f2 = self._booking_with_outstanding(Decimal("120.50"))
        self._login(self.finance)
        body = self.client.get("/api/v1/billing/folios").json()
        rows = {row["id"]: row for row in body["data"]}
        self.assertEqual(rows[f1.pk]["balance"], "300.00")
        self.assertEqual(rows[f2.pk]["balance"], "120.50")
        self.assertIsInstance(rows[f1.pk]["balance"], str)
        summary = body["meta"]["pagination"]["summary"]
        self.assertEqual(summary["open_folios"], 2)
        self.assertEqual(summary["open_balance"], "420.50")
        self.assertIn("LedgerEntry", summary["basis"])

    def test_folio_list_pagination_contract(self):
        for _ in range(3):
            self._booking_with_outstanding()
        self._login(self.finance)
        # 合法分页
        body = self.client.get(
            "/api/v1/billing/folios?page=2&page_size=2").json()
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["meta"]["pagination"]["page"], 2)
        self.assertTrue(body["meta"]["pagination"]["has_previous"])
        self.assertFalse(body["meta"]["pagination"]["has_next"])
        # 非法分页 → 400 VALIDATION_ERROR
        for query in ("page=0", "page=-1", "page=abc", "page=",
                      "page_size=0", "page_size=-2", "page_size=101",
                      "page_size=abc"):
            response = self.client.get(f"/api/v1/billing/folios?{query}")
            self.assertEqual(response.status_code, 400, query)
            self.assertEqual(response.json()["error"]["code"],
                             "VALIDATION_ERROR", query)

    def test_ledger_list_filters_and_append_only_display(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        BillingService.post_entry(
            property_obj=self.property, folio=folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT, amount=Decimal("-20.00"),
            reference="bc-adj-1", description="调整测试")
        self._login(self.finance)
        body = self.client.get("/api/v1/billing/ledger").json()
        self.assertEqual(body["meta"]["pagination"]["total"], 2)
        row = body["data"][0]
        self.assertEqual(row["entry_type"], "ADJUSTMENT")
        self.assertEqual(row["amount"], "-20.00")
        self.assertIsInstance(row["amount"], str)
        self.assertIn("booking_no", row)
        # 类型筛选
        data = self.client.get(
            "/api/v1/billing/ledger?entry_type=ROOM_CHARGE").json()["data"]
        self.assertEqual({row["entry_type"] for row in data}, {"ROOM_CHARGE"})
        # 日期区间筛选
        data = self.client.get(
            f"/api/v1/billing/ledger?from={self.today}&to={self.today}").json()["data"]
        self.assertEqual(len(data), 2)
        bad = self.client.get("/api/v1/billing/ledger?entry_type=BOGUS")
        self.assertEqual(bad.status_code, 400)
        bad = self.client.get("/api/v1/billing/ledger?from=2026-13-99")
        self.assertEqual(bad.status_code, 400)

    def test_payments_and_refunds_list_filters(self):
        reservation, folio = self._booking_with_outstanding()
        self._login(self.finance)
        collect = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "100.00",
             "method": "CASH", "reference": f"bc-list-{self._seq}"})
        self.assertEqual(collect.status_code, 201, collect.content)
        payment_id = collect.json()["data"]["payment_id"]
        refund = _post_json(
            self.client, "/api/v1/billing/refunds",
            {"payment_id": payment_id, "amount": "40.00", "reason": "列表测试退款"},
            idempotency_key=f"bc-ref-list-{self._seq}")
        self.assertEqual(refund.status_code, 201, refund.content)

        data = self.client.get(
            "/api/v1/billing/payments?status=SUCCEEDED").json()["data"]
        self.assertTrue(any(row["id"] == payment_id for row in data))
        self.assertEqual(data[0]["amount"], "100.00")
        self.assertIsInstance(data[0]["amount"], str)
        self.assertEqual(
            self.client.get(
                "/api/v1/billing/payments?status=FAILED").json()["data"], [])

        data = self.client.get(
            "/api/v1/billing/refunds?status=PENDING").json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["amount"], "40.00")
        self.assertEqual(data[0]["requested_by"], "fin_center_user")
        self.assertEqual(data[0]["approval_status"], "PENDING")

    def test_shifts_list(self):
        shift, created = BillingService.open_shift(
            property_obj=self.property, cashier=self.finance,
            opening_cash=Decimal("200.00"))
        self.assertTrue(created)
        self._login(self.finance)
        body = self.client.get("/api/v1/billing/shifts").json()
        self.assertEqual(body["meta"]["pagination"]["total"], 1)
        row = body["data"][0]
        self.assertEqual(row["cashier"], "fin_center_user")
        self.assertEqual(row["status"], "OPEN")
        self.assertEqual(row["opening_cash"], "200.00")
        data = self.client.get(
            "/api/v1/billing/shifts?status=CLOSED").json()["data"]
        self.assertEqual(data, [])
        bad = self.client.get("/api/v1/billing/shifts?status=BOGUS")
        self.assertEqual(bad.status_code, 400)

    def test_summary_endpoint_period_and_validation(self):
        self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        body = self.client.get("/api/v1/billing/summary").json()["data"]
        self.assertEqual(body["total_revenue"], "300.00")
        self.assertEqual(body["entry_count"], 1)
        self.assertIn("period", body)
        body = self.client.get(
            f"/api/v1/billing/summary?from={self.today}&to={self.today}"
        ).json()["data"]
        self.assertEqual(body["total_revenue"], "300.00")
        bad = self.client.get(
            f"/api/v1/billing/summary?from={self.today}&to=2020-01-01")
        self.assertEqual(bad.status_code, 400)
        bad = self.client.get("/api/v1/billing/summary?from=not-a-date")
        self.assertEqual(bad.status_code, 400)

    def test_property_isolation_default_deny(self):
        """物业隔离：他物业对象列表不可见、详情 404、写操作 404。"""
        other = Property.objects.create(code="BC-OTHER", name="隔离测试酒店")
        other_guest = Guest.objects.create(
            property=other, full_name="他店住客", phone="13700000001")
        other_reservation = Reservation.objects.create(
            property=other, guest=other_guest, booking_no="BC-OTHER-001",
            status=Reservation.Status.CHECKED_IN,
            check_in=self.today, check_out=self.today + timezone.timedelta(days=1),
            total_amount=Decimal("100.00"), currency="CNY")
        other_folio = Folio.objects.create(
            property=other, reservation=other_reservation,
            folio_type=Folio.FolioType.MASTER, guest=other_guest,
            status=Folio.Status.OPEN, currency="CNY")
        other_entry = LedgerEntry.objects.create(
            property=other, folio=other_folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=Decimal("100.00"), currency="CNY",
            business_date=self.today, reference="bc-other-room-1")
        self._login(self.finance)
        # 列表只见本物业
        data = self.client.get("/api/v1/billing/folios").json()["data"]
        self.assertNotIn(other_folio.pk, {row["id"] for row in data})
        data = self.client.get("/api/v1/billing/ledger").json()["data"]
        self.assertNotIn(other_entry.pk, {row["id"] for row in data})
        # 详情/写操作：非本物业一律 404，不泄露存在性
        self.assertEqual(
            self.client.get(f"/api/v1/billing/folios/{other_folio.pk}").status_code, 404)
        response = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": other_reservation.pk, "amount": "10.00",
             "method": "CASH"})
        self.assertEqual(response.status_code, 404)
        response = _post_json(
            self.client, "/api/v1/billing/reversals",
            {"entry_id": other_entry.pk, "reason": "跨物业冲销测试"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"reversal:{other_entry.reference}").exists())

    def test_folio_detail_not_found_and_404_envelope(self):
        self._login(self.finance)
        response = self.client.get("/api/v1/billing/folios/999999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "RESOURCE_NOT_FOUND")


class BillingCenterFolioDetailTests(BillingCenterTestBase):
    """账单详情：entries/payments/refunds 明细与金额字符串口径。"""

    def test_folio_detail_returns_full_ledger_payment_refund_detail(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        collect = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "300.00",
             "method": "CARD", "reference": f"bc-detail-{self._seq}"})
        self.assertEqual(collect.status_code, 201, collect.content)
        payment_id = collect.json()["data"]["payment_id"]
        refund = _post_json(
            self.client, "/api/v1/billing/refunds",
            {"payment_id": payment_id, "amount": "50.00", "reason": "明细测试退款"},
            idempotency_key=f"bc-ref-detail-{self._seq}")
        self.assertEqual(refund.status_code, 201, refund.content)

        response = self.client.get(f"/api/v1/billing/folios/{folio.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], "OPEN")
        self.assertEqual(data["reservation"]["booking_no"], reservation.booking_no)
        self.assertEqual(data["guest"]["full_name"], reservation.guest.full_name)
        # 余额 = 房费 300 - 收款 300 = 0.00（账本口径；退款申请 PENDING 尚未入账，
        # REFUND 反向调整凭证要等双人审批执行后才追加——账本只记已发生事实）
        self.assertEqual(data["summary"]["charges"], "300.00")
        self.assertEqual(data["summary"]["credits"], "300.00")
        self.assertEqual(data["summary"]["balance"], "0.00")
        entry_types = {row["entry_type"] for row in data["entries"]}
        self.assertEqual(entry_types, {"ROOM_CHARGE", "PAYMENT"})
        for row in data["entries"]:
            self.assertIsInstance(row["amount"], str)
        self.assertEqual(len(data["payments"]), 1)
        payment_row = data["payments"][0]
        self.assertEqual(payment_row["method"], "CARD")
        self.assertEqual(payment_row["status_label"], "成功")
        self.assertEqual(len(payment_row["refunds"]), 1)
        refund_row = payment_row["refunds"][0]
        self.assertEqual(refund_row["amount"], "50.00")
        self.assertEqual(refund_row["status"], "PENDING")
        self.assertEqual(refund_row["requested_by"], "fin_center_user")


class BillingCenterWriteTests(BillingCenterTestBase):
    """写操作：成功路径 DB 副作用 + 幂等 + 权限 403 + 业务校验。"""

    def test_collect_payment_creates_payment_ledger_audit_and_balance(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        response = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "250.00",
             "method": "CASH", "reference": f"bc-pay-{self._seq}"})
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], "SUCCEEDED")
        self.assertEqual(data["amount"], "250.00")
        self.assertEqual(data["currency"], "CNY")
        self.assertEqual(data["folio_id"], folio.pk)

        payment = Payment.objects.get(pk=data["payment_id"])
        self.assertEqual(payment.provider, "frontdesk_manual")
        self.assertEqual(payment.method, "CASH")
        self.assertEqual(payment.amount, Decimal("250.00"))
        self.assertEqual(payment.status, Payment.Status.SUCCEEDED)
        entry = LedgerEntry.objects.get(reference=f"payment:{payment.provider_ref}")
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.PAYMENT)
        self.assertEqual(entry.amount, Decimal("-250.00"))
        self.assertEqual(entry.folio_id, folio.pk)
        balance = sum((e.amount for e in folio.entries.all()), Decimal("0.00"))
        self.assertEqual(balance, Decimal("50.00"))
        self.assertTrue(self._audit_exists(
            "billing.collect", entity_id=payment.pk, property_obj=self.property))

    def test_collect_payment_reference_idempotent_replay(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        reference = f"bc-idem-{self._seq}"
        first = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "100.00",
             "method": "CASH", "reference": reference})
        self.assertEqual(first.status_code, 201, first.content)
        replay = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "100.00",
             "method": "CASH", "reference": reference})
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["data"]["payment_id"],
                         first.json()["data"]["payment_id"])
        self.assertEqual(
            Payment.objects.filter(provider_ref=reference).count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(reference=f"payment:{reference}").count(), 1)
        # 同 reference 不同支付事实 → 400，不产生第二条记录
        conflict = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "200.00",
             "method": "CASH", "reference": reference})
        self.assertEqual(conflict.status_code, 400, conflict.content)
        self.assertEqual(
            Payment.objects.filter(provider_ref=reference).count(), 1)

    def test_collect_payment_over_outstanding_rejected(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        response = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "300.01",
             "method": "CASH", "reference": f"bc-over-{self._seq}"})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertFalse(Payment.objects.filter(
            reservation=reservation, provider="frontdesk_manual").exists())

    def test_collect_payment_requires_membership_and_finance_access(self):
        """写操作权限不足：匿名 401 / 无任职 403 / 无 finance_access 403。"""
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        payload = {"reservation_id": reservation.pk, "amount": "10.00",
                   "method": "CASH", "reference": f"bc-denied-{self._seq}"}
        self._logout()
        self.assertEqual(_post_json(
            self.client, "/api/v1/billing/payments", payload).status_code, 401)
        self._login(self.no_membership_user)
        self.assertEqual(_post_json(
            self.client, "/api/v1/billing/payments", payload).status_code, 403)
        self._login(self.no_finance)
        response = _post_json(
            self.client, "/api/v1/billing/payments", payload)
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        self.assertFalse(Payment.objects.filter(
            provider_ref=payload["reference"]).exists())
        self.assertFalse(self._audit_exists("billing.collect"))

    def test_refund_request_creates_refund_approval_and_audit(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        payment_id = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "300.00",
             "method": "CASH", "reference": f"bc-rf-{self._seq}"}
        ).json()["data"]["payment_id"]
        response = _post_json(
            self.client, "/api/v1/billing/refunds",
            {"payment_id": payment_id, "amount": "80.00", "reason": "客人协商退款"},
            idempotency_key=f"bc-rf-key-{self._seq}")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["status"], "PENDING")

        refund = Refund.objects.get(pk=data["refund_id"])
        self.assertEqual(refund.amount, Decimal("80.00"))
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertEqual(refund.requested_by_id, self.finance.pk)
        self.assertEqual(refund.idempotency_key, f"bc-rf-key-{self._seq}")
        approval = ApprovalRecord.objects.get(pk=refund.approval_record_id)
        self.assertEqual(approval.action_code, "billing.refund")
        self.assertEqual(approval.status, ApprovalRecord.Status.PENDING)
        self.assertEqual(approval.requested_by_id, self.finance.pk)
        self.assertTrue(self._audit_exists(
            "billing.refund_request", entity_id=refund.pk,
            property_obj=self.property))

    def test_refund_request_idempotent_and_conflict(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        payment_id = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "300.00",
             "method": "CASH", "reference": f"bc-rf2-{self._seq}"}
        ).json()["data"]["payment_id"]
        key = f"bc-rf2-key-{self._seq}"
        payload = {"payment_id": payment_id, "amount": "60.00",
                   "reason": "幂等退款测试", "idempotency_key": key}
        first = _post_json(self.client, "/api/v1/billing/refunds", payload)
        self.assertEqual(first.status_code, 201, first.content)
        replay = _post_json(self.client, "/api/v1/billing/refunds", payload)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["data"]["refund_id"],
                         first.json()["data"]["refund_id"])
        self.assertEqual(Refund.objects.filter(idempotency_key=key).count(), 1)
        # 同幂等键不同事实 → 400
        conflict = _post_json(
            self.client, "/api/v1/billing/refunds",
            dict(payload, amount="70.00"))
        self.assertEqual(conflict.status_code, 400, conflict.content)
        self.assertEqual(Refund.objects.filter(idempotency_key=key).count(), 1)
        # 超额退款 → 400（累计退款不得超过原支付金额）
        over = _post_json(
            self.client, "/api/v1/billing/refunds",
            {"payment_id": payment_id, "amount": "301.00", "reason": "超额度"},
            idempotency_key=f"bc-rf2-over-{self._seq}")
        self.assertEqual(over.status_code, 400, over.content)

    def test_refund_request_requires_refund_request_permission(self):
        """refund_request 专项权：财务可申请；仅 finance_access 无专项权 → 403。"""
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        self._login(self.finance)
        payment_id = _post_json(
            self.client, "/api/v1/billing/payments",
            {"reservation_id": reservation.pk, "amount": "300.00",
             "method": "CASH", "reference": f"bc-rf3-{self._seq}"}
        ).json()["data"]["payment_id"]
        view_only = self._make_staff(
            "fin_center_viewonly", "前台",
            extra_codenames=["reverse_entry"], finance_access=False)
        self._login(view_only)
        response = _post_json(
            self.client, "/api/v1/billing/refunds",
            {"payment_id": payment_id, "amount": "10.00", "reason": "无权限退款"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        self.assertEqual(Refund.objects.filter(payment_id=payment_id).count(), 0)

        # 持有 billing.refund_request 的财务账号可正常申请（有权限路径）
        self._login(self.finance)
        ok = _post_json(
            self.client, "/api/v1/billing/refunds",
            {"payment_id": payment_id, "amount": "10.00", "reason": "有权限退款"},
            idempotency_key=f"bc-rf3-ok-{self._seq}")
        self.assertEqual(ok.status_code, 201, ok.content)
        self.assertEqual(Refund.objects.filter(payment_id=payment_id).count(), 1)

    def test_reverse_entry_posts_paired_reversal_with_audit_and_idempotent(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        original = folio.entries.get(entry_type=LedgerEntry.EntryType.ROOM_CHARGE)
        self._login(self.finance)
        response = _post_json(
            self.client, "/api/v1/billing/reversals",
            {"entry_id": original.pk, "reason": "录错房价，红字冲销重入"})
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        self.assertEqual(data["entry_type"], "REVERSAL")
        self.assertEqual(data["amount"], "-300.00")
        self.assertEqual(data["void_of_id"], original.pk)

        original.refresh_from_db()
        self.assertEqual(original.amount, Decimal("300.00"))
        self.assertIsNone(original.void_of_id)  # 原凭证不可变
        reversal = LedgerEntry.objects.get(pk=data["reversal_id"])
        self.assertEqual(reversal.void_of_id, original.pk)
        self.assertEqual(reversal.void_reason, "录错房价，红字冲销重入")
        self.assertEqual(reversal.reference, f"reversal:{original.reference}")
        balance = sum((e.amount for e in folio.entries.all()), Decimal("0.00"))
        self.assertEqual(balance, Decimal("0.00"))
        self.assertTrue(self._audit_exists(
            "billing.reverse_entry", entity_id=original.pk,
            property_obj=self.property))

        # 幂等重放：同一原凭证只冲一次，返回首次反向凭证
        replay = _post_json(
            self.client, "/api/v1/billing/reversals",
            {"entry_id": original.pk, "reason": "录错房价，红字冲销重入"})
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["data"]["reversal_id"], reversal.pk)
        self.assertEqual(
            LedgerEntry.objects.filter(void_of=original).count(), 1)

        # 红字凭证不可再冲销 → 400
        again = _post_json(
            self.client, "/api/v1/billing/reversals",
            {"entry_id": reversal.pk, "reason": "试图冲销红字凭证"})
        self.assertEqual(again.status_code, 400, again.content)

    def test_reverse_entry_requires_reverse_permission(self):
        """reverse_entry 专项权：仅 finance_access 无专项权 → 403，零副作用。"""
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        original = folio.entries.get(entry_type=LedgerEntry.EntryType.ROOM_CHARGE)
        view_only = self._make_staff("fin_center_revview", "前台",
                                     extra_codenames=[])
        self._login(view_only)
        response = _post_json(
            self.client, "/api/v1/billing/reversals",
            {"entry_id": original.pk, "reason": "无权限冲销"})
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
        self.assertFalse(LedgerEntry.objects.filter(void_of=original).exists())
        self.assertFalse(self._audit_exists("billing.reverse_entry"))

    def test_reverse_entry_reason_required(self):
        reservation, folio = self._booking_with_outstanding(Decimal("300.00"))
        original = folio.entries.get(entry_type=LedgerEntry.EntryType.ROOM_CHARGE)
        self._login(self.finance)
        response = _post_json(
            self.client, "/api/v1/billing/reversals",
            {"entry_id": original.pk, "reason": "   "})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(LedgerEntry.objects.filter(void_of=original).exists())


class BillingCenterPageTests(BillingCenterTestBase):
    """/manage/finance/ 财务中心页面注册与门禁。"""

    def test_finance_center_page_requires_login_and_permission(self):
        response = self.client.get("/manage/finance/")
        self.assertEqual(response.status_code, 302)  # login_required
        self.assertIn("/login/", response["Location"])
        self._login(self.no_finance)
        self.assertEqual(self.client.get("/manage/finance/").status_code, 403)
        self._login(self.no_membership_user)
        self.assertEqual(self.client.get("/manage/finance/").status_code, 403)

    def test_finance_center_page_renders_for_finance_staff(self):
        self._booking_with_outstanding()
        self._login(self.finance)
        response = self.client.get("/manage/finance/")
        self.assertEqual(response.status_code, 200, response.content)
        content = response.content.decode("utf-8")
        self.assertIn("财务与账单中心", content)
        self.assertIn("/api/v1/billing/folios", content)
        self.assertIn("/api/v1/billing/payments", content)
        self.assertIn("/api/v1/billing/refunds", content)
        self.assertIn('id="fin-page-size"', content)
        self.assertIn('id="fin-goto-page"', content)
        self.assertIn('id="fin-goto-btn"', content)



class BillingSummaryCacheInvalidationTests(BillingCenterTestBase):
    """账本写入后，未结余额摘要缓存必须在事务提交时失效。"""

    def test_post_entry_invalidates_open_summary_cache_after_commit(self):
        from django.core.cache import cache

        _reservation, folio = self._booking_with_outstanding()
        key = "billing:open-summary:%s" % self.property.pk
        cache.set(key, {"open_folios": 1, "open_balance": "300.00"}, 60)
        self.assertIsNotNone(cache.get(key))

        with self.captureOnCommitCallbacks(execute=True):
            BillingService.post_entry(
                property_obj=self.property,
                folio=folio,
                entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
                amount=Decimal("1.00"),
                reference="bc-cache-invalidation-%s" % uuid.uuid4().hex,
                description="缓存失效回归测试",
                posted_by=self.finance,
            )

        self.assertIsNone(cache.get(key))


class BillingRefundThreePartyApiTests(BillingCenterTestBase):
    """API 层验证申请、审批、执行独立账号及仅执行时落账。"""

    def test_refund_api_requires_three_distinct_people_and_posts_once(self):
        reservation, _folio = self._booking_with_outstanding()
        payment = BillingService.collect_payment(
            property_obj=self.property, reservation=reservation,
            amount="60.00", method="CARD", collected_by=self.finance,
            reference="BC-REFUND-3P-PAY-001")

        approver = self._make_staff("fin_refund_approver", "财务")
        executor = self._make_staff("fin_refund_executor", "财务")
        self._grant(approver, "refund_approve")
        self._grant(executor, "refund_execute")

        self._login(self.finance)
        requested = _post_json(
            self.client, "/api/v1/billing/refunds",
            {"payment_id": payment.pk, "amount": "25.00",
             "reason": "三方退款 API 验收",
             "idempotency_key": "bc-refund-3p-request-001"})
        self.assertEqual(requested.status_code, 201, requested.content)
        refund_id = requested.json()["data"]["refund_id"]
        refund = Refund.objects.get(pk=refund_id)
        self.assertEqual(refund.status, Refund.Status.PENDING)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        # 申请人即使拥有审批权限，也不能审批自己的退款。
        self._grant(self.finance, "refund_approve")
        self._login(self.finance)
        self_approval = _post_json(
            self.client, f"/api/v1/billing/refunds/{refund_id}/approve", {})
        self.assertEqual(self_approval.status_code, 400, self_approval.content)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        self._login(approver)
        approved = _post_json(
            self.client, f"/api/v1/billing/refunds/{refund_id}/approve", {})
        self.assertEqual(approved.status_code, 200, approved.content)
        refund.refresh_from_db()
        self.assertEqual(refund.approved_by_id, approver.pk)
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"refund:{refund.provider_ref}").exists())

        self._login(executor)
        executed = _post_json(
            self.client, f"/api/v1/billing/refunds/{refund_id}/execute",
            {"confirm_refund_paid": True,
             "execution_reference": "BANK-BC-REFUND-001",
             "idempotency_key": "bc-refund-3p-exec-001"})
        self.assertEqual(executed.status_code, 201, executed.content)
        refund.refresh_from_db()
        self.assertEqual(refund.executed_by_id, executor.pk)
        self.assertEqual(refund.status, Refund.Status.SUCCEEDED)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"refund:{refund.provider_ref}").count(), 1)

        replay = _post_json(
            self.client, f"/api/v1/billing/refunds/{refund_id}/execute",
            {"confirm_refund_paid": True,
             "execution_reference": "BANK-BC-REFUND-001",
             "idempotency_key": "bc-refund-3p-exec-001"})
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"refund:{refund.provider_ref}").count(), 1)


class FinanceLegacyRouteRetirementTests(BillingCenterTestBase):
    """旧 /finance 页面只迁移到 PMS2；旧财务写入口永久退役。"""

    def test_legacy_finance_page_redirects_to_single_pms2_entry(self):
        self._login(self.finance)
        response = self.client.get("/finance/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/manage/finance/")

    def test_legacy_finance_write_endpoint_is_gone(self):
        self._login(self.finance)
        response = self.client.post("/finance_add/", {
            "type": "收入", "amount": "10.00",
            "reason": "不得进入旧账本", "date": self.today.isoformat()})
        self.assertEqual(response.status_code, 410)
        self.assertFalse(
            LedgerEntry.objects.filter(description="不得进入旧账本").exists())
