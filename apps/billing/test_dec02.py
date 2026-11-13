"""DEC-02-A1/B2 验收测试（依据 docs/PMS2_DEC-02业务决策基线_2026-11-09.md）。

A1：原负债冻结 + 账务调整独立记录（财务发起/值班经理审批/执行生凭证/
    抵扣减净额/冲销增净额/净额重建/SoD/幂等/超额拒绝/未审批不影响净额）。
B2：异常放行（前台申请/值班经理审批/放行后不建正式负债/待财务核查/
    受控转负债/无需退款关闭/申请人自批拒绝/重复申请拒绝/跨物业隔离）。
"""

import threading
import unittest
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402

from apps.billing.dec02 import (
    approve_adjustment,
    approve_exception_release,
    consume_approved_exception_release,
    execute_adjustment,
    liability_net_snapshot,
    reject_exception_release,
    request_adjustment,
    request_exception_release,
    settle_exception_release,
)
from apps.billing.models import (
    BillingAdjustment,
    CheckoutExceptionRelease,
    Folio,
    LedgerEntry,
    Payment,
    RefundLiability,
)
from apps.core.models import AuditEvent, Property
from apps.reservations.front_desk_services import (
    FrontDeskOperationError,
    check_out_stay,
)
from apps.reservations.models import Reservation

_TEST_PW_4756BB53 = get_random_string(24)


def _money(v):
    return Decimal(str(v))


