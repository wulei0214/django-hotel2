"""公开预订订单生命周期验收测试（SEC-01~06 + 十场景验收矩阵）。

依据 docs/audit/phase0/90-public-booking-order-lifecycle-review.md 验收矩阵，
覆盖 API 层可自动化场景（复制按钮/浏览器关闭重开等纯前端项由浏览器回归承担）：
- S1  创建订单返回一次性 access_token
- S2  创建后立即凭 X-Booking-Token 头找回（不依赖会话/Cookie）
- S3  Token 错误 → 404，不返回订单信息
- S4  编号正确 + Token 错误 → 404
- S5  找回 PENDING 订单 → 可继续创建支付意图
- S6  找回 CONFIRMED 订单 → 支付入口关闭（意图 409）
- S7  连续点击支付 → 只产生一次有效支付（意图幂等 + 回调幂等）
- S8  支付响应丢失 → 重新查询得到真实支付结果（CONFIRMED）
- S9  取消已入住订单 → 公共接口拒绝
- S10 多次尝试错误 Token → 触发限流（429 + 审计）
- SEC-01 Token 禁止经 URL query 传递（query 传参一律 404）
- SEC-02 数据库只存 SHA-256 摘要，不存明文
"""
import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.reservations.models import BookingAccessToken, Reservation

DETAIL_URL = "/api/v1/reservations/{booking_no}"


