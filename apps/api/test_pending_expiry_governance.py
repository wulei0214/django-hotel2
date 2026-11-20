"""PENDING 支付保留期（TTL）过期治理回归（MC11-RES-01/02，任务书 PMS2-11C-001）。

覆盖两条业务事实：

1. 过期释放（expire_reservation_holds 命令，调度每 5 分钟）：
   过期 PENDING 单置 EXPIRED、释放库存 Hold、取消开放支付意图、
   落 public.reservation_expired 审计；重复执行幂等；未过期/非 PENDING 不动。
2. 过期确认守卫（MC11-RES-02）：过期 PENDING 单不得再经前台收款/押金
   确认（与在线入口 create_mock_payment_intent 的过期拒绝口径对齐，
   D-R4-10 文案承诺的服务端守卫）；已存在的收款事实重放不受影响。

测试密码一律运行时随机生成（无凭据字面量门禁口径）。
"""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from apps.billing.models import Payment
from apps.core.models import AuditEvent
from apps.inventory.models import RoomTypeInventory
from apps.reservations.models import InventoryHold, Reservation

from .test_walk_in_api import WalkInApiTestBase, _post_json


class PendingExpiryCommandTests(WalkInApiTestBase):
    """expire_reservation_holds：过期释放链路与幂等。"""

    def _seed_expired_pending(self, phone, idem_key, minutes_ago=20):
        reservation = self._seed_pending_order(phone, idem_key)
        reservation.expires_at = timezone.now() - timedelta(minutes=minutes_ago)
        reservation.save(update_fields=("expires_at", "updated_at"))
        return reservation

    def _seed_pending_order(self, phone, idem_key):
        """与 PendingConfirmationFlowTests 相同的真实公开预订入口。"""

        today = timezone.localdate()
        payload = {
            "full_name": "过期治理客人",
            "phone": phone,
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": today.isoformat(),
            "check_out": (today + timedelta(days=1)).isoformat(),
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        response = _post_json(
            self.client, "/api/v1/reservations", payload, HTTP_IDEMPOTENCY_KEY=idem_key
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Reservation.objects.get(booking_no=response.json()["booking_no"])

    def _hold_state(self, reservation):
        hold = InventoryHold.objects.filter(reservation_room__reservation=reservation).first()
        return hold

    def test_expired_pending_is_expired_and_hold_released_with_audit(self):
        reservation = self._seed_expired_pending("13922220001", "exp-cmd-001")
        inventory = RoomTypeInventory.objects.get(
            property=self.property,
            room_type=self.room_type,
            stay_date=timezone.localdate(),
        )
        held_before = inventory.held
        self.assertGreater(held_before, 0, "PENDING 建单应已占用 held")

        call_command("expire_reservation_holds", stdout=mock.Mock())

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.EXPIRED)
        inventory.refresh_from_db()
        self.assertEqual(inventory.held, held_before - 1, "过期后库存占用必须释放")
        hold = self._hold_state(reservation)
        self.assertIsNotNone(hold)
        self.assertEqual(hold.status, InventoryHold.HoldStatus.RELEASED)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="public.reservation_expired",
                entity_id=reservation.booking_no,
            ).exists(),
            "过期释放必须落审计（RSV-07 口径）",
        )

    def test_command_rerun_is_idempotent_and_skips_unexpired(self):
        expired = self._seed_expired_pending("13922220002", "exp-cmd-002")
        fresh = self._seed_pending_order("13922220003", "exp-cmd-003")

        call_command("expire_reservation_holds", stdout=mock.Mock())
        call_command("expire_reservation_holds", stdout=mock.Mock())

        expired.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(expired.status, Reservation.Status.EXPIRED)
        self.assertEqual(fresh.status, Reservation.Status.PENDING, "未过期订单不得被命令释放")
        # 重复执行不产生重复审计
        self.assertEqual(
            AuditEvent.objects.filter(
                action="public.reservation_expired", entity_id=expired.booking_no
            ).count(),
            1,
        )

    def test_confirmed_order_with_stale_expires_at_is_untouched(self):
        reservation = self._seed_pending_order("13922220004", "exp-cmd-004")
        # 走真实确认链（收款即确认），再把 expires_at 留在过去
        self._auth()
        collect = _post_json(
            self.client,
            f"/api/v1/front-desk/reservations/{reservation.booking_no}/payments",
            {"amount": "300.00", "method": "CARD"},
            HTTP_IDEMPOTENCY_KEY="exp-cmd-004-pay",
        )
        self.assertEqual(collect.status_code, 201, collect.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        Reservation.objects.filter(pk=reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=30)
        )

        call_command("expire_reservation_holds", stdout=mock.Mock())

        reservation.refresh_from_db()
        self.assertEqual(
            reservation.status, Reservation.Status.CONFIRMED, "非 PENDING 单不在命令视野"
        )