@override_settings(DEBUG=True, DJANGO_ENV="test")
class Dec02TestBase(TestCase):
    """公共夹具：双房预订 + 预付 1200，两间各 600，均已入住。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.other_prop = Property.objects.create(
            code="OTHER-DEC02", name="其他物业（DEC-02 测试）", timezone="Asia/Shanghai"
        )
        from apps.guests.models import Guest
        from apps.inventory.models import RatePlan, Room, RoomType

        cls.room_type = RoomType.objects.create(
            property=cls.prop, code="STD-DEC02", name="DEC-02 测试房型", capacity=2
        )
        cls.rate_plan = RatePlan.objects.create(
            property=cls.prop,
            code="DEC02-BAR",
            name="DEC-02 测试门市价",
            valid_from=timezone.localdate() - timedelta(days=1),
        )
        cls.room_a = Room.objects.create(
            property=cls.prop,
            room_type=cls.room_type,
            number="D201",
            occupancy_status=Room.OccupancyStatus.VACANT,
        )
        cls.room_b = Room.objects.create(
            property=cls.prop,
            room_type=cls.room_type,
            number="D202",
            occupancy_status=Room.OccupancyStatus.VACANT,
        )
        cls.guest = Guest.objects.create(property=cls.prop, full_name="DEC-02 测试客人")
        cls.users = {}
        for name in ("dec02_front", "dec02_duty", "dec02_finance"):
            cls.users[name.split("_")[1]] = User.objects.create_user(
                username=name, password=_TEST_PW_4756BB53
            )
        cls.front = cls.users["front"]
        cls.duty = cls.users["duty"]
        cls.finance = cls.users["finance"]

    def setUp(self):
        from apps.billing.models import Folio
        from apps.reservations.models import ReservationRoom, Stay

        # WB-097 正式语义（DEC-ED-01 §2.1）：退房按实际入住晚数的逐晚快照
        # 结算，未住晚不计房费。原夹具为"当日入住、住 2 晚、当日退房"，
        # 在正式语义下同日退房=0 晚=0 房费，负债基数会从 600 变为 1200。
        # 为保持既有 DEC-02 断言口径（负债 600 = 预付 1200 - 两间各 300
        # 已履行房费），改为"昨日入住 1 晚 300、今日退房"的已住 1 晚事实
        # （每间实际结算 300，与旧口径一致）。
        check_in = timezone.localdate() - timedelta(days=1)
        check_out = timezone.localdate()
        self.reservation = Reservation.objects.create(
            property=self.prop,
            guest=self.guest,
            booking_no="DEC02TEST001",
            status=Reservation.Status.CHECKED_IN,
            check_in=check_in,
            check_out=check_out,
            room_count=2,
            total_amount=_money("1200.00"),
            currency="CNY",
        )
        self.line_a = ReservationRoom.objects.create(
            property=self.prop,
            reservation=self.reservation,
            room_type_id=self.room_type.pk,
            rate_plan=self.rate_plan,
            check_in=check_in,
            check_out=check_out,
            amount=_money("300.00"),
            nightly_rates=[{"date": str(check_in), "price": "300.00"}],
        )
        self.line_b = ReservationRoom.objects.create(
            property=self.prop,
            reservation=self.reservation,
            room_type_id=self.room_type.pk,
            rate_plan=self.rate_plan,
            check_in=check_in,
            check_out=check_out,
            amount=_money("300.00"),
            nightly_rates=[{"date": str(check_in), "price": "300.00"}],
        )
        self.folio = Folio.objects.create(
            property=self.prop, reservation=self.reservation, guest=self.guest, currency="CNY"
        )
        self.stay_a = Stay.objects.create(
            property=self.prop,
            reservation_room=self.line_a,
            room=self.room_a,
            status=Stay.Status.IN_HOUSE,
        )
        self.stay_b = Stay.objects.create(
            property=self.prop,
            reservation_room=self.line_b,
            room=self.room_b,
            status=Stay.Status.IN_HOUSE,
        )
        self.prepaid = Payment.objects.create(
            property=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            provider="mock",
            provider_ref="DEC02-PREPAID-001",
            method="MOCK",
            amount=_money("1200.00"),
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            paid_at=timezone.now(),
        )
        if getattr(self, "prepaid_has_entry", True):
            LedgerEntry.objects.create(
                property=self.prop,
                folio=self.folio,
                entry_type=LedgerEntry.EntryType.PAYMENT,
                amount=_money("-1200.00"),
                currency="CNY",
                business_date=timezone.localdate(),
                reference="payment:DEC02-PREPAID-001",
                description="预付",
            )

    def _checkout_first_room(self):
        return check_out_stay(
            property_obj=self.prop, stay_id=self.stay_a.pk, checked_out_by=self.front
        )

    def _checkout_all_and_register_liability(self):
        self._checkout_first_room()
        check_out_stay(property_obj=self.prop, stay_id=self.stay_b.pk, checked_out_by=self.front)
        return RefundLiability.objects.get(folio=self.folio)


class Dec02A1AdjustmentTests(Dec02TestBase):
    """A1：账务调整/抵扣全流程与控制。"""

    def test_a1_sod_self_approve_rejected(self):
        liability = self._checkout_all_and_register_liability()
        adj, _ = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
            amount="100.00",
            reason="漏记迷你吧消费",
            requested_by=self.finance,
            idempotency_key="dec02-adj-001",
        )
        with self.assertRaises(ValueError) as ctx:
            approve_adjustment(
                property_obj=self.prop, adjustment_id=adj.pk, approved_by=self.finance
            )
        self.assertIn("职责分离", str(ctx.exception))
        adj.refresh_from_db()
        self.assertEqual(adj.status, BillingAdjustment.Status.REQUESTED)
        # 净额不受未审批调整影响
        net = liability_net_snapshot(liability)
        self.assertEqual(net["deducted_amount"], Decimal("0.00"))

    def test_a1_deduction_flow_updates_net_and_audits(self):
        liability = self._checkout_all_and_register_liability()
        adj, created = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
            amount="100.00",
            reason="漏记损坏赔偿",
            requested_by=self.finance,
            idempotency_key="dec02-adj-002",
        )
        self.assertTrue(created)
        approve_adjustment(property_obj=self.prop, adjustment_id=adj.pk, approved_by=self.duty)
        executed = execute_adjustment(
            property_obj=self.prop, adjustment_id=adj.pk, executed_by=self.finance
        )
        self.assertEqual(executed.status, BillingAdjustment.Status.EXECUTED)
        # 独立抵扣凭证（ADJUSTMENT 类型，正数），挂在已关闭账单上
        entry = LedgerEntry.objects.get(reference="adjustment:dec02-adj-002")
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.ADJUSTMENT)
        self.assertEqual(entry.amount, Decimal("100.00"))
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, Decimal("500.00"))
        self.assertEqual(liability.status, RefundLiability.Status.PARTIALLY_SETTLED)
        net = liability_net_snapshot(liability)
        self.assertEqual(net["net_refundable"], Decimal("500.00"))
        self.assertEqual(net["deducted_amount"], Decimal("100.00"))
        self.assertTrue(AuditEvent.objects.filter(action="billing.adjustment_executed").exists())

    def test_a1_charge_reverse_increases_net(self):
        liability = self._checkout_all_and_register_liability()
        adj, _ = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_REVERSE,
            amount="80.00",
            reason="重复计费冲销",
            requested_by=self.finance,
            idempotency_key="dec02-adj-003",
        )
        approve_adjustment(property_obj=self.prop, adjustment_id=adj.pk, approved_by=self.duty)
        execute_adjustment(property_obj=self.prop, adjustment_id=adj.pk, executed_by=self.finance)
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, Decimal("680.00"))
        net = liability_net_snapshot(liability)
        self.assertEqual(net["increased_amount"], Decimal("80.00"))
        self.assertEqual(net["net_refundable"], Decimal("680.00"))

    def test_a1_refund_increase_beyond_original_allowed(self):
        liability = self._checkout_all_and_register_liability()
        adj, _ = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.REFUND_INCREASE,
            amount="150.00",
            reason="投诉补偿（经批准）",
            requested_by=self.finance,
            idempotency_key="dec02-adj-004",
        )
        approve_adjustment(property_obj=self.prop, adjustment_id=adj.pk, approved_by=self.duty)
        execute_adjustment(property_obj=self.prop, adjustment_id=adj.pk, executed_by=self.finance)
        liability.refresh_from_db()
        # 增加应退可突破原始登记额（约束已放宽为非负）
        self.assertEqual(liability.remaining_amount, Decimal("750.00"))
        # WB-091：增加应退与冲销同构——执行必须生成独立负向调整凭证
        # （语义=补偿/增加应退），并回填 adjustment.ledger_entry。
        entry = LedgerEntry.objects.get(reference="adjustment:dec02-adj-004")
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.ADJUSTMENT)
        self.assertEqual(entry.amount, Decimal("-150.00"))
        self.assertEqual(entry.folio_id, self.folio.pk)
        adj.refresh_from_db()
        self.assertEqual(adj.ledger_entry_id, entry.pk)
        net = liability_net_snapshot(liability)
        self.assertEqual(net["net_refundable"], Decimal("750.00"))

    def test_a1_charge_reverse_reopens_settled_liability_and_keeps_prior_settled_at(self):
        """WB-093：结清负债经批准回增重新打开——settled_at 必须清空，
        原结清时间通过审计 after.prior_settled_at 保留。"""
        from apps.billing.services_v2 import BillingService

        liability = self._checkout_all_and_register_liability()
        # 先结清：全额退款执行成功
        refund, created = BillingService.request_refund(
            property_obj=self.prop,
            payment_id=self.prepaid.pk,
            amount="600.00",
            reason="全额退款结清负债",
            requested_by=self.front,
            idempotency_key="dec02-settle-rfd-001",
            refund_liability_id=liability.pk,
        )
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.prop, refund_id=refund.pk, approved_by=self.duty
        )
        BillingService.execute_refund(
            property_obj=self.prop,
            refund_id=refund.pk,
            executed_by=self.finance,
            confirm_refund_paid=True,
            execution_reference="BANK-DEC02-SETTLE-1",
            idempotency_key="dec02-settle-exec-001",
        )
        liability.refresh_from_db()
        self.assertEqual(liability.status, RefundLiability.Status.SETTLED)
        prior_settled_at = liability.settled_at
        self.assertIsNotNone(prior_settled_at)
        # 冲销回增 → 余额重新 >0，负债重新打开
        adj, _ = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_REVERSE,
            amount="80.00",
            reason="重复计费冲销（重新打开负债）",
            requested_by=self.finance,
            idempotency_key="dec02-adj-reopen-001",
        )
        approve_adjustment(property_obj=self.prop, adjustment_id=adj.pk, approved_by=self.duty)
        execute_adjustment(property_obj=self.prop, adjustment_id=adj.pk, executed_by=self.finance)
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, Decimal("80.00"))
        self.assertEqual(liability.status, RefundLiability.Status.PARTIALLY_SETTLED)
        self.assertIsNone(liability.settled_at, "重新打开的负债不得残留旧结清时间")
        event = AuditEvent.objects.filter(
            action="billing.adjustment_executed", entity_id=adj.pk
        ).latest("pk")
        self.assertEqual(event.after_json["prior_settled_at"], prior_settled_at.isoformat())

    def test_a1_deduction_exceeding_net_rejected(self):
        liability = self._checkout_all_and_register_liability()
        adj, _ = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
            amount="9999.00",
            reason="超额抵扣",
            requested_by=self.finance,
            idempotency_key="dec02-adj-005",
        )
        approve_adjustment(property_obj=self.prop, adjustment_id=adj.pk, approved_by=self.duty)
        with self.assertRaises(ValueError) as ctx:
            execute_adjustment(
                property_obj=self.prop, adjustment_id=adj.pk, executed_by=self.finance
            )
        self.assertIn("不足以抵扣", str(ctx.exception))
        adj.refresh_from_db()
        self.assertEqual(adj.status, BillingAdjustment.Status.APPROVED)
        liability.refresh_from_db()
        self.assertEqual(liability.remaining_amount, Decimal("600.00"))

    def test_a1_execute_twice_rejected_by_status_machine(self):
        liability = self._checkout_all_and_register_liability()
        adj, _ = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
            amount="50.00",
            reason="幂等测试",
            requested_by=self.finance,
            idempotency_key="dec02-adj-006",
        )
        approve_adjustment(property_obj=self.prop, adjustment_id=adj.pk, approved_by=self.duty)
        execute_adjustment(property_obj=self.prop, adjustment_id=adj.pk, executed_by=self.finance)
        with self.assertRaises(ValueError) as ctx:
            execute_adjustment(
                property_obj=self.prop, adjustment_id=adj.pk, executed_by=self.finance
            )
        self.assertIn("只有已批准", str(ctx.exception))
        self.assertFalse(
            LedgerEntry.objects.filter(reference="adjustment:dec02-adj-006-dup").exists()
        )

    def test_a1_idempotent_request_same_facts(self):
        liability = self._checkout_all_and_register_liability()
        adj1, c1 = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
            amount="60.00",
            reason="幂等重放",
            requested_by=self.finance,
            idempotency_key="dec02-adj-007",
        )
        adj2, c2 = request_adjustment(
            property_obj=self.prop,
            folio_id=self.folio.pk,
            adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
            amount="60.00",
            reason="幂等重放",
            requested_by=self.finance,
            idempotency_key="dec02-adj-007",
        )
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(adj1.pk, adj2.pk)
        with self.assertRaises(ValueError):
            request_adjustment(
                property_obj=self.prop,
                folio_id=self.folio.pk,
                adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
                amount="999.00",
                reason="同键不同事实",
                requested_by=self.finance,
                idempotency_key="dec02-adj-007",
            )

    def test_a1_open_folio_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            request_adjustment(
                property_obj=self.prop,
                folio_id=self.folio.pk,
                adjustment_type=BillingAdjustment.AdjustmentType.CHARGE_ADD,
                amount="10.00",
                reason="未关闭账单",
                requested_by=self.finance,
                idempotency_key="dec02-adj-008",
            )
        self.assertIn("已关闭账单", str(ctx.exception))


class Dec02B2ExceptionReleaseTests(Dec02TestBase):
    """B2：异常放行与待财务核查全流程。

    凭证孤儿化：收款账本凭证的引用被改为孤儿引用（余额事实保留在账本中，
    但负债登记按引用匹配找不到可追溯来源）→ 负债登记被阻断。
    """

    def _orphan_prepaid_voucher(self):
        """把预付凭证引用改为孤儿引用（模拟凭证缺失/不一致）。

        LedgerEntry 的不可变查询集禁止 update/delete（业务守卫）；
        测试通过原生 QuerySet 绕过，仅用于构造 B2 触发场景。
        """
        from django.db.models import QuerySet

        QuerySet(LedgerEntry).filter(reference="payment:DEC02-PREPAID-001").update(
            reference="orphaned:DEC02-PREPAID-001"
        )

    def _restore_prepaid_voucher(self):
        """财务补录：把孤儿凭证引用恢复为可追溯引用（同上，测试专用绕过）。"""
        from django.db.models import QuerySet

        QuerySet(LedgerEntry).filter(reference="orphaned:DEC02-PREPAID-001").update(
            reference="payment:DEC02-PREPAID-001"
        )

    def _register_blocked(self):
        self._orphan_prepaid_voucher()
        self._checkout_first_room()
        with self.assertRaises((ValueError, FrontDeskOperationError)):
            check_out_stay(
                property_obj=self.prop, stay_id=self.stay_b.pk, checked_out_by=self.front
            )

    def test_b2_blocked_without_approved_release(self):
        self._register_blocked()
        self.assertFalse(CheckoutExceptionRelease.objects.exists())
        self.assertFalse(
            RefundLiability.objects.exists(), "无可靠来源时不得创建正式负债（DEC-02-B2 禁止项）"
        )

    def test_b2_full_flow_release_without_liability(self):
        self._register_blocked()
        # 前台发起
        release, created = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.NO_PAYMENT_SOURCE,
            reason_text="离店时无法追溯预付款来源，申请异常放行",
            requested_by=self.front,
            idempotency_key="dec02-rel-001",
        )
        self.assertTrue(created)
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.REQUESTED)
        # 申请人自批拒绝
        with self.assertRaises(ValueError) as ctx:
            approve_exception_release(
                property_obj=self.prop, release_id=release.pk, approved_by=self.front
            )
        self.assertIn("职责分离", str(ctx.exception))
        # 值班经理批准
        approve_exception_release(
            property_obj=self.prop, release_id=release.pk, approved_by=self.duty
        )
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.APPROVED)
        # 放行退房：不再阻断、不创建负债、记录转 RELEASED
        stay_out, _ = check_out_stay(
            property_obj=self.prop, stay_id=self.stay_b.pk, checked_out_by=self.front
        )
        self.assertEqual(
            stay_out.status,
            "CHECKED_OUT" if not hasattr(stay_out.status, "value") else stay_out.status.value,
        )
        self.assertFalse(RefundLiability.objects.exists())
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.RELEASED)
        self.assertEqual(release.stay_id, self.stay_b.pk)
        # 待财务核查：转正式负债（受控重验 → 仍无凭证则失败）
        with self.assertRaises(ValueError):
            settle_exception_release(
                property_obj=self.prop,
                release_id=release.pk,
                settled_by=self.finance,
                outcome="CONVERT_LIABILITY",
                verify_note="核查尝试转负债",
            )
        # 财务按证据关闭（无需退款）
        settled = settle_exception_release(
            property_obj=self.prop,
            release_id=release.pk,
            settled_by=self.finance,
            outcome="CLOSE_NO_REFUND",
            verify_note="核查确认预付款来源已在线下人工核销，无需退款",
        )
        self.assertEqual(settled.status, CheckoutExceptionRelease.Status.CLOSED_NO_REFUND)
        self.assertTrue(
            AuditEvent.objects.filter(action="billing.checkout_exception_settled").exists()
        )

    def test_b2_settle_converts_to_liability_when_evidence_restored(self):
        self._register_blocked()
        release, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.LEDGER_MISSING,
            reason_text="凭证缺失，后续人工补录",
            requested_by=self.front,
            idempotency_key="dec02-rel-002",
        )
        approve_exception_release(
            property_obj=self.prop, release_id=release.pk, approved_by=self.duty
        )
        check_out_stay(property_obj=self.prop, stay_id=self.stay_b.pk, checked_out_by=self.front)
        # 财务补录：恢复凭证可追溯引用（证据补齐）
        self._restore_prepaid_voucher()
        settled = settle_exception_release(
            property_obj=self.prop,
            release_id=release.pk,
            settled_by=self.finance,
            outcome="CONVERT_LIABILITY",
            verify_note="收款凭证已补录，金额/币种/物业归属核验通过",
        )
        self.assertEqual(settled.status, CheckoutExceptionRelease.Status.CONVERTED_LIABILITY)
        self.assertIsNotNone(settled.refund_liability)
        self.assertEqual(settled.refund_liability.amount, Decimal("600.00"))

    def test_b2_request_amount_bound_to_blocked_balance(self):
        """WB-049：申请金额与账单负余额绑定——超额/零额/无负余额拒绝。"""
        # setUp：预付 1200、无房费凭证 → 负余额 -1200
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=self.reservation,
                folio=self.folio,
                amount="99999.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="超额放行必须拒绝",
                requested_by=self.front,
                idempotency_key="dec02-rel-wb49a",
            )
        self.assertIn("超过当前账单负余额", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=self.reservation,
                folio=self.folio,
                amount="0.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="零额放行必须拒绝",
                requested_by=self.front,
                idempotency_key="dec02-rel-wb49b",
            )
        self.assertIn("必须大于零", str(ctx.exception))
        # 恰等于当前负余额绝对值 → 允许
        release, created = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="1200.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="等于当前负余额",
            requested_by=self.front,
            idempotency_key="dec02-rel-wb49c",
        )
        self.assertTrue(created)
        self.assertEqual(release.amount, Decimal("1200.00"))

    def test_b2_consume_keeps_approved_amount_and_blocks_excess(self):
        """WB-051：放行消费不覆盖已批金额；受阻金额超过已批额时维持阻断。"""
        self._register_blocked()
        release, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.NO_PAYMENT_SOURCE,
            reason_text="批准放行 600",
            requested_by=self.front,
            idempotency_key="dec02-rel-wb51a",
        )
        approve_exception_release(
            property_obj=self.prop, release_id=release.pk, approved_by=self.duty
        )
        # 实际受阻 900 > 已批 600 → 不消费（返回 None），金额不被覆盖
        consumed = consume_approved_exception_release(
            property_obj=self.prop,
            folio=self.folio,
            stay=self.stay_b,
            amount="900.00",
            actor=self.front,
            trigger_reason="测试受阻",
        )
        self.assertIsNone(consumed)
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.APPROVED)
        self.assertEqual(release.amount, Decimal("600.00"))
        # 实际受阻 600 ≤ 已批 600 → 消费，且 release.amount 保持批准值
        consumed = consume_approved_exception_release(
            property_obj=self.prop,
            folio=self.folio,
            stay=self.stay_b,
            amount="600.00",
            actor=self.front,
            trigger_reason="测试受阻",
        )
        self.assertIsNotNone(consumed)
        release.refresh_from_db()
        self.assertEqual(release.amount, Decimal("600.00"))
        event = AuditEvent.objects.filter(
            action="billing.checkout_exception_approved", entity_id=release.pk
        ).latest("pk")
        self.assertEqual(event.after_json["amount"], "600.00")

    def test_b2_settle_converts_at_actual_blocked_amount(self):
        """WB-051：转负债按核查时点实际负余额建账（小于已批额时）。"""
        self._register_blocked()
        # 申请时点：stay_a 房费已入账（stay_b 被阻退房已回滚）→ 负余额 -900
        release, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="900.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.LEDGER_MISSING,
            reason_text="凭证缺失，后续人工补录",
            requested_by=self.front,
            idempotency_key="dec02-rel-wb51b",
        )
        approve_exception_release(
            property_obj=self.prop, release_id=release.pk, approved_by=self.duty
        )
        check_out_stay(property_obj=self.prop, stay_id=self.stay_b.pk, checked_out_by=self.front)
        # 余额变化：补录后另一间房费入账使实际负余额缩为 -600+300=-300
        LedgerEntry.objects.create(
            property=self.prop,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
            amount=Decimal("300.00"),
            currency="CNY",
            business_date=timezone.localdate(),
            reference="stay-room-charge:wb51-settle-drift",
            description="核查前补入房费（测试余额漂移）",
        )
        self._restore_prepaid_voucher()
        settled = settle_exception_release(
            property_obj=self.prop,
            release_id=release.pk,
            settled_by=self.finance,
            outcome="CONVERT_LIABILITY",
            verify_note="凭证已补录，按实际负余额转负债",
        )
        self.assertEqual(settled.status, CheckoutExceptionRelease.Status.CONVERTED_LIABILITY)
        self.assertEqual(settled.refund_liability.amount, Decimal("300.00"))

    def test_b2_settle_rejects_blocked_exceeding_approved(self):
        """WB-051：实际负余额超过已批放行额 → 拒绝转负债（超出未审批）。"""
        self._register_blocked()
        release, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.LEDGER_MISSING,
            reason_text="凭证缺失，后续人工补录",
            requested_by=self.front,
            idempotency_key="dec02-rel-wb51c",
        )
        approve_exception_release(
            property_obj=self.prop, release_id=release.pk, approved_by=self.duty
        )
        check_out_stay(property_obj=self.prop, stay_id=self.stay_b.pk, checked_out_by=self.front)
        # 负余额扩大（多退了一笔）：-600 − 400 = -1000，abs > 已批 600
        LedgerEntry.objects.create(
            property=self.prop,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("-400.00"),
            currency="CNY",
            business_date=timezone.localdate(),
            reference="adjustment:wb51-exceed-drift",
            description="核查前补入负向调整（测试超额拒绝）",
        )
        self._restore_prepaid_voucher()
        with self.assertRaises(ValueError) as ctx:
            settle_exception_release(
                property_obj=self.prop,
                release_id=release.pk,
                settled_by=self.finance,
                outcome="CONVERT_LIABILITY",
                verify_note="负余额超出审批额度",
            )
        self.assertIn("超过已批准放行金额", str(ctx.exception))
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.RELEASED)
        self.assertFalse(RefundLiability.objects.filter(folio=self.folio).exists())

    def test_b2_duplicate_active_request_rejected(self):
        self._register_blocked()
        request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="第一笔在途申请",
            requested_by=self.front,
            idempotency_key="dec02-rel-003",
        )
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=self.reservation,
                folio=self.folio,
                amount="600.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="第二笔重复申请",
                requested_by=self.front,
                idempotency_key="dec02-rel-004",
            )
        self.assertIn("不能重复发起", str(ctx.exception))

    def test_b2_cross_property_isolation(self):
        self._register_blocked()
        release, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="跨物业隔离测试",
            requested_by=self.front,
            idempotency_key="dec02-rel-005",
        )
        with self.assertRaises(ValueError):
            approve_exception_release(
                property_obj=self.other_prop, release_id=release.pk, approved_by=self.duty
            )
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.REQUESTED)

    # ------------------------------------------------------------------
    # WB-094：异常放行对象关联核验
    # ------------------------------------------------------------------
    def _make_other_reservation_folio(
        self, property_obj, booking_no="DEC02TEST002", currency="CNY"
    ):
        """构造另一张（物业, 预订, 账单）三元组，供归属核验测试用。"""
        reservation = Reservation.objects.create(
            property=property_obj,
            guest=self.guest,
            booking_no=booking_no,
            status=Reservation.Status.CHECKED_IN,
            check_in=timezone.localdate() - timedelta(days=1),
            check_out=timezone.localdate(),
            room_count=1,
            total_amount=_money("300.00"),
            currency="CNY",
        )
        folio = Folio.objects.create(
            property=property_obj, reservation=reservation, guest=self.guest, currency=currency
        )
        return reservation, folio

    def test_b2_request_rejects_cross_reservation_folio(self):
        """WB-094：账单属于其他预订 → 拒绝发起异常放行。"""
        self._register_blocked()
        _, other_folio = self._make_other_reservation_folio(self.prop)
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=self.reservation,
                folio=other_folio,
                amount="600.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="跨预订 folio 必须拒绝",
                requested_by=self.front,
                idempotency_key="dec02-rel-wb94a",
            )
        self.assertIn("账单与预订不一致", str(ctx.exception))
        self.assertFalse(
            CheckoutExceptionRelease.objects.filter(idempotency_key="dec02-rel-wb94a").exists()
        )

    def test_b2_request_rejects_cross_property_objects(self):
        """WB-094：预订或账单属于其他物业 → 拒绝发起异常放行。"""
        self._register_blocked()
        other_reservation, other_folio = self._make_other_reservation_folio(
            self.other_prop, booking_no="DEC02OTHER01"
        )
        # 预订跨物业（folio 为本物业账单，预订属于其他物业）
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=other_reservation,
                folio=self.folio,
                amount="600.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="预订跨物业必须拒绝",
                requested_by=self.front,
                idempotency_key="dec02-rel-wb94b1",
            )
        self.assertIn("预订不属于当前物业", str(ctx.exception))
        # 账单跨物业
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=self.reservation,
                folio=other_folio,
                amount="600.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="账单跨物业必须拒绝",
                requested_by=self.front,
                idempotency_key="dec02-rel-wb94b2",
            )
        self.assertIn("账单不属于当前物业", str(ctx.exception))
        self.assertFalse(
            CheckoutExceptionRelease.objects.filter(
                idempotency_key__startswith="dec02-rel-wb94b"
            ).exists()
        )

    def test_b2_request_rejects_currency_mismatch_folio(self):
        """WB-094：账单币种与预订币种不一致 → 拒绝发起异常放行。"""
        self._register_blocked()
        # 模拟脏数据：账单币种被改写为 USD（M-04 限一个预订一张 MASTER 账单，
        # 不能另建异币种账单；ImmutableFinancialFactQuerySet 禁 update，
        # 测试经原生 QuerySet 绕过，仅用于构造不一致场景）
        from django.db.models import QuerySet

        QuerySet(Folio).filter(pk=self.folio.pk).update(currency="USD")
        mismatched_folio = Folio.objects.get(pk=self.folio.pk)
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=self.reservation,
                folio=mismatched_folio,
                amount="600.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="异币种账单必须拒绝",
                requested_by=self.front,
                idempotency_key="dec02-rel-wb94c",
            )
        self.assertIn("币种", str(ctx.exception))
        self.assertFalse(
            CheckoutExceptionRelease.objects.filter(idempotency_key="dec02-rel-wb94c").exists()
        )

    def test_b2_consume_rejects_release_with_mismatched_reservation(self):
        """WB-094：放行记录关联的预订与账单不一致 → 锁内复核拒绝消费。"""
        self._register_blocked()
        release, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.NO_PAYMENT_SOURCE,
            reason_text="归属复核测试",
            requested_by=self.front,
            idempotency_key="dec02-rel-wb94d",
        )
        approve_exception_release(
            property_obj=self.prop, release_id=release.pk, approved_by=self.duty
        )
        other_reservation, _ = self._make_other_reservation_folio(
            self.prop, booking_no="DEC02TEST003"
        )
        # 直接改写 release.reservation 指向其他预订（模拟历史脏数据；
        # ImmutableFinancialFactQuerySet 禁 update，测试经原生 QuerySet 绕过）
        from django.db.models import QuerySet

        QuerySet(CheckoutExceptionRelease).filter(pk=release.pk).update(
            reservation=other_reservation
        )
        with self.assertRaises(ValueError) as ctx:
            consume_approved_exception_release(
                property_obj=self.prop,
                folio=self.folio,
                stay=self.stay_b,
                amount="600.00",
                actor=self.front,
                trigger_reason="测试受阻",
            )
        self.assertIn("归属不一致", str(ctx.exception))
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.APPROVED)

    def test_b2_settle_rejects_release_with_cross_property_folio(self):
        """WB-094：放行记录关联的账单跨物业 → 锁内复核拒绝结案。"""
        self._register_blocked()
        release, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.LEDGER_MISSING,
            reason_text="结案归属复核测试",
            requested_by=self.front,
            idempotency_key="dec02-rel-wb94e",
        )
        approve_exception_release(
            property_obj=self.prop, release_id=release.pk, approved_by=self.duty
        )
        check_out_stay(property_obj=self.prop, stay_id=self.stay_b.pk, checked_out_by=self.front)
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.RELEASED)
        _, other_folio = self._make_other_reservation_folio(
            self.other_prop, booking_no="DEC02OTHER02"
        )
        from django.db.models import QuerySet

        QuerySet(CheckoutExceptionRelease).filter(pk=release.pk).update(folio=other_folio)
        with self.assertRaises(ValueError) as ctx:
            settle_exception_release(
                property_obj=self.prop,
                release_id=release.pk,
                settled_by=self.finance,
                outcome="CLOSE_NO_REFUND",
                verify_note="账单跨物业必须拒绝结案",
            )
        self.assertIn("禁止结案", str(ctx.exception))
        release.refresh_from_db()
        self.assertEqual(release.status, CheckoutExceptionRelease.Status.RELEASED)

    # ------------------------------------------------------------------
    # WB-112：异常放行申请防并发重复（Folio 行锁 + 幂等键 + 在途检查）
    # ------------------------------------------------------------------
    def _make_folio_with_negative_balance(self, booking_no, amount="800.00"):
        """构造另一张有负余额的（预订, 账单）：预付 amount、无房费凭证。"""
        reservation, folio = self._make_other_reservation_folio(self.prop, booking_no=booking_no)
        Payment.objects.create(
            property=self.prop,
            reservation=reservation,
            folio=folio,
            provider="mock",
            provider_ref=f"{booking_no}-PREPAID",
            method="MOCK",
            amount=_money(amount),
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            paid_at=timezone.now(),
        )
        LedgerEntry.objects.create(
            property=self.prop,
            folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=-_money(amount),
            currency="CNY",
            business_date=timezone.localdate(),
            reference=f"payment:{booking_no}-PREPAID",
            description="预付",
        )
        return reservation, folio

    def test_wb112_same_folio_two_keys_second_rejected(self):
        """WB-112：同 Folio 两次不同幂等键申请 → 第二个被拒（在途唯一）。"""
        self._register_blocked()
        request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="第一笔在途申请（WB-112）",
            requested_by=self.front,
            idempotency_key="wb112-key-a",
        )
        with self.assertRaises(ValueError) as ctx:
            request_exception_release(
                property_obj=self.prop,
                reservation=self.reservation,
                folio=self.folio,
                amount="600.00",
                reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                reason_text="第二笔同 Folio 申请（WB-112）",
                requested_by=self.front,
                idempotency_key="wb112-key-b",
            )
        self.assertIn("在途", str(ctx.exception))
        self.assertEqual(
            CheckoutExceptionRelease.objects.filter(
                folio=self.folio, status=CheckoutExceptionRelease.Status.REQUESTED
            ).count(),
            1,
        )

    def test_wb112_same_key_replay_returns_same_record(self):
        """WB-112：同幂等键重放返回原记录，不新建申请。"""
        self._register_blocked()
        first, created = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="幂等重放（WB-112）",
            requested_by=self.front,
            idempotency_key="wb112-replay",
        )
        self.assertTrue(created)
        replayed, created2 = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="幂等重放（WB-112）",
            requested_by=self.front,
            idempotency_key="wb112-replay",
        )
        self.assertFalse(created2)
        self.assertEqual(replayed.pk, first.pk)
        self.assertEqual(CheckoutExceptionRelease.objects.filter(folio=self.folio).count(), 1)

    def test_wb112_different_folios_do_not_block_each_other(self):
        """WB-112：不同 Folio 各自申请互不影响（在途隔离按 Folio 判定）。"""
        self._register_blocked()
        release_a, created_a = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="Folio A 在途申请",
            requested_by=self.front,
            idempotency_key="wb112-folio-a",
        )
        self.assertTrue(created_a)
        other_reservation, other_folio = self._make_folio_with_negative_balance("WB112OTHER1")
        release_b, created_b = request_exception_release(
            property_obj=self.prop,
            reservation=other_reservation,
            folio=other_folio,
            amount="500.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="Folio B 独立申请",
            requested_by=self.front,
            idempotency_key="wb112-folio-b",
        )
        self.assertTrue(created_b)
        self.assertNotEqual(release_a.folio_id, release_b.folio_id)
        self.assertEqual(
            CheckoutExceptionRelease.objects.filter(
                status=CheckoutExceptionRelease.Status.REQUESTED
            ).count(),
            2,
        )

    def test_wb112_rejected_release_allows_new_request(self):
        """WB-112：已驳回（终态）后可再次申请（换新幂等键），既有语义确认。"""
        self._register_blocked()
        first, _ = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="将被驳回的申请",
            requested_by=self.front,
            idempotency_key="wb112-reject-1",
        )
        reject_exception_release(
            property_obj=self.prop,
            release_id=first.pk,
            rejected_by=self.duty,
            reason="材料不足驳回",
        )
        second, created = request_exception_release(
            property_obj=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            amount="600.00",
            reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
            reason_text="驳回后重新申请",
            requested_by=self.front,
            idempotency_key="wb112-reject-1",
        )
        self.assertTrue(created)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.status, CheckoutExceptionRelease.Status.REQUESTED)
        first.refresh_from_db()
        self.assertEqual(first.status, CheckoutExceptionRelease.Status.REJECTED)


@override_settings(DEBUG=True, DJANGO_ENV="test")
@unittest.skipUnless(
    connection.vendor == "mysql",
    "WB-112 并发申请依赖 MySQL Folio 行锁（select_for_update 在 SQLite 下为 "
    "no-op），SQLite 测试环境跳过。",
)
class WB112ExceptionReleaseConcurrencyTests(TransactionTestCase):
    """WB-112 可选并发用例：双线程同 Folio 申请 → 最多一笔在途。"""

    def setUp(self):
        from apps.guests.models import Guest

        self.prop = Property.objects.create(code="WB112CC", name="放行并发测试酒店")
        self.user = User.objects.create_user(username="wb112cc-front")
        self.guest = Guest.objects.create(property=self.prop, full_name="并发放行客人")
        self.reservation = Reservation.objects.create(
            property=self.prop,
            guest=self.guest,
            booking_no="WB112CC001",
            status=Reservation.Status.CHECKED_IN,
            check_in=timezone.localdate() - timedelta(days=1),
            check_out=timezone.localdate(),
            room_count=1,
            total_amount=_money("600.00"),
            currency="CNY",
        )
        self.folio = Folio.objects.create(
            property=self.prop, reservation=self.reservation, guest=self.guest, currency="CNY"
        )
        Payment.objects.create(
            property=self.prop,
            reservation=self.reservation,
            folio=self.folio,
            provider="mock",
            provider_ref="WB112CC-PREPAID",
            method="MOCK",
            amount=_money("1200.00"),
            currency="CNY",
            status=Payment.Status.SUCCEEDED,
            paid_at=timezone.now(),
        )
        LedgerEntry.objects.create(
            property=self.prop,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=-_money("1200.00"),
            currency="CNY",
            business_date=timezone.localdate(),
            reference="payment:WB112CC-PREPAID",
            description="预付",
        )

    def test_two_threads_same_folio_leave_single_active_request(self):
        from django.db import connections as db_connections

        results = {}

        def _request(key):
            try:
                release, created = request_exception_release(
                    property_obj=self.prop,
                    reservation=self.reservation,
                    folio=self.folio,
                    amount="600.00",
                    reason_code=CheckoutExceptionRelease.ReasonCode.OTHER,
                    reason_text=f"并发申请 {key}",
                    requested_by=self.user,
                    idempotency_key=key,
                )
                results[key] = created
            except ValueError as exc:
                results[key] = f"rejected:{exc}"
            finally:
                db_connections["default"].close()

        threads = [threading.Thread(target=_request, args=(f"wb112cc-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            self.assertFalse(thread.is_alive(), "并发申请线程超时未结束")

        created_flags = [v for v in results.values() if v is True]
        self.assertEqual(len(created_flags), 1, f"并发结果异常：{results}")
        self.assertEqual(CheckoutExceptionRelease.objects.filter(folio=self.folio).count(), 1)
        self.assertEqual(
            CheckoutExceptionRelease.objects.filter(
                folio=self.folio, status=CheckoutExceptionRelease.Status.REQUESTED
            ).count(),
            1,
        )
