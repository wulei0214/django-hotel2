"""TASK-04 前台取消 PENDING 预订入口（V3.0 任务书第一批，RES-06）API 测试。

覆盖 POST /front-desk/reservations/{booking_no}/cancel 端点契约：

* 前台取消 PENDING → CANCELLED，库存 Hold 同步释放，原因进审计；
* 重复取消幂等（changed=false）；已过期单置 EXPIRED（幂等 false）；
* 已有成功收款 → 409（服务层守卫，必须走退款三人分离流程）；
* CONFIRMED 等非 PENDING 状态 → 409；原因缺失/超长 → 400；无 operate → 403；
* booking_no 不存在 → 404。

服务层守卫（cancel_pending_reservation）为公开域自助取消共用，其行为变更
由本类端点级测试锁定。
"""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import Payment
from apps.core.models import AuditEvent
from apps.inventory.models import RoomTypeInventory
from apps.reservations.models import InventoryHold, Reservation

from .test_walk_in_api import WalkInApiTestBase, _post_json


class FrontDeskCancelApiTests(WalkInApiTestBase):
    """POST /api/v1/front-desk/reservations/{booking_no}/cancel。"""

    def _create_pending_reservation(self, key="fd-cancel-pending"):
        """公开域 PENDING 单（带 Hold）：走 create_reservation 服务，同前台场景。"""
        from apps.reservations.services import create_reservation

        today = timezone.localdate()
        payload = {
            "full_name": "取消测试住客",
            "phone": "13800001111",
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": today,
            "check_out": today + timedelta(days=1),
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        reservation, created, _token = create_reservation(
            property_obj=self.property, payload=payload, idempotency_key=key
        )
        self.assertTrue(created)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        return reservation

    def _cancel(self, booking_no, reason="客人来电取消", **extra):
        payload = {"reason": reason}
        payload.update(extra)
        return _post_json(
            self.client,
            f"/api/v1/front-desk/reservations/{booking_no}/cancel",
            payload,
            HTTP_IDEMPOTENCY_KEY="fd-cancel-req",
        )

    def test_cancel_pending_releases_hold_and_audits_reason(self):
        self._auth()
        reservation = self._create_pending_reservation()
        hold = InventoryHold.objects.filter(reservation_room__reservation=reservation).first()
        self.assertIsNotNone(hold)
        assert hold is not None  # mypy 收窄
        inv = RoomTypeInventory.objects.get(
            property=self.property, room_type=self.room_type, stay_date=reservation.check_in
        )
        held_before = inv.held

        response = self._cancel(reservation.booking_no)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertTrue(data["changed"])
        self.assertEqual(data["status"], "CANCELLED")

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        # Hold 释放：Hold 行 EXPIRED/取消 + 库存 held 回落
        inv.refresh_from_db()
        self.assertEqual(inv.held, held_before - 1)
        hold.refresh_from_db()
        self.assertNotEqual(hold.status, InventoryHold.HoldStatus.HELD)
        # 审计含取消原因
        audit = AuditEvent.objects.filter(
            action="frontdesk.reservation_cancelled",
            entity_id=reservation.booking_no,
        ).first()
        self.assertIsNotNone(audit)
        assert audit is not None  # mypy 收窄
        self.assertEqual(audit.reason, "客人来电取消")

        # 重复取消：幂等 changed=false
        repeat = self._cancel(reservation.booking_no)
        self.assertEqual(repeat.status_code, 200, repeat.content)
        self.assertFalse(repeat.json()["data"]["changed"])

    def test_cancel_confirmed_with_payment_redirects_to_refund_flow(self):
        """已收款订单：服务层守卫 409，不得绕过退款三人分离流程。"""
        from apps.reservations.front_desk_services import collect_manual_payment

        user = self._auth()
        reservation = self._create_pending_reservation(key="fd-cancel-paid")
        # 收款成功 → 订单按 DEC-PND-01 确认（CONFIRMED）
        collect_manual_payment(
            property_obj=self.property,
            booking_no=reservation.booking_no,
            amount=reservation.total_amount,
            method="CASH",
            idempotency_key="fd-cancel-pay-001",
            received_by=user,
        )
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        response = self._cancel(reservation.booking_no)
        self.assertEqual(response.status_code, 409, response.content)
        message = response.json()["error"]["message"]
        self.assertIn("成功收款", message)
        self.assertIn("退款", message)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_cancel_pending_with_succeeded_payment_blocked(self):
        """异常态兜底：PENDING 单若存在成功收款（回调处理中），同样禁止普通取消。"""
        reservation = self._create_pending_reservation(key="fd-cancel-abnormal")
        folio = reservation.folios.order_by("id").first()
        self.assertIsNotNone(folio)
        Payment.objects.create(
            property=self.property,
            reservation=reservation,
            folio=folio,
            amount=Decimal("100.00"),
            method="CASH",
            status=Payment.Status.SUCCEEDED,
        )
        self._auth()
        response = self._cancel(reservation.booking_no)
        self.assertEqual(response.status_code, 409, response.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_cancel_input_validation_and_permission(self):
        # 原因必填
        self._auth()
        reservation = self._create_pending_reservation(key="fd-cancel-input")
        empty = self._cancel(reservation.booking_no, reason="")
        self.assertEqual(empty.status_code, 400, empty.content)
        long = self._cancel(reservation.booking_no, reason="长" * 241)
        self.assertEqual(long.status_code, 400, long.content)
        # 不存在的预订号
        missing = self._cancel("BK-NO-SUCH-BOOKING")
        self.assertEqual(missing.status_code, 404, missing.content)
        # 只读岗 403（换回新夹具避免上面单据已被误取消）
        other = self._create_pending_reservation(key="fd-cancel-perm")
        readonly = self._make_staff(codenames={"view_accommodation"})
        self.client.force_login(readonly)
        denied = self._cancel(other.booking_no)
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertEqual(denied.json()["error"]["code"], "PERMISSION_DENIED")
