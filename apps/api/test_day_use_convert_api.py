"""TASK-03 Day-use 转过夜前台操作入口（V3.0 任务书第一批）API 测试。

覆盖 POST /front-desk/reservation-rooms/{id}/convert-to-overnight 端点契约
（业务事实源 apps.reservations.day_use.convert_day_use_to_overnight，DU-11）：

* 转换成功：计费类型→NIGHT_STAY、明细离店日推进、新增住宿日库存 sold+1、
  审计落库（reservations.day_use_converted_to_overnight）、重复提交 409；
* 仅 day-use 可转：过夜订单反向转换 409（提前离店走政策链路）；
* fail-closed：缺任一晚房价快照 409（WB-075 同口径，不静默 0 元）；
* 入参校验：理由必填 1-240、日期格式 400；无 operate 权限 403。
"""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import DayUsePolicy
from apps.core.models import AuditEvent
from apps.inventory.models import DailyRate, RoomTypeInventory
from apps.reservations.models import Reservation

from .test_walk_in_api import WalkInApiTestBase, _post_json


class DayUseConvertOvernightApiTests(WalkInApiTestBase):
    """POST /api/v1/front-desk/reservation-rooms/{id}/convert-to-overnight。"""

    def _create_day_use_walk_in(self, key="du-conv-base"):
        """建单夹具：政策价日租 walk-in（服务端全链：排房+入住+押金）。"""
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
            self._payload(billing_type="DAY_USE", nights=3),
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(response.status_code, 201, response.content)
        reservation = Reservation.objects.get(booking_no=response.json()["data"]["booking_no"])
        return reservation

    def _convert(self, line_id, new_check_out, reason="客人要求续住过夜", **extra):
        payload = {"new_check_out": new_check_out, "reason": reason}
        payload.update(extra)
        return _post_json(
            self.client,
            f"/api/v1/front-desk/reservation-rooms/{line_id}/convert-to-overnight",
            payload,
            HTTP_IDEMPOTENCY_KEY="du-conv-req",
        )

    def test_convert_success_inventory_audit_and_repeat_rejected(self):
        self._auth()
        reservation = self._create_day_use_walk_in()
        line = reservation.reservation_rooms.first()  # type: ignore[attr-defined]
        today = line.check_in
        # 新增住宿日 [check_in+1, new_check_out) 的过夜房价快照与库存日历行
        DailyRate.objects.create(
            property=self.property,
            rate_plan=self.rate_plan,
            room_type=self.room_type,
            stay_date=today + timedelta(days=1),
            price=Decimal("300.00"),
        )
        RoomTypeInventory.objects.create(
            property=self.property,
            room_type=self.room_type,
            stay_date=today + timedelta(days=1),
            total_rooms=3,
        )
        tomorrow_inv = RoomTypeInventory.objects.get(
            property=self.property,
            room_type=self.room_type,
            stay_date=today + timedelta(days=1),
        )
        sold_before = tomorrow_inv.sold

        response = self._convert(line.pk, (today + timedelta(days=2)).isoformat())
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["billing_type"], "NIGHT_STAY")
        self.assertEqual(data["check_out"], (today + timedelta(days=2)).isoformat())
        # 总额 = 原日租政策价 288 + 夜租快照整段重算（入住日+新增日各 300）
        self.assertEqual(Decimal(data["total_amount"]), Decimal("888.00"))

        reservation.refresh_from_db()
        self.assertEqual(reservation.billing_type, Reservation.BillingType.NIGHT_STAY)
        line.refresh_from_db()
        self.assertEqual(line.check_out, today + timedelta(days=2))
        # 新增住宿日库存 sold+1（首日为 day-use 占用不重复计）
        tomorrow_inv.refresh_from_db()
        self.assertEqual(tomorrow_inv.sold, sold_before + 1)
        # 审计：动作、理由、前后状态
        audit = AuditEvent.objects.filter(
            action="reservations.day_use_converted_to_overnight",
            entity_id=str(line.pk),
        ).first()
        self.assertIsNotNone(audit)
        assert audit is not None  # mypy 收窄
        self.assertEqual(audit.reason, "客人要求续住过夜")
        self.assertEqual(audit.before_json["billing_type"], "DAY_USE")
        self.assertEqual(audit.after_json["billing_type"], "NIGHT_STAY")

        # 重复提交：计费类型已是 NIGHT_STAY → 409 拒绝
        repeat = self._convert(line.pk, (today + timedelta(days=3)).isoformat())
        self.assertEqual(repeat.status_code, 409, repeat.content)
        self.assertIn("仅 day-use", repeat.json()["error"]["message"])

    def test_convert_rejects_overnight_order(self):
        self._auth()
        response = _post_json(
            self.client,
            "/api/v1/front-desk/walk-ins",
            self._payload(),
            HTTP_IDEMPOTENCY_KEY="ns-conv-base",
        )
        self.assertEqual(response.status_code, 201, response.content)
        reservation = Reservation.objects.get(booking_no=response.json()["data"]["booking_no"])
        line = reservation.reservation_rooms.first()  # type: ignore[attr-defined]
        today = timezone.localdate()
        blocked = self._convert(line.pk, (today + timedelta(days=2)).isoformat())
        self.assertEqual(blocked.status_code, 409, blocked.content)
        self.assertIn("仅 day-use", blocked.json()["error"]["message"])

    def test_convert_fail_closed_without_nightly_rate(self):
        self._auth()
        reservation = self._create_day_use_walk_in(key="du-conv-norate")
        line = reservation.reservation_rooms.first()  # type: ignore[attr-defined]
        today = line.check_in
        # 未创建明晚 DailyRate：缺快照必须整体拒绝
        blocked = self._convert(line.pk, (today + timedelta(days=2)).isoformat())
        self.assertEqual(blocked.status_code, 409, blocked.content)
        self.assertIn("房价快照", blocked.json()["error"]["message"])
        # 原子回滚：计费类型与离店日不变
        reservation.refresh_from_db()
        self.assertEqual(reservation.billing_type, Reservation.BillingType.DAY_USE)

    def test_convert_input_validation(self):
        self._auth()
        reservation = self._create_day_use_walk_in(key="du-conv-input")
        line = reservation.reservation_rooms.first()  # type: ignore[attr-defined]
        today = line.check_in
        future = (today + timedelta(days=2)).isoformat()
        # 理由必填
        empty_reason = self._convert(line.pk, future, reason="")
        self.assertEqual(empty_reason.status_code, 400, empty_reason.content)
        # 理由超长
        long_reason = self._convert(line.pk, future, reason="长" * 241)
        self.assertEqual(long_reason.status_code, 400, long_reason.content)
        # 日期格式
        bad_date = self._convert(line.pk, "2026/11/23")
        self.assertEqual(bad_date.status_code, 400, bad_date.content)
        # 离店日必须晚于今天
        stale = self._convert(line.pk, today.isoformat())
        self.assertEqual(stale.status_code, 409, stale.content)

    def test_convert_requires_operate_permission(self):
        self._auth()  # 授权岗建单
        reservation = self._create_day_use_walk_in(key="du-conv-perm")
        line = reservation.reservation_rooms.first()  # type: ignore[attr-defined]
        today = line.check_in
        # 切换只读岗（仅 view_accommodation）：转换必须 403
        readonly = self._make_staff(codenames={"view_accommodation"})
        self.client.force_login(readonly)
        response = self._convert(line.pk, (today + timedelta(days=2)).isoformat())
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")
