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
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from threading import Barrier, Thread

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.billing.models import Folio, Payment, PaymentIntent
from apps.billing.services_v2 import BillingService
from apps.guests.models import Guest
from apps.inventory.models import Room, RoomTypeInventory
from apps.reservations.models import Reservation, Stay
from apps.reservations.walkin import create_walk_in

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


class WalkInsDepositPolicyTests(WalkInApiTestBase):
    """R1-02：押金为 DEC-DEP-01 强制政策——客户端不传/传零额不得跳过押金。

    传入值仅作展示核对（record_deposit client_display_amount 不一致记
    warning），服务端一律按 Reservation.total_amount×30% 强制计收
    （WB-030）。免押金如未来成为业务选项必须走显式授权路径，不得通过
    省略字段隐式免除。"""

    url = "/api/v1/front-desk/walk-ins"

    def _deposits(self):
        return Payment.objects.filter(
            reservation__source="WALK_IN", provider="frontdesk_deposit"
        )

    def test_deposit_collected_when_client_omits_amount(self):
        """不传 deposit_amount：服务端仍按 300×30%=90 强制收押。"""
        user = self._auth()
        # CASH 押金要求收款人先开立 OPEN 收银班次（record_deposit 服务端约束）。
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        response = _post_json(
            self.client, self.url, self._payload(), HTTP_IDEMPOTENCY_KEY="r102-omit"
        )
        self.assertEqual(response.status_code, 201, response.content)
        reservation = Reservation.objects.get(booking_no=response.json()["data"]["booking_no"])
        self.assertEqual(reservation.deposit_amount, Decimal("90.00"))
        self.assertEqual(self._deposits().count(), 1)
        self.assertEqual(self._deposits().first().amount, Decimal("90.00"))

    def test_client_zero_deposit_does_not_gate_collection(self):
        """客户端传 0：不得拒单，服务端照常强制计收 30%（R1-02 整改点——
        客户端金额不能控制是否收取）。"""
        user = self._auth()
        BillingService.open_shift(
            property_obj=self.property, cashier=user, opening_cash=Decimal("0.00")
        )
        response = _post_json(
            self.client,
            self.url,
            self._payload(deposit_amount="0"),
            HTTP_IDEMPOTENCY_KEY="r102-zero",
        )
        self.assertEqual(response.status_code, 201, response.content)
        reservation = Reservation.objects.get(booking_no=response.json()["data"]["booking_no"])
        self.assertEqual(reservation.deposit_amount, Decimal("90.00"))
        self.assertEqual(self._deposits().count(), 1)

    def test_client_invalid_deposit_format_returns_400(self):
        """押金展示值无法解析为金额 → 400 VALIDATION_ERROR，不产生任何押金。"""
        self._auth()
        response = _post_json(
            self.client,
            self.url,
            self._payload(deposit_amount="abc"),
            HTTP_IDEMPOTENCY_KEY="r102-format",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"]["code"], "VALIDATION_ERROR")
        self.assertEqual(self._deposits().count(), 0)


@override_settings(DEBUG=True)
@unittest.skipUnless(
    connection.vendor == "mysql",
    "真实并发幂等验证需 MySQL 行锁语义；SQLite 的 select_for_update 为 no-op "
    "且写串行，不能替代（本仓库并发测试既定纪律，与 night_audit/WB-106 同法）。",
)
class WalkInsConcurrentIdempotencyTests(TransactionTestCase):
    """R1-01：真实 MySQL 双连接并发同键 Walk-in。

    服务层 (property, idempotency_key) 唯一约束 + create_reservation 幂等
    仲裁保证并发窗口内恰有一个 created=True 写入者，另一个经仲裁得到
    created=False 重放语义；不产生重复 Reservation/Guest/Stay/押金。API 层
    200/201 映射直接取该仲裁值（本测试锁定服务层事实源）。"""

    reset_sequences = True

    def setUp(self):
        from apps.core.models import Property
        from apps.inventory.models import DailyRate, RatePlan

        self.property, _ = Property.objects.get_or_create(
            code="R101-PROP",
            defaults={
                "name": "并发幂等测试酒店",
                "timezone": "Asia/Shanghai",
                "currency": "CNY",
                "status": "ACTIVE",
            },
        )
        today = timezone.localdate()
        self.room_type = RoomType.objects.create(
            property=self.property, code="R101-KING", name="并发散客房型", capacity=2
        )
        self.rate_plan = RatePlan.objects.create(
            property=self.property, code="R101-BAR", name="并发散客门市价", valid_from=today
        )
        Room.objects.create(
            property=self.property,
            room_type=self.room_type,
            number="R101-8101",
            floor="8",
            occupancy_status=Room.OccupancyStatus.VACANT,
            housekeeping_status=Room.HousekeepingStatus.CLEAN,
            maintenance_status=Room.MaintenanceStatus.NORMAL,
        )
        DailyRate.objects.create(
            property=self.property,
            room_type=self.room_type,
            rate_plan=self.rate_plan,
            stay_date=today,
            price=Decimal("300.00"),
        )
        RoomTypeInventory.objects.create(
            property=self.property, room_type=self.room_type, stay_date=today, total_rooms=3
        )
        self.cashier = User.objects.create_user(
            username="r101-cashier", password="r101-pass123"
        )
        BillingService.open_shift(
            property_obj=self.property, cashier=self.cashier, opening_cash=Decimal("0.00")
        )
        self.key = "r101-concurrent-walkin-key"

    def test_parallel_same_key_exactly_one_creator(self):
        barrier = Barrier(2)
        outcomes = {"created": [], "replayed": [], "errors": []}

        def call_once(index):
            close_old_connections()
            try:
                property_obj = Property.objects.get(pk=self.property.pk)
                cashier = User.objects.get(username="r101-cashier")
                barrier.wait(timeout=10)
                try:
                    result = create_walk_in(
                        property_obj=property_obj,
                        full_name=f"并发散客{index}",
                        phone="",
                        room_type_id=self.room_type.pk,
                        rate_plan_id=self.rate_plan.pk,
                        nights=1,
                        created_by=cashier,
                        idempotency_key=self.key,
                    )
                    bucket = "created" if result["created"] else "replayed"
                    outcomes[bucket].append(index)
                except Exception as exc:  # noqa: BLE001  并发异常必须显式登记
                    outcomes["errors"].append(f"{index}: {type(exc).__name__}: {exc}")
            finally:
                close_old_connections()

        threads = [Thread(target=call_once, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(outcomes["errors"], [], "并发 Walk-in 不得出现任何异常")
        self.assertEqual(len(outcomes["created"]), 1, "恰有一个 created=True 写入者")
        self.assertEqual(
            len(outcomes["replayed"]), 1, "另一个必须得到 created=False 重放仲裁"
        )
        self.assertEqual(Reservation.objects.filter(source="WALK_IN").count(), 1, "Reservation=1")
        self.assertEqual(
            Guest.objects.filter(full_name__startswith="并发散客").count(), 1,
            "Guest=1（同键同占位号不得重复建档）",
        )
        self.assertEqual(Stay.objects.filter(property=self.property).count(), 1, "Stay=1")
        self.assertEqual(
            Payment.objects.filter(
                reservation__source="WALK_IN", provider="frontdesk_deposit"
            ).count(),
            1,
            "重复押金记录=0（幂等键 r101-concurrent-walkin-key-dep 恰一笔）",
        )
