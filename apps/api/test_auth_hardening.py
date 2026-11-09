# -*- coding: utf-8 -*-
"""身份认证与账户体系 Phase 0 止血验收测试（AUTH-01/02/04/05/07/08/09）。

依据 docs/PMS2身份认证与员工住客账户体系改造开发方案.md §验收：
- 公开注册不创建 User；旧 booking 不写旧业务表
- 无开放重定向；登录错误不泄露账号状态
- 密码规则使用 validate_password；生产不能创建 mock payment
- 订单查询/取消有限流与审计
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.reservations.models import BookingAccessToken, Reservation
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

    def _intent_payload(self, booking_no, token):
        return {"booking_no": booking_no, "token": token}

    def test_mock_payment_blocked_outside_dev(self):
        token = BookingAccessToken.issue_for(self.reservation)
        with override_settings(DEBUG=False, DJANGO_ENV="prod"):
            response = self.client.post("/api/v1/payments/intents",
                self._intent_payload("BKAUTH0001", token),
                content_type="application/json")
            self.assertEqual(response.status_code, 403,
                             "生产环境必须拒绝创建 mock payment")

    def test_mock_payment_allowed_in_dev(self):
        token = BookingAccessToken.issue_for(self.reservation)
        response = self.client.post("/api/v1/payments/intents",
            self._intent_payload("BKAUTH0001", token),
            content_type="application/json")
        self.assertEqual(response.status_code, 201)

    def test_payment_intent_rejects_wrong_token(self):
        response = self.client.post("/api/v1/payments/intents",
            self._intent_payload("BKAUTH0001", "bad-token"),
            content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_payment_query_throttled_after_limit(self):
        token = BookingAccessToken.issue_for(self.reservation)
        payload = self._intent_payload("BKAUTH0001", token)
        last = None
        for _ in range(12):
            last = self.client.post("/api/v1/payments/intents", payload,
                                    content_type="application/json")
        self.assertEqual(last.status_code, 429, "超限后必须返回 429")
        self.assertTrue(AuditEvent.objects.filter(
            action="public.rate_limited").exists(),
            "限流拦截必须落审计")

@override_settings(DEBUG=True, DJANGO_ENV="test")
class LoginThrottleTests(TestCase):
    """P0-11：登录失败限流（IP+username 5 次/5 分钟，fail-closed）"""

    def test_login_throttled_after_failures(self):
        last = None
        for _ in range(6):
            last = self.client.post("/login/", {
                "username": "ghost_login", "password": "wrong"})
        self.assertEqual(last.status_code, 429, "连续失败后必须限流")
        self.assertTrue(AuditEvent.objects.filter(
            action="auth.login_failure").exists(), "失败必须持久审计")

    def test_login_failure_audited_with_fingerprint(self):
        self.client.post("/login/", {"username": "some_user", "password": "bad"})
        event = AuditEvent.objects.filter(action="auth.login_failure").latest("created_at")
        self.assertNotEqual(event.entity_id, "some_user", "不得存明文用户名")
        self.assertEqual(event.reason, "bad_credentials")


@override_settings(DEBUG=True, DJANGO_ENV="test", PAYMENT_WEBHOOK_SECRET="auth-test-secret")
class PublicBookingAntiAbuseTests(TestCase):
    """P0-07/08：公开预订防刷、PENDING 上限、token 生命周期、限流计数器"""

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory
        self.room_type = RoomType.objects.create(
            property=self.prop, code="AUTH-KING", name="防刷测试房", capacity=2)
        self.rate_plan = RatePlan.objects.create(
            property=self.prop, code="AUTH-BAR", name="防刷门市价",
            valid_from=timezone.localdate())
        for day in (timezone.localdate() + timedelta(days=n) for n in (3, 4)):
            DailyRate.objects.create(
                property=self.prop, rate_plan=self.rate_plan,
                room_type=self.room_type, stay_date=day, price=Decimal("399.00"))
            RoomTypeInventory.objects.create(
                property=self.prop, room_type=self.room_type, stay_date=day,
                total_rooms=5)

    def _payload(self, phone, key):
        return {
            "full_name": "防刷测试", "phone": phone,
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": (timezone.localdate() + timedelta(days=3)).isoformat(),
            "check_out": (timezone.localdate() + timedelta(days=5)).isoformat(),
            "rooms": 1, "adults": 1, "children": 0,
        }

    def test_pending_cap_blocks_third_unpaid_reservation(self):
        """§6.4.2：同一手机号 PENDING 上限（默认 2 笔）——第 3 笔 409 且不新增"""
        from apps.reservations.models import Reservation
        phone = "13811110000"
        for i in range(2):
            response = self.client.post(
                "/api/v1/reservations", data=json.dumps(self._payload(phone, "")),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="cap-key-%d" % i)
            self.assertEqual(response.status_code, 201)
        third = self.client.post(
            "/api/v1/reservations", data=json.dumps(self._payload(phone, "")),
            content_type="application/json", HTTP_IDEMPOTENCY_KEY="cap-key-2")
        self.assertEqual(third.status_code, 409, "同手机号 PENDING 上限（2 笔）必须拦截")
        self.assertEqual(Reservation.objects.filter(
            guest__phone_normalized=phone).count(), 2,
            "超限请求不得新增预订")

    def test_rate_limit_429_with_retry_after(self):
        """§6.3：phone 维度 3 次/10 分钟——第 4 笔同手机号请求 429 + Retry-After
        （第 3 笔会被 PENDING 上限 409 拦截，但限流计数仍累加）"""
        phone = "13822220000"
        statuses = []
        for i in range(4):
            response = self.client.post(
                "/api/v1/reservations",
                data=json.dumps(self._payload(phone, "")),
                content_type="application/json",
                HTTP_IDEMPOTENCY_KEY="rl-key-%d" % i)
            statuses.append(response.status_code)
        self.assertEqual(statuses[3], 429, "第 4 次同手机号请求必须 429")
        self.assertIn("Retry-After", response)

    def test_token_revocation_and_expiry_validation(self):
        from apps.reservations.models import BookingAccessToken, Reservation
        from apps.guests.models import Guest
        guest = Guest.objects.create(
            property=self.prop, full_name="令牌测试",
            phone="13833330000", phone_normalized="13833330000")
        reservation = Reservation.objects.create(
            property=self.prop, booking_no="BKTOK0001", guest=guest,
            status=Reservation.Status.PENDING,
            check_in=timezone.localdate() + timedelta(days=2),
            check_out=timezone.localdate() + timedelta(days=4), room_count=1)
        token = BookingAccessToken.issue_for(reservation)
        self.assertIsNotNone(BookingAccessToken.validate("BKTOK0001", token))
        # 撤销后立即失效
        reservation.access_token.revoke()
        self.assertIsNone(BookingAccessToken.validate("BKTOK0001", token))
        # 跨订单拒绝
        token2 = BookingAccessToken.issue_for(reservation)
        self.assertIsNone(BookingAccessToken.validate("BKOTHER01", token2))

    def test_rate_limit_counter_atomic_and_cleanable(self):
        from django.test import RequestFactory
        from apps.core.models import RateLimitCounter
        from apps.core import rate_limit
        factory = RequestFactory()
        probe = factory.post("/api/v1/reservations/x", REMOTE_ADDR="10.1.1.9")
        for _ in range(5):
            rate_limit.check(probe, "public.reservation_query", booking_no="BKATOM0001")
        counter = RateLimitCounter.objects.filter(action="public.reservation_query").first()
        self.assertIsNotNone(counter)
        self.assertEqual(counter.count, 5, "原子计数必须精确")
        deleted = rate_limit.cleanup_expired()
        self.assertGreaterEqual(deleted, 0)

    def test_empty_phone_does_not_create_shared_bucket(self):
        from apps.core.models import RateLimitCounter
        # 空手机号请求不产生 phone 维度共享桶（只按 IP 限）
        self.client.post("/api/v1/reservations", data=json.dumps(
            {"full_name": "无手机号", "phone": "", "room_type_id": self.room_type.pk,
             "check_in": (timezone.localdate() + timedelta(days=3)).isoformat(),
             "check_out": (timezone.localdate() + timedelta(days=5)).isoformat(),
             "rooms": 1, "adults": 1, "children": 0}), content_type="application/json")
        self.assertFalse(RateLimitCounter.objects.filter(
            key_hash="", action="public.reservation_create").exists(),
            "空值不得形成共享全局桶")
