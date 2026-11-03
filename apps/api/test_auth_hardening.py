# -*- coding: utf-8 -*-
"""身份认证与账户体系 Phase 0 止血验收测试（AUTH-01/02/04/05/07/08/09）。

依据 docs/PMS2身份认证与员工住客账户体系改造开发方案.md §验收：
- 公开注册不创建 User；旧 booking 不写旧业务表
- 无开放重定向；登录错误不泄露账号状态
- 密码规则使用 validate_password；生产不能创建 mock payment
- 订单查询/取消有限流与审计
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.identity.services import EmployeeLifecycleError, EmployeeLifecycleService
from apps.identity.models import StaffProfile

from hotelapp.tests import _make_user  # 复用员工夹具（Membership+RoleAssignment）


@override_settings(DEBUG=True, DJANGO_ENV="test")
class RegisterAndBookingFreezeTests(TestCase):
    """AUTH-01/02：注册止血 + 旧预订冻结"""

    def test_register_redirects_and_creates_nothing(self):
        before_users = User.objects.count()
        response = self.client.post("/register/", {
            "username": "ghost_user", "password": "x", "password2": "x",
            "fullname": "幽灵", "tel": "13800009999",
        })
        self.assertRedirects(response, "/reserve/", fetch_redirect_response=False)
        self.assertEqual(User.objects.count(), before_users)

    def test_booking_page_frozen_to_reserve(self):
        response = self.client.get("/booking/")
        self.assertRedirects(response, "/reserve/", fetch_redirect_response=False)

    def test_booking_post_writes_no_legacy_rows(self):
        from hotelapp.models import Accommodation, Client
        before_client = Client.objects.count()
        before_accom = Accommodation.objects.count()
        response = self.client.post("/booking/", {
            "name": "测试顾客", "tel": "13800001234",
            "room_type": "标准间", "date": str(timezone.localdate()),
        })
        self.assertRedirects(response, "/reserve/", fetch_redirect_response=False)
        self.assertEqual(Client.objects.count(), before_client,
                         "冻结后不得再写旧 Client 表")
        self.assertEqual(Accommodation.objects.count(), before_accom,
                         "冻结后不得再写旧 Accommodation 表")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class LoginErrorUnificationTests(TestCase):
    """AUTH-04：登录失败对外统一文案，不泄露账号状态"""

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        self.member = _make_user("auth_normal", "前台")

    def test_wrong_password_message(self):
        response = self.client.post("/login/", {
            "username": "auth_normal", "password": "wrong-pass"})
        self.assertContains(response, "账号或密码不正确，或账号暂不可用")

    def test_unknown_user_same_message(self):
        response = self.client.post("/login/", {
            "username": "no_such_user", "password": "whatever"})
        self.assertContains(response, "账号或密码不正确，或账号暂不可用")

    def test_disabled_user_same_message(self):
        self.member.is_active = False
        self.member.save(update_fields=("is_active",))
        response = self.client.post("/login/", {
            "username": "auth_normal", "password": "auth_normal-pass123"})
        self.assertContains(
            response, "账号或密码不正确，或账号暂不可用",
            msg_prefix="停用状态不得单独提示（避免泄露账号存在性）: ")

    def test_no_employment_same_message(self):
        user = User.objects.create_user(username="noemp01", password="noemp01-pass123")
        StaffProfile.objects.create(user=user, full_name="无任职",
                                    employment_status="TERMINATED")
        response = self.client.post("/login/", {
            "username": "noemp01", "password": "noemp01-pass123"})
        self.assertContains(response, "账号或密码不正确，或账号暂不可用")


@override_settings(DEBUG=True, DJANGO_ENV="test")
class PasswordPolicyTests(TestCase):
    """AUTH-05：开通/绑定/重置统一 validate_password"""

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        self.owner = _make_user("auth_owner", "店长")
        self.target = _make_user("auth_target", "前台")
        self.profile = self.target.membership.staff_profile \
            if hasattr(self.target, "membership") else None
        if self.profile is None:
            from apps.identity.models import PropertyStaffMembership
            self.profile = StaffProfile.objects.get(user=self.target)

    def _membership(self):
        from apps.identity.models import PropertyStaffMembership
        return PropertyStaffMembership.objects.get(
            staff_profile=self.profile, property=self.prop)

    def test_reset_rejects_weak_password(self):
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.reset_password(
                self.profile, property_obj=self.prop,
                new_password="123456", reset_by=self.owner)

    def test_reset_accepts_strong_password(self):
        EmployeeLifecycleService.reset_password(
            self.profile, property_obj=self.prop,
            new_password="Str0ng!Pass2026", reset_by=self.owner)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("Str0ng!Pass2026"))


@override_settings(DEBUG=True, DJANGO_ENV="test", PAYMENT_WEBHOOK_SECRET="auth-test-secret")
class MockPaymentGateTests(TestCase):
    """AUTH-07：mock 支付门禁 + AUTH-08：查询面限流"""

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        from apps.guests.models import Guest
        from apps.reservations.models import Reservation
        self.guest = Guest.objects.create(
            property=self.prop, full_name="支付测试", phone="13800007777",
            phone_normalized="13800007777")
        self.reservation = Reservation.objects.create(
            property=self.prop, booking_no="BKAUTH0001", guest=self.guest,
            status=Reservation.Status.PENDING,
            check_in=timezone.localdate() + timedelta(days=2),
            check_out=timezone.localdate() + timedelta(days=3),
            room_count=1, total_amount=Decimal("580.00"),
            expires_at=timezone.now() + timedelta(minutes=30))
        # mock 支付落 Payment 需要已开的住客账单（与公开预订主流程一致）
        from apps.billing.models import Folio
        Folio.objects.create(property=self.prop, reservation=self.reservation,
                             guest=self.guest, currency="CNY")

    def test_mock_payment_blocked_outside_dev(self):
        with override_settings(DEBUG=False, DJANGO_ENV="prod"):
            response = self.client.post("/api/v1/payments/intents", {
                "booking_no": "BKAUTH0001", "phone": "13800007777"},
                content_type="application/json")
            self.assertEqual(response.status_code, 403,
                             "生产环境必须拒绝创建 mock payment")

    def test_mock_payment_allowed_in_dev(self):
        response = self.client.post("/api/v1/payments/intents", {
            "booking_no": "BKAUTH0001", "phone": "13800007777"},
            content_type="application/json")
        self.assertEqual(response.status_code, 201)

    def test_payment_query_throttled_after_limit(self):
        payload = {"booking_no": "BKAUTH0001", "phone": "13800007777"}
        last = None
        for _ in range(12):
            last = self.client.post("/api/v1/payments/intents", payload,
                                    content_type="application/json")
        self.assertEqual(last.status_code, 429, "超限后必须返回 429")
        self.assertTrue(AuditEvent.objects.filter(
            entity_type="public_throttle_blocked").exists(),
            "限流拦截必须落审计")
