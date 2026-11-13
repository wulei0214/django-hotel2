"""A1 Walk-in 散客（/front-desk/walk-ins）+ A2 支付窗口延长 API 端点测试。

只断言 API 层行为契约（业务细节由服务层白盒测试覆盖）：

A1（POST /api/v1/front-desk/walk-ins，A1 契约口径）：
  * 复用 create_walk_in 原子链路；押金由服务端按总额 30% 强制计得（DEC-DEP-01）；
  * 业务冲突（无净房/幂等指纹冲突）→ 409 BUSINESS_CONFLICT 消息透传；
  * 幂等重放 → 200 + created=false（服务层 (property, idempotency_key) + 指纹仲裁）；
  * 入参结构错误 → 400；无写权限 → 403 PERMISSION_DENIED。

A2（POST /api/v1/billing/payment-intents/{intent_id}/window-extension）：
  * 复用 extend_intent_window（D2 服务端策略，mock 渠道 max_extensions=1）；
  * 意图不存在/跨物业 → 404；终态/超上限 → 409 消息透传；
  * 无 finance_access → 403 PERMISSION_DENIED。
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import Folio, PaymentIntent
from apps.billing.services_v2 import BillingService
from apps.guests.models import Guest
from apps.inventory.models import Room
from apps.reservations.models import Reservation, Stay

from .test_walk_in_api import WalkInApiTestBase, _post_json
from .test_whitebox_api import WhiteboxApiTestBase


class WalkInsCreateApiTests(WalkInApiTestBase):
    """A1：POST /api/v1/front-desk/walk-ins（契约口径端点）。"""

    url = "/api/v1/front-desk/walk-ins"

    # ------------------------------------------------------------------
    # 任务验收点①：正常创建 201 + 服务端 30% 押金 + 房态（占用 OCCUPIED；
    # DIRTY 在本仓库属退房清洁生命周期，入住不置 DIRTY——见下方断言注释）
    # ------------------------------------------------------------------
    def test_walk_ins_create_returns_201_with_server_deposit_and_occupied_room(self):
        user = self._auth()
        # CASH 押金登记要求收款人先开立 OPEN 收银班次（record_deposit 服务端约束）。
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00"))
        response = _post_json(
            self.client, self.url,
            # 客户端传 999.00 仅用于展示核对；服务端必须按 300.00×30%=90.00 强制计收
            self._payload(deposit_amount="999.00"),
            HTTP_IDEMPOTENCY_KEY="a1-create-001")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertIn("meta", body)
        self.assertEqual(body["meta"]["source_system"], "PMS2")
        data = body["data"]
        self.assertEqual(data["source"], "WALK_IN")
        self.assertTrue(data["created"])
        self.assertTrue(data["booking_no"])
        self.assertEqual(data["deposit_amount"], "90.00")

        reservation = Reservation.objects.get(booking_no=data["booking_no"])
        self.assertEqual(reservation.source, "WALK_IN")
        self.assertTrue(
            Stay.objects.filter(reservation_room__reservation=reservation).exists())
        # 入住后房态：占用置 OCCUPIED；housekeeping 保持 CLEAN（本仓库
        # 房态生命周期中 DIRTY 在退房时置入并联动 WB-055 离店清洁任务，
        # 入住不产生待清洁房态——见 front_desk_services.check_in_stay）。
        self.room.refresh_from_db()
        self.assertEqual(self.room.occupancy_status, Room.OccupancyStatus.OCCUPIED)
        self.assertEqual(self.room.housekeeping_status, Room.HousekeepingStatus.CLEAN)
        # 押金落库值同样为服务端计得的 90.00（客户端传入的 999.00 不生效）
        self.assertEqual(reservation.deposit_amount, Decimal("90.00"))

    # ------------------------------------------------------------------
    # 任务验收点②：同 Idempotency-Key 重放 → 200 + created=false，数据一致
    # ------------------------------------------------------------------
    def test_walk_ins_idempotent_replay_returns_200_created_false(self):
        user = self._auth()
        # R1-02：押金强制化后，成功创建必须先开立本人 OPEN 收银班次。
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        kw = {"HTTP_IDEMPOTENCY_KEY": "a1-replay-001"}
        first = _post_json(self.client, self.url, self._payload(), **kw)
        second = _post_json(self.client, self.url, self._payload(), **kw)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        first_data = first.json()["data"]
        second_data = second.json()["data"]
        self.assertTrue(first_data["created"])
        self.assertFalse(second_data["created"])
        for field in ("booking_no", "reservation_id", "stay_id", "room_number",
                      "deposit_amount", "source"):
            self.assertEqual(first_data[field], second_data[field], field)
        # 幂等重放不重复建档
        self.assertEqual(
            Reservation.objects.filter(booking_no=first_data["booking_no"]).count(), 1)
        self.assertEqual(Guest.objects.filter(full_name="散客张三").count(), 1)
        self.assertEqual(Stay.objects.count(), 1)

    # ------------------------------------------------------------------
    # 任务验收点③：缺幂等键 → 400
    # ------------------------------------------------------------------
    def test_walk_ins_missing_idempotency_key_returns_400(self):
        self._auth()
        response = _post_json(self.client, self.url, self._payload())
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_walk_ins_overlong_idempotency_key_returns_400(self):
        self._auth()
        response = _post_json(
            self.client, self.url, self._payload(),
            HTTP_IDEMPOTENCY_KEY="k" * 129)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    # ------------------------------------------------------------------
    # 任务验收点④：无写权限 → 403 PERMISSION_DENIED
    # ------------------------------------------------------------------
    def test_walk_ins_without_change_permission_returns_403(self):
        self._auth(codenames=("view_accommodation",))
        response = _post_json(
            self.client, self.url, self._payload(),
            HTTP_IDEMPOTENCY_KEY="a1-noperm-001")
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_walk_ins_requires_authentication_returns_401(self):
        response = _post_json(
            self.client, self.url, self._payload(),
            HTTP_IDEMPOTENCY_KEY="a1-anon-001")
        self.assertEqual(response.status_code, 401, response.content)

    # ------------------------------------------------------------------
    # 任务验收点⑤：无可入净房 → 409 BUSINESS_CONFLICT（消息透传，原子回滚）
    # ------------------------------------------------------------------
    def test_walk_ins_no_clean_room_returns_409(self):
        self.room.occupancy_status = Room.OccupancyStatus.OCCUPIED
        self.room.save(update_fields=("occupancy_status",))
        self._auth()
        response = _post_json(
            self.client, self.url, self._payload(),
            HTTP_IDEMPOTENCY_KEY="a1-novacant-001")
        self.assertEqual(response.status_code, 409, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "BUSINESS_CONFLICT")
        self.assertIn("净房", error["message"])
        # 原子回滚：无新增预订/Stay
        self.assertEqual(Reservation.objects.filter(source="WALK_IN").count(), 0)
        self.assertEqual(Stay.objects.count(), 0)

    # ------------------------------------------------------------------
    # 补充契约：同键不同内容 → 409；入参边界 → 400；房型不存在 → 400
    # ------------------------------------------------------------------
    def test_walk_ins_same_key_different_body_returns_409(self):
        user = self._auth()
        # R1-02：首建成功即收押金（强制），需先开立本人 OPEN 收银班次。
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        kw = {"HTTP_IDEMPOTENCY_KEY": "a1-fingerprint-001"}
        first = _post_json(self.client, self.url, self._payload(nights=1), **kw)
        second = _post_json(self.client, self.url, self._payload(nights=2), **kw)
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 409, second.content)
        self.assertEqual(second.json()["error"]["code"], "BUSINESS_CONFLICT")

    def test_walk_ins_nights_out_of_range_returns_400(self):
        self._auth()
        for nights in (0, 31):
            response = _post_json(
                self.client, self.url, self._payload(nights=nights),
                HTTP_IDEMPOTENCY_KEY=f"a1-nights-{nights}")
            self.assertEqual(response.status_code, 400, response.content)
            self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_walk_ins_missing_rate_plan_returns_400(self):
        """rate_plan_id 为 A1 契约必填字段。"""
        payload = self._payload()
        del payload["rate_plan_id"]
        self._auth()
        response = _post_json(
            self.client, self.url, payload, HTTP_IDEMPOTENCY_KEY="a1-noplan-001")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_walk_ins_unknown_room_type_returns_400(self):
        self._auth()
        response = _post_json(
            self.client, self.url, self._payload(room_type_id=999999),
            HTTP_IDEMPOTENCY_KEY="a1-badtype-001")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")


class PaymentIntentWindowExtensionApiTests(WhiteboxApiTestBase):
    """A2：POST /api/v1/billing/payment-intents/{intent_id}/window-extension。"""

    url_template = "/api/v1/billing/payment-intents/{pk}/window-extension"

    def setUp(self):
        super().setUp()
        today = timezone.localdate()
        self.guest = Guest.objects.create(property=self.property, full_name="支付窗口住客")
        self.reservation = Reservation.objects.create(
            property=self.property, guest=self.guest, booking_no="A2INTENT-001",
            status=Reservation.Status.PENDING,
            check_in=today, check_out=today + timedelta(days=1),
            room_count=1, adult_count=1,
            total_amount=Decimal("600.00"), currency="CNY")
        self.folio = Folio.objects.create(
            property=self.property, reservation=self.reservation,
            guest=self.guest, currency="CNY")
        self.intent = PaymentIntent.objects.create(
            property=self.property, reservation=self.reservation, folio=self.folio,
            intent_no="A2-INTENT-001", idempotency_key="A2-INTENT-KEY-001",
            amount=Decimal("600.00"), currency="CNY",
            status=PaymentIntent.Status.PENDING,
            expires_at=timezone.now() + timedelta(minutes=15),
            max_expires_at=timezone.now() + timedelta(minutes=30))

    def _finance_auth(self):
        user = self._make_staff(codenames={"finance_access"})
        self.client.force_login(user)
        return user

    def _post(self, intent=None):
        intent = intent or self.intent
        return self.client.post(
            self.url_template.format(pk=intent.pk),
            data=json.dumps({}), content_type="application/json")

    # ------------------------------------------------------------------
    # 任务验收点①：PENDING 意图延长成功 extension_count=1、expires_at 后移
    # ------------------------------------------------------------------
    def test_pending_intent_extension_succeeds_once(self):
        self._finance_auth()
        before = self.intent.expires_at
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIn("meta", body)
        data = body["data"]
        self.assertEqual(data["intent_id"], self.intent.pk)
        self.assertEqual(data["status"], PaymentIntent.Status.PENDING)
        self.assertEqual(data["extension_count"], 1)
        self.assertEqual(data["max_extensions"], 1)
        self.assertEqual(
            datetime.fromisoformat(data["expires_at"]),
            before + timedelta(seconds=300))
        window = data["channel_window"]
        self.assertEqual(window["provider_code"], "mock")
        self.assertEqual(window["max_extensions"], 1)
        self.assertEqual(window["extension_seconds"], 300)
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.extension_count, 1)
        self.assertEqual(self.intent.expires_at, before + timedelta(seconds=300))

    # ------------------------------------------------------------------
    # 任务验收点②：二次延长 → 409（mock 渠道 max_extensions=1）
    # ------------------------------------------------------------------
    def test_second_extension_returns_409(self):
        self._finance_auth()
        first = self._post()
        self.assertEqual(first.status_code, 200, first.content)
        second = self._post()
        self.assertEqual(second.status_code, 409, second.content)
        error = second.json()["error"]
        self.assertEqual(error["code"], "BUSINESS_CONFLICT")
        self.assertIn("最大延长次数", error["message"])
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.extension_count, 1)

    # ------------------------------------------------------------------
    # 任务验收点③：终态意图 → 409
    # ------------------------------------------------------------------
    def test_terminal_intent_extension_returns_409(self):
        self.intent.status = PaymentIntent.Status.FAILED
        self.intent.save(update_fields=("status", "updated_at"))
        self._finance_auth()
        response = self._post()
        self.assertEqual(response.status_code, 409, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "BUSINESS_CONFLICT")
        self.assertIn("待支付/处理中", error["message"])

    # ------------------------------------------------------------------
    # 任务验收点④：无 finance_access → 403
    # ------------------------------------------------------------------
    def test_without_finance_permission_returns_403(self):
        user = self._make_staff(codenames={"view_accommodation"})
        self.client.force_login(user)
        response = self._post()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_anonymous_returns_401(self):
        response = self._post()
        self.assertEqual(response.status_code, 401, response.content)

    # ------------------------------------------------------------------
    # 任务验收点⑤：跨物业意图 → 404 RESOURCE_NOT_FOUND
    # ------------------------------------------------------------------
    def test_cross_property_intent_returns_404(self):
        other_guest = Guest.objects.create(
            property=self.other_property, full_name="别店支付住客")
        other_reservation = Reservation.objects.create(
            property=self.other_property, guest=other_guest,
            booking_no="A2INTENT-OTHER",
            status=Reservation.Status.PENDING,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, adult_count=1,
            total_amount=Decimal("500.00"), currency="CNY")
        other_folio = Folio.objects.create(
            property=self.other_property, reservation=other_reservation,
            guest=other_guest, currency="CNY")
        other_intent = PaymentIntent.objects.create(
            property=self.other_property, reservation=other_reservation,
            folio=other_folio,
            intent_no="A2-INTENT-OTHER", idempotency_key="A2-INTENT-KEY-OTHER",
            amount=Decimal("500.00"), currency="CNY",
            status=PaymentIntent.Status.PENDING,
            expires_at=timezone.now() + timedelta(minutes=15))
        self._finance_auth()
        response = self._post(intent=other_intent)
        self.assertEqual(response.status_code, 404, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "RESOURCE_NOT_FOUND")
        other_intent.refresh_from_db()
        self.assertEqual(other_intent.extension_count, 0)
