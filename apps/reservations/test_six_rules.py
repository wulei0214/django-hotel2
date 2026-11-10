# -*- coding: utf-8 -*-
"""六项业务规则验收测试。"""
import datetime
from datetime import timedelta
import unittest

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import Property, AuditEvent
from apps.guests.models import Guest
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType, RoomTypeInventory
from apps.identity.models import PropertyStaffMembership, StaffProfile
from apps.billing.models import Folio
from apps.reservations.models import (
    EarlyDeparturePolicy,
    Reservation,
    ReservationRoom,
)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class DecDep01DepositPolicyTests(TestCase):
    """DEC-DEP-01：押金 30% 服务端计算。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.rt = RoomType.objects.create(property=cls.prop, code="DEP-STD", name="押金测试", capacity=2)
        cls.rate = RatePlan.objects.create(property=cls.prop, code="DEP-BAR", name="押金门市价", valid_from=timezone.localdate())
        from apps.reservations.models import DepositPolicy
        DepositPolicy.objects.create(rate_plan=cls.rate, deposit_type="PERCENT", deposit_value=Decimal("30.00"))

    def _make_booking(self, total, phone):
        guest = Guest.objects.create(property=self.prop, full_name="押金测试客")
        res = Reservation.objects.create(
            property=self.prop, guest=guest, booking_no=f"DEP-{phone[-4:]}",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(), check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal(str(total)), currency="CNY")
        ReservationRoom.objects.create(
            property=self.prop, reservation=res, room_type=self.rt, rate_plan=self.rate,
            check_in=timezone.localdate(), check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal(str(total)))
        folio = Folio.objects.create(property=self.prop, reservation=res, guest=guest, currency="CNY")
        finance = User.objects.create_user(username=f"dep_{phone[-4:]}", password="x")
        return res, folio, finance

    def test_30_percent_server_side(self):
        res, folio, finance = self._make_booking(1000, "1390000001")
        from apps.billing.services_v2 import BillingService
        deposit, _ = BillingService.record_deposit(
            property_obj=self.prop, reservation=res,
            amount="999.00", method="CARD", idempotency_key="dep-001", received_by=finance)
        self.assertEqual(deposit.amount, Decimal("300.00"))

    def test_no_policy_passes_through(self):
        rt2 = RoomType.objects.create(property=self.prop, code="DEP-NOP", name="无政策房型", capacity=2)
        rate2 = RatePlan.objects.create(property=self.prop, code="DEP-NOP-BAR", name="无政策门市价", valid_from=timezone.localdate())
        res, folio, finance = self._make_booking(500, "1390000002")
        res.reservation_rooms.update(room_type=rt2, rate_plan=rate2)
        from apps.billing.services_v2 import BillingService
        deposit, _ = BillingService.record_deposit(
            property_obj=self.prop, reservation=res,
            amount="200.00", method="CARD", idempotency_key="dep-002", received_by=finance)
        self.assertEqual(deposit.amount, Decimal("150.00"))  # WB-030: 500*30%=150


@override_settings(DEBUG=True, DJANGO_ENV="test")
class DecEmp01RehireTests(TestCase):
    """DEC-EMP-01：员工重新入职（约束迁移后同一员工可再入职）。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")

    def _make_profile(self, username, emp_no):
        user = User.objects.create_user(username=username, password="x")
        return StaffProfile.objects.create(
            user=user, full_name=f"员工{emp_no}")

    def test_rehire_after_termination(self):
        profile = self._make_profile("rehire_t1", "FO-9101")
        PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile, employee_no="FO-9101",
            position_code="FO", status=PropertyStaffMembership.Status.TERMINATED,
            started_at=timezone.now() - timedelta(days=30),
            ended_at=timezone.now().date() - timedelta(days=1))
        m2 = PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile, employee_no="FO-9102",
            position_code="FO", status=PropertyStaffMembership.Status.ACTIVE,
            started_at=timezone.now())
        self.assertEqual(m2.status, PropertyStaffMembership.Status.ACTIVE)
        self.assertEqual(PropertyStaffMembership.objects.filter(
            property=self.prop, staff_profile=profile).count(), 2)

    @override_settings(DEBUG=True, DJANGO_ENV="test")
    @unittest.expectedFailure
    def test_active_duplicate_blocked(self):
        """同一时点两条 ACTIVE 任职应被阻止。
        MySQL backend 不支持 partial unique index（P0-06 §13.4 已知限制），
        条件唯一约束仅在 SQLite 生效；MySQL 需改用生成列+唯一索引方案。"""
        profile = self._make_profile("rehire_t2", "FO-9103")
        PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile, employee_no="FO-9103",
            position_code="FO", status=PropertyStaffMembership.Status.ACTIVE,
            started_at=timezone.now())
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            PropertyStaffMembership.objects.create(
                property=self.prop, staff_profile=profile, employee_no="FO-9104",
                position_code="FO", status=PropertyStaffMembership.Status.ACTIVE,
                started_at=timezone.now())


