"""TASK-02 Day-use 前台建单入口（V3.0 任务书第一批）API 测试。

覆盖 /front-desk/walk-in-options（建单表单数据源）与 POST /front-desk/walk-ins
billing_type=DAY_USE 的端点级契约：

* 选项端点：启用房型 + 当日有效价格计划 + 逐房型日租政策价（fail-closed：
  无覆盖当日政策的房型不透出 day_use_policies 条目）；
* 权限：无 view_accommodation → 403；未登录 → 401；
* 日租建单：POST walk-ins（billing_type=DAY_USE）→ 201，房费=政策价
  （fail-closed：无政策房型 → 409，消息指向日租政策维护）；
* 过夜默认：不带 billing_type → NIGHT_STAY，离店日=入住日+晚数。
"""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.billing.models import DayUsePolicy, LedgerEntry
from apps.inventory.models import RoomType
from apps.reservations.models import Reservation

from .test_walk_in_api import WalkInApiTestBase, _post_json


class WalkInOptionsApiTests(WalkInApiTestBase):
    """GET /api/v1/front-desk/walk-in-options：建单表单数据源（只读）。"""

    def _get(self):
        return self.client.get("/api/v1/front-desk/walk-in-options")

    def test_options_lists_room_types_rate_plans_and_dayuse_policy(self):
        self._auth()
        today = timezone.localdate()
        DayUsePolicy.objects.create(
            property=self.property,
            room_type=self.room_type,
            day_use_price=Decimal("288.00"),
            start_time="08:00",
            end_time="18:00",
            overtime_hourly_rate=Decimal("50.00"),
            valid_from=today,
            valid_to=today + timedelta(days=30),
        )
        response = self._get()
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        type_ids = [row["id"] for row in data["room_types"]]
        self.assertIn(self.room_type.pk, type_ids)
        plan_ids = [row["id"] for row in data["rate_plans"]]
        self.assertIn(self.rate_plan.pk, plan_ids)
        policy_rows = [row for row in data["day_use_policies"]
                       if row["room_type_id"] == self.room_type.pk]
        self.assertEqual(len(policy_rows), 1)
        self.assertEqual(policy_rows[0]["price"], "288.00")
        self.assertEqual(policy_rows[0]["start_time"], "08:00")
        self.assertEqual(policy_rows[0]["end_time"], "18:00")
        self.assertEqual(policy_rows[0]["overtime_hourly_rate"], "50.00")
        # business_date = 物业营业日
        self.assertEqual(data["business_date"], today.isoformat())

    def test_options_fail_closed_without_active_policy(self):
        """无覆盖当日的启用政策 → 该房型不进 day_use_policies（不透出价格）。"""
        self._auth()
        response = self._get()
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        policy_rows = [row for row in data["day_use_policies"]
                       if row["room_type_id"] == self.room_type.pk]
        self.assertEqual(policy_rows, [])

    def test_options_requires_view_accommodation(self):
        self._auth(codenames=("view_accommodation",))  # 只读岗可见
        self.assertEqual(self._get().status_code, 200)
        self._auth(codenames=("view_accommodation", "change_accommodation"))
        self.assertEqual(self._get().status_code, 200)
        # 无任何入住权限的任职员工 → 403
        staff = self._make_staff()
        self.client.force_login(staff)
        response = self._get()
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_options_requires_authentication(self):
        response = self._get()
        self.assertEqual(response.status_code, 401, response.content)


class DayUseWalkInCreateApiTests(WalkInApiTestBase):
    """POST /front-desk/walk-ins billing_type=DAY_USE：政策价建单（端点级）。"""

    def _day_use_payload(self, **overrides):
        payload = self._payload(billing_type="DAY_USE", nights=3)
        payload.update(overrides)
        return payload

    def test_day_use_walk_in_uses_policy_price_and_fails_closed_without_policy(self):
        self._auth()  # 登录 + 预开收银班次（押金登记前置条件）
        today = timezone.localdate()
        DayUsePolicy.objects.create(
            property=self.property,
            room_type=self.room_type,
            day_use_price=Decimal("288.00"),
            start_time="08:00",
            end_time="18:00",
            overtime_hourly_rate=Decimal("50.00"),
            valid_from=today,
            valid_to=today + timedelta(days=30),
        )
        response = _post_json(
            self.client,
            "/api/v1/front-desk/walk-ins",
            self._day_use_payload(),
            HTTP_IDEMPOTENCY_KEY="du-create-001",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()["data"]
        reservation = Reservation.objects.get(booking_no=data["booking_no"])
        self.assertEqual(reservation.billing_type, Reservation.BillingType.DAY_USE)
        self.assertEqual(reservation.total_amount, Decimal("288.00"))
        # 日租离店日=入住日
        self.assertEqual(reservation.reservation_rooms.first().check_out,
                         reservation.reservation_rooms.first().check_in)
        # 房费凭证=政策价一次性入账
        charge = LedgerEntry.objects.filter(
            folio__reservation=reservation, entry_type="ROOM_CHARGE",
        ).aggregate(total=Sum("amount"))["total"]
        self.assertEqual(charge, Decimal("288.00"))
        # 无政策房型 → 409 fail-closed（政策解析先于净房检查）
        no_policy_type = RoomType.objects.create(
            property=self.property, code="WBI-NOPOLICY", name="无日租政策房型", capacity=2
        )
        second = _post_json(
            self.client,
            "/api/v1/front-desk/walk-ins",
            self._day_use_payload(room_type_id=no_policy_type.pk),
            HTTP_IDEMPOTENCY_KEY="du-create-002",
        )
        self.assertEqual(second.status_code, 409, second.content)
        self.assertEqual(second.json()["error"]["code"], "BUSINESS_CONFLICT")
        self.assertIn("日租政策", second.json()["error"]["message"])
        # 库存与订单计数：首次成功建单已占用；失败请求零写入（原子回滚）
        self.assertEqual(
            Reservation.objects.filter(billing_type=Reservation.BillingType.DAY_USE).count(), 1
        )

    def test_night_stay_default_unchanged(self):
        """不带 billing_type 仍按 NIGHT_STAY：离店日=入住日+晚数。"""
        self._auth()
        response = _post_json(
            self.client,
            "/api/v1/front-desk/walk-ins",
            self._payload(),
            HTTP_IDEMPOTENCY_KEY="ns-default-001",
        )
        self.assertEqual(response.status_code, 201, response.content)
        reservation = Reservation.objects.get(
            booking_no=response.json()["data"]["booking_no"]
        )
        self.assertEqual(reservation.billing_type, Reservation.BillingType.NIGHT_STAY)
        line = reservation.reservation_rooms.first()
        self.assertEqual(line.check_out, line.check_in + timedelta(days=1))