class PendingExpiryConfirmationGuardTests(WalkInApiTestBase):
    """MC11-RES-02：过期 PENDING 单的前台确认守卫（收款/押金 → 409）。"""

    def _seed_expired_pending(self, phone, idem_key):
        reservation = self._seed_pending_order(phone, idem_key)
        Reservation.objects.filter(pk=reservation.pk).update(
            expires_at=timezone.now() - timedelta(minutes=20)
        )
        reservation.refresh_from_db()
        return reservation

    def _seed_pending_order(self, phone, idem_key):
        today = timezone.localdate()
        payload = {
            "full_name": "过期守卫客人",
            "phone": phone,
            "room_type_id": self.room_type.pk,
            "rate_plan_id": self.rate_plan.pk,
            "check_in": today.isoformat(),
            "check_out": (today + timedelta(days=1)).isoformat(),
            "rooms": 1,
            "adults": 1,
            "children": 0,
        }
        response = _post_json(
            self.client, "/api/v1/reservations", payload, HTTP_IDEMPOTENCY_KEY=idem_key
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Reservation.objects.get(booking_no=response.json()["booking_no"])

    def test_expired_pending_frontdesk_payment_rejected_and_hold_untouched(self):
        reservation = self._seed_expired_pending("13933330001", "exp-guard-001")
        self._auth()
        inventory = RoomTypeInventory.objects.get(
            property=self.property,
            room_type=self.room_type,
            stay_date=timezone.localdate(),
        )
        held_before = inventory.held

        collect = _post_json(
            self.client,
            f"/api/v1/front-desk/reservations/{reservation.booking_no}/payments",
            {"amount": "300.00", "method": "CASH"},
            HTTP_IDEMPOTENCY_KEY="exp-guard-pay-001",
        )
        self.assertEqual(collect.status_code, 409, collect.content)
        self.assertIn("支付保留期", collect.json()["error"]["message"])

        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        inventory.refresh_from_db()
        self.assertEqual(inventory.held, held_before, "被拒绝的收款不得消耗库存占用")
        self.assertFalse(
            Payment.objects.filter(reservation=reservation, provider="frontdesk_manual").exists()
        )

    def test_expired_pending_deposit_rejected(self):
        reservation = self._seed_expired_pending("13933330002", "exp-guard-002")
        self._auth()

        deposit = _post_json(
            self.client,
            f"/api/v1/front-desk/reservations/{reservation.booking_no}/deposits",
            {"amount": "90.00", "method": "CASH"},
            HTTP_IDEMPOTENCY_KEY="exp-guard-dep-001",
        )
        self.assertEqual(deposit.status_code, 409, deposit.content)
        self.assertIn("支付保留期", deposit.json()["error"]["message"])
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_unexpired_pending_still_confirms_by_payment(self):
        """守卫只拦过期单：未过期 PENDING 收款即确认的原口径不回归（R3-01）。"""
        reservation = self._seed_pending_order("13933330003", "exp-guard-003")
        self._auth()
        collect = _post_json(
            self.client,
            f"/api/v1/front-desk/reservations/{reservation.booking_no}/payments",
            {"amount": "300.00", "method": "CARD"},
            HTTP_IDEMPOTENCY_KEY="exp-guard-pay-003",
        )
        self.assertEqual(collect.status_code, 201, collect.content)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    def test_existing_payment_fact_replay_survives_expiry(self):
        """收款事实先于过期成立：同幂等键重放必须返回原事实，不得触发过期守卫。"""
        reservation = self._seed_expired_pending("13933330004", "exp-guard-004")
        user = self._auth()
        from apps.reservations.front_desk_services import (
            FrontDeskOperationError,
            collect_manual_payment,
        )

        # 预置一笔同 provider_ref 的已成功收款事实（模拟事实先于过期写入，
        # 确认迁移尚未完成的并发窗口）。
        provider_ref = f"FD{self.property.pk}-exp-guard-pay-004"
        Payment.objects.create(
            property=self.property,
            reservation=reservation,
            folio=reservation.folios.first(),
            provider="frontdesk_manual",
            method="CARD",
            amount=Decimal("300.00"),
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            provider_ref=provider_ref,
            paid_at=timezone.now(),
        )
        payment, created = collect_manual_payment(
            property_obj=self.property,
            booking_no=reservation.booking_no,
            amount="300.00",
            method="CARD",
            idempotency_key="exp-guard-pay-004",
            received_by=user,
        )
        self.assertFalse(created)
        self.assertEqual(payment.provider_ref, provider_ref)
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

        # 反向对照：无既有事实时同场景必须被守卫拒绝
        reservation2 = self._seed_expired_pending("13933330005", "exp-guard-005")
        with self.assertRaises(FrontDeskOperationError) as ctx:
            collect_manual_payment(
                property_obj=self.property,
                booking_no=reservation2.booking_no,
                amount="300.00",
                method="CARD",
                idempotency_key="exp-guard-pay-005",
                received_by=user,
            )
        self.assertIn("支付保留期", str(ctx.exception))