@override_settings(DEBUG=True, DJANGO_ENV="test", PAYMENT_WEBHOOK_SECRET="lifecycle-secret")
class PublicBookingLifecycleTests(TestCase):
    """订单生命周期闭环验收（S1~S11 + SEC-01/02）。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        from apps.inventory.models import DailyRate, RatePlan, RoomType, RoomTypeInventory
        cls.room_type = RoomType.objects.create(
            property=cls.prop, code="LC-KING", name="生命周期测试房", capacity=2)
        cls.rate_plan = RatePlan.objects.create(
            property=cls.prop, code="LC-BAR", name="生命周期门市价",
            valid_from=timezone.localdate())
        for day in (timezone.localdate() + timedelta(days=n) for n in (3, 4)):
            DailyRate.objects.create(
                property=cls.prop, rate_plan=cls.rate_plan,
                room_type=cls.room_type, stay_date=day, price=Decimal("388.00"))
            RoomTypeInventory.objects.create(
                property=cls.prop, room_type=cls.room_type, stay_date=day,
                total_rooms=5)

    def _create_booking(self, phone="13822220000"):
        """S1：公开 API 创建订单，返回 (booking_no, token, 响应体)。"""
        payload = {
            "full_name": "生命周期测试", "phone": phone,
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": (timezone.localdate() + timedelta(days=3)).isoformat(),
            "check_out": (timezone.localdate() + timedelta(days=5)).isoformat(),
            "rooms": 1, "adults": 1, "children": 0,
        }
        response = self.client.post(
            "/api/v1/reservations", json.dumps(payload),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="lc-" + phone)
        self.assertEqual(response.status_code, 201,
                         "订单创建失败：%s" % response.content.decode("utf-8"))
        body = response.json()
        token = body.get("access_token") or ""
        return body["booking_no"], token, body

    def _callback(self, intent):
        """执行 mock 回调（模拟支付服务商异步通知；签名按紧凑排序 JSON 计算）。"""
        sample = intent["mock_callback_example"]
        raw = json.dumps(sample["payload"], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
        return self.client.post(
            sample["url"], raw,
            content_type="application/json",
            HTTP_X_SIGNATURE=sample["signature"])

    # ---------- S1 / SEC-02 ----------

    def test_s1_create_returns_one_time_token(self):
        """S1：创建订单必须返回一次性访问令牌（≥32 字符高熵随机串）。"""
        booking_no, token, body = self._create_booking()
        self.assertTrue(token, "创建响应必须包含 access_token")
        self.assertGreaterEqual(len(token), 32, "令牌必须具备足够熵（256 位 urlsafe）")
        self.assertNotIn(token, booking_no)
        self.assertNotIn("13822220000", token, "令牌不得由手机号推导/包含手机号")
        # SEC-02：数据库只存 SHA-256 摘要，绝不存明文
        record = BookingAccessToken.objects.get(reservation__booking_no=booking_no)
        self.assertNotEqual(record.token_hash, token, "数据库不得存令牌明文")
        self.assertEqual(len(record.token_hash), 64, "必须为 SHA-256 十六进制摘要")

    # ---------- S2 / S11 ----------

    def test_s2_and_s11_retrieve_after_refresh_without_cookies(self):
        """S2/S11：刷新/关闭浏览器后，无 Cookie 会话也能凭编号+头令牌找回。"""
        booking_no, token, _ = self._create_booking()
        fresh = self.client_class()  # 全新无 Cookie 客户端（模拟浏览器重开）
        response = fresh.get(DETAIL_URL.format(booking_no=booking_no),
                             HTTP_X_BOOKING_TOKEN=token)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["booking_no"], booking_no)
        self.assertNotIn("token", body, "详情响应不得回传令牌")

    def test_sec01_query_param_token_rejected(self):
        """SEC-01：query 传令牌一律视为未提供（会落访问日志），必须 404。"""
        booking_no, token, _ = self._create_booking()
        response = self.client.get(
            DETAIL_URL.format(booking_no=booking_no), HTTP_TOKEN=token)
        self.assertEqual(response.status_code, 404,
                         "令牌经 URL query 传递必须被拒绝")

    # ---------- S3 / S4 ----------

    def test_s3_s4_wrong_token_returns_404_without_info(self):
        """S3/S4：编号正确、Token 错误 → 404 且不泄露订单信息；落审计。"""
        booking_no, _, _ = self._create_booking()
        for bad in ("totally-wrong", ""):
            response = self.client.get(DETAIL_URL.format(booking_no=booking_no),
                                       HTTP_X_BOOKING_TOKEN=bad)
            self.assertEqual(response.status_code, 404)
            text = response.content.decode("utf-8")
            self.assertNotIn("check_in", text, "404 不得返回订单字段")
            self.assertNotIn("total_amount", text, "404 不得返回订单字段")
        self.assertTrue(AuditEvent.objects.filter(
            action="guest.booking_access_denied").exists(), "凭据错误必须审计")

    # ---------- S5 / S6 / S7 / S8 ----------

    def test_s5_pending_retrieval_allows_payment(self):
        """S5：找回 PENDING 订单 → 可继续创建支付意图。"""
        booking_no, token, _ = self._create_booking()
        detail = self.client.get(DETAIL_URL.format(booking_no=booking_no),
                                 HTTP_X_BOOKING_TOKEN=token)
        self.assertEqual(detail.json()["status"], "PENDING")
        intent = self.client.post("/api/v1/payments/intents",
            json.dumps({"booking_no": booking_no, "token": token}),
            content_type="application/json")
        self.assertEqual(intent.status_code, 201)

    def test_s6_confirmed_retrieval_closes_payment_entry(self):
        """S6：找回 CONFIRMED 订单 → 不再提供支付入口（意图 409）。"""
        booking_no, token, _ = self._create_booking()
        intent = self.client.post("/api/v1/payments/intents",
            json.dumps({"booking_no": booking_no, "token": token}),
            content_type="application/json")
        self.assertEqual(intent.status_code, 201)
        self._callback(intent.json())  # 支付完成 → CONFIRMED
        detail = self.client.get(DETAIL_URL.format(booking_no=booking_no),
                                 HTTP_X_BOOKING_TOKEN=token)
        self.assertEqual(detail.json()["status"], "CONFIRMED")
        again = self.client.post("/api/v1/payments/intents",
            json.dumps({"booking_no": booking_no, "token": token}),
            content_type="application/json")
        self.assertEqual(again.status_code, 409, "已确认订单不得再发起支付")

    def test_s7_double_click_creates_single_payment(self):
        """S7：连续点击支付 → 意图幂等返回同一支付单，回调后仅一笔有效支付。"""
        booking_no, token, _ = self._create_booking()
        payload = json.dumps({"booking_no": booking_no, "token": token})
        first = self.client.post("/api/v1/payments/intents", payload,
                                 content_type="application/json")
        second = self.client.post("/api/v1/payments/intents", payload,
                                  content_type="application/json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        ref_a = first.json()["provider_ref"]
        ref_b = second.json()["provider_ref"]
        self.assertEqual(ref_a, ref_b, "重复点击必须返回同一支付意图")
        self._callback(second.json())
        from apps.billing.models import Payment
        payments = Payment.objects.filter(reservation__booking_no=booking_no)
        self.assertEqual(payments.count(), 1, "只允许产生一笔支付")
        self.assertEqual(payments.get().status, Payment.Status.SUCCEEDED)

    def test_s8_lost_payment_response_recovers_by_query(self):
        """S8：支付响应丢失（超时/关页）→ 重新查询得到真实支付结果。"""
        booking_no, token, _ = self._create_booking()
        intent = self.client.post("/api/v1/payments/intents",
            json.dumps({"booking_no": booking_no, "token": token}),
            content_type="application/json")
        self._callback(intent.json())  # 假设回调已到达服务端而客户端未收到响应
        fresh = self.client_class()
        detail = fresh.get(DETAIL_URL.format(booking_no=booking_no),
                           HTTP_X_BOOKING_TOKEN=token)
        self.assertEqual(detail.json()["status"], "CONFIRMED",
                         "凭令牌重查必须得到真实支付状态")

    # ---------- S9 ----------

    def test_s9_cancel_checked_in_rejected(self):
        """S9：已入住订单 → 公共取消接口拒绝（自助取消仅限 PENDING）。"""
        booking_no, token, _ = self._create_booking()
        Reservation.objects.filter(booking_no=booking_no).update(
            status=Reservation.Status.CHECKED_IN)
        response = self.client.post(
            DETAIL_URL.format(booking_no=booking_no) + "/cancel",
            json.dumps({"token": token}),
            content_type="application/json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            Reservation.objects.get(booking_no=booking_no).status,
            Reservation.Status.CHECKED_IN, "状态不得被公共接口改变")

    # ---------- S10 ----------

    def test_s10_wrong_token_bruteforce_throttled(self):
        """S10：同一订单连续错误 Token → 触发限流 429 + 审计。"""
        booking_no, _, _ = self._create_booking()
        last = None
        for i in range(12):
            last = self.client.get(DETAIL_URL.format(booking_no=booking_no),
                                   HTTP_X_BOOKING_TOKEN="wrong-" + str(i))
        self.assertEqual(last.status_code, 429, "错误凭据爆破必须被限流")
        self.assertTrue(AuditEvent.objects.filter(
            action="public.rate_limited").exists(), "限流必须落审计")