@override_settings(DEBUG=True, DJANGO_ENV="test")
class DecEmp02EmployeeNumberTests(TestCase):
    """DEC-EMP-02：工号自动生成。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")

    def test_sequence_generation(self):
        from apps.identity.employee_number import generate_employee_no
        no1 = generate_employee_no(property_obj=self.prop, department_code="FO")
        # 创建一条 membership 记录使下次递增
        from apps.guests.models import Guest
        guest = Guest.objects.create(property=self.prop, full_name="编号测试")
        user = User.objects.create_user(username="seq_emp", password="x")
        profile = StaffProfile.objects.create(user=user, full_name="编号测试")
        PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile, employee_no=no1,
            position_code="FO", status=PropertyStaffMembership.Status.ACTIVE,
            started_at=timezone.now())
        no2 = generate_employee_no(property_obj=self.prop, department_code="FO")
        self.assertTrue(no1.startswith("FO-"))
        self.assertNotEqual(no1, no2)

    def test_different_departments_independent(self):
        from apps.identity.employee_number import generate_employee_no
        fo = generate_employee_no(property_obj=self.prop, department_code="FO")
        hk = generate_employee_no(property_obj=self.prop, department_code="HK")
        self.assertTrue(fo.startswith("FO-"))
        self.assertTrue(hk.startswith("HK-"))

    def test_invalid_department_rejected(self):
        from apps.identity.employee_number import generate_employee_no
        with self.assertRaises(ValueError):
            generate_employee_no(property_obj=self.prop, department_code="XXX")


# DEC-WALKIN-01 Walk-in 原子化创建走 E2E 验证
@override_settings(DEBUG=True, DJANGO_ENV="test")
class DecEd01EarlyDepartureTests(TestCase):
    """DEC-ED-01：提前离店政策计算（纯函数测试）。"""

    def _make_line(self, nightly_prices, rate_plan_id=None):
        check_in = datetime.date(2026, 11, 10)
        check_out = check_in + datetime.timedelta(days=len(nightly_prices))
        return ReservationRoom(
            nightly_rates=[{"date": str(check_in + datetime.timedelta(days=i)), "price": p}
                           for i, p in enumerate(nightly_prices)],
            amount=sum(Decimal(p) for p in nightly_prices),
            check_in=check_in, check_out=check_out,
            rate_plan_id=rate_plan_id)

    def test_no_policy_no_penalty(self):
        from apps.reservations.walkin import calculate_early_departure
        line = self._make_line(["300.00", "300.00", "300.00"])
        result = calculate_early_departure(
            line=line, actual_check_out=datetime.datetime(2026, 11, 11, 12, 0))
        self.assertEqual(result["actual_room_charge"], Decimal("300.00"))
        self.assertEqual(result["penalty"], Decimal("0.00"))

    def test_first_night_policy(self):
        prop = Property.objects.get(code="DEFAULT")
        rate = RatePlan.objects.create(
            property=prop, code="ED-BAR", name="提前离店测试", valid_from=timezone.localdate())
        EarlyDeparturePolicy.objects.create(
            rate_plan=rate, policy_type="FIRST_NIGHT", is_active=True)
        from apps.reservations.walkin import calculate_early_departure
        line = self._make_line(["300.00", "300.00"], rate_plan_id=rate.pk)
        result = calculate_early_departure(
            line=line, actual_check_out=datetime.datetime(2026, 11, 11, 12, 0))
        self.assertEqual(result["actual_room_charge"], Decimal("300.00"))
        self.assertEqual(result["penalty"], Decimal("300.00"))
