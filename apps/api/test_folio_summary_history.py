"""V12-10：退房后财务摘要展示口径验收（当前未结应收为 0 ≠ 历史账务消失）。

背景：退房后 CLOSED Folio 不再进入"当前应收"聚合，摘要三个数字会同时归零；
如果页面只有这三个数字，用户会把"归零"读成"历史账单/收款/退款记录消失"。

本模块固化两条契约（不改变账务计算规则）：
1. `folio_summary` 的 charges/credits/balance 仍然只统计 OPEN 账单（既有
   D-R4-08 口径，不回退、不把 CLOSED 重新纳入聚合）；
2. 同时给出 `closed_folio_count/closing_charges/closed_credits` 历史口径，
   供页面在"当前未结应收为 0"时说明历史账务仍在；
3. 账单明细接口（``front-desk/reservations/<no>/folios``）在账单已结后仍可
   逐笔查看历史凭证与收款——历史记录入口不能因为关账而消失。
"""

import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.billing.models import Folio, LedgerEntry
from apps.core.models import Property
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.reservations.models import Reservation, ReservationRoom

_TEST_PW = get_random_string(24)


class FolioSummaryHistoryTests(TestCase):
    """退房（CLOSED Folio）后的摘要展示契约。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.today = timezone.localdate()
        self.user = self._staff_with(["view_accommodation"], app_label="hotelapp")
        self.client.force_login(self.user)
        self.reservation = self._reservation()
        self.folio = Folio.objects.create(
            property=self.property,
            reservation=self.reservation,
            guest=self.reservation.guest,
            folio_type=Folio.FolioType.MASTER,
            status=Folio.Status.CLOSED,
            currency="CNY",
        )
        self._entry(LedgerEntry.EntryType.ROOM_CHARGE, "200.00", "已结账房费")
        self._entry(LedgerEntry.EntryType.PAYMENT, "-200.00", "已收款")

    # ------------------------------------------------------------------
    # 夹具
    # ------------------------------------------------------------------
    def _staff_with(self, codenames, app_label):
        group = Group.objects.create(name=f"V1210-{uuid.uuid4().hex[:8]}")
        group.permissions.add(
            *Permission.objects.filter(content_type__app_label=app_label, codename__in=codenames)
        )
        group.permissions.add(
            *Permission.objects.filter(content_type__app_label="core", codename="finance_access")
        )
        user = User.objects.create_user(username=f"v1210-{uuid.uuid4().hex[:8]}", password=_TEST_PW)
        profile = StaffProfile.objects.create(
            user=user,
            full_name="V12-10 前台",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no=f"V1210-{user.pk:06d}",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        return user

    def _reservation(self):
        from apps.inventory.models import RatePlan, RoomType

        room_type = RoomType.objects.create(
            property=self.property, code="V1210-KING", name="摘要测试大床房", capacity=2
        )
        rate_plan = RatePlan.objects.create(
            property=self.property,
            code="V1210-BAR",
            name="摘要测试门市价",
            valid_from=self.today - timedelta(days=1),
        )
        guest = Guest.objects.create(property=self.property, full_name="摘要测试住客")
        reservation = Reservation.objects.create(
            property=self.property,
            guest=guest,
            booking_no=f"V1210-{uuid.uuid4().hex[:8].upper()}",
            status=Reservation.Status.CHECKED_OUT,
            check_in=self.today - timedelta(days=1),
            check_out=self.today,
            room_count=1,
            total_amount=Decimal("200.00"),
            currency="CNY",
        )
        ReservationRoom.objects.create(
            property=self.property,
            reservation=reservation,
            room_type=room_type,
            rate_plan=rate_plan,
            check_in=self.today - timedelta(days=1),
            check_out=self.today,
            amount=Decimal("200.00"),
        )
        return reservation

    def _entry(self, entry_type, amount, description):
        return LedgerEntry.objects.create(
            property=self.property,
            folio=self.folio,
            entry_type=entry_type,
            amount=Decimal(amount),
            currency="CNY",
            business_date=self.today,
            reference=f"v1210:{uuid.uuid4().hex[:8]}",
            description=description,
            posted_by=self.user,
        )

    # ------------------------------------------------------------------
    # 契约
    # ------------------------------------------------------------------
    def test_closed_folio_keeps_current_balance_zero_but_exposes_history(self):
        response = self.client.get(f"/api/v1/front-desk/reservations/{self.reservation.booking_no}")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)["data"]
        summary = data["folio_summary"]

        # 当前口径：CLOSED 不进聚合 → 未结应收为 0（契约不回退）
        self.assertEqual(summary["balance"], "0.00")
        self.assertEqual(summary["charges"], "0.00")
        self.assertEqual(summary["credits"], "0.00")
        self.assertEqual(summary["status"], Folio.Status.CLOSED)
        # 历史口径：已结账单的消费/收款仍可读出
        self.assertEqual(summary["closed_folio_count"], 1)
        self.assertEqual(summary["closed_charges"], "200.00")
        self.assertEqual(summary["closed_credits"], "200.00")

    def test_closed_folio_entries_remain_queryable(self):
        """历史账单明细入口不得因关账而消失（逐笔凭证 + 收款仍可查）。"""
        response = self.client.get(f"/api/v1/front-desk/folios/{self.reservation.booking_no}")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)["data"]
        types = [item["entry_type"] for item in data["entries"]]
        self.assertIn(LedgerEntry.EntryType.ROOM_CHARGE, types)
        self.assertIn(LedgerEntry.EntryType.PAYMENT, types)
        self.assertEqual(len(data["entries"]), 2)

    def test_open_folio_history_fields_stay_zero(self):
        """未结账单不产生"历史"计数，避免摘要把在住账读成已结历史。"""
        self.folio.status = Folio.Status.OPEN
        self.folio.save(update_fields=("status",))
        response = self.client.get(f"/api/v1/front-desk/reservations/{self.reservation.booking_no}")
        summary = json.loads(response.content)["data"]["folio_summary"]
        self.assertEqual(summary["closed_folio_count"], 0)
        self.assertEqual(summary["closed_charges"], "0.00")
        self.assertEqual(summary["closed_credits"], "0.00")
        self.assertEqual(summary["balance"], "0.00")
        self.assertEqual(summary["charges"], "200.00")
        self.assertEqual(summary["credits"], "200.00")
