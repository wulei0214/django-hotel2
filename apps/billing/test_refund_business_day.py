"""DEC-RF-01 退款营业日归属测试固化（R1-02/R1-03，任务书 V1.0 第二批）。

行为固化（白盒回归，不改任何实现）：

  * DEC-RF-01 ① 退款凭证 business_date = BillingService._business_date
    （执行时物业时区营业日）；
  * DEC-RF-01 ⑤/WB-079 目标营业日已关账（BusinessDate.Status.CLOSED 记录
    存在）→ execute_refund 抛错、Refund 保持 APPROVED 待处理状态（无
    REFUND 凭证、无执行人/执行时间/执行幂等键残留）；
  * DEC-RF-01「次日开放营业日执行」：营业日推进（新 OPEN 营业日建立）后，
    同一退款重试执行成功，REFUND 凭证归属新营业日；
  * 原订单/原支付/原营业日经 refund.payment_id + REFUND 凭证 reference
    （refund:{provider_ref}）可溯；
  * execution_idempotency_key 幂等（同键重放返回已执行事实、不重复入账；
    不同键重放明确拒绝）。

夹具纪律（对齐 apps/billing/test_cashier_reconcile.py）：
  * 支付/退款/凭证一律经 BillingService 真实服务构造（申请/审批/执行三方
    账号分离），不直写财务事实；
  * 营业日关账状态按任务书以简单者为先：直接构造 BusinessDate.Status
    记录（等价夜审 run 后的终态）；
  * 「次日」推进用 mock.patch 冻结 django.utils.timezone.now + 1 天，使
    _business_date 返回下一营业日（营业日推进语义的事实源）。

口径注记（重要，防止未来误改）：WB-079 关账守卫的暂停条件是「目标营业日
记录为 CLOSED」；营业日完全无记录（白天未到夜审时点的正常态）按隐式开放
放行、不暂停——test_no_open_business_day_pauses_execution 固化的是
「物业全部营业日记录均已关账、无任何 OPEN 日、次日未建」的暂停形态。
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry, Payment, Refund
from apps.billing.services_v2 import BillingService
from apps.core.models import BusinessDate, Property
from apps.guests.models import Guest
from apps.inventory.models import RatePlan, RoomType
from apps.reservations.models import Reservation, ReservationRoom


@override_settings(DEBUG=True, DJANGO_ENV="test")
class RefundBusinessDayTestBase(TestCase):
    """最小夹具：物业 + 申请/审批/执行三方分离账号 + 真实收款构造器。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.create(
            code="RBD", name="退款营业日归属酒店", timezone="Asia/Shanghai")
        cls.requester = User.objects.create_user(username="rbd-requester")
        cls.approver = User.objects.create_user(username="rbd-approver")
        cls.executor = User.objects.create_user(username="rbd-executor")
        cls.guest = Guest.objects.create(property=cls.prop, full_name="退款营业日客人")

    def setUp(self):
        self.today = BillingService._business_date(self.prop)

    # ------------------------------------------------------------------
    # 夹具构造器（全部经真实 Service / 状态合法创建）
    # ------------------------------------------------------------------
    def _make_folio(self, booking_no, total="600.00"):
        """CHECKED_IN 预订 + 房价明细 + 开放主账单，返回 (reservation, folio)。"""
        check_in = self.today
        check_out = check_in + timedelta(days=1)
        suffix = booking_no[-4:]
        room_type = RoomType.objects.create(
            property=self.prop, code=f"RBD-{suffix}", name="退款营业日房型",
            capacity=2)
        rate_plan = RatePlan.objects.create(
            property=self.prop, code=f"RBD-BAR-{suffix}", name="退款营业日门市价",
            valid_from=check_in)
        reservation = Reservation.objects.create(
            property=self.prop, guest=self.guest, booking_no=booking_no,
            status=Reservation.Status.CHECKED_IN,
            check_in=check_in, check_out=check_out,
            room_count=1, adult_count=1, total_amount=Decimal(total),
            currency="CNY")
        ReservationRoom.objects.create(
            property=self.prop, reservation=reservation,
            room_type=room_type, rate_plan=rate_plan,
            check_in=check_in, check_out=check_out,
            nightly_rates=[{"date": check_in.isoformat(),
                            "price": total, "currency": "CNY"}],
            amount=Decimal(total))
        folio = Folio.objects.create(
            property=self.prop, reservation=reservation, guest=self.guest,
            currency="CNY")
        return reservation, folio

    def _collect(self, reservation, provider_ref, amount="600.00", method="CARD"):
        """真实收款（BillingService.collect_payment）：Payment + PAYMENT 凭证。"""
        return BillingService.collect_payment(
            property_obj=self.prop, reservation=reservation,
            amount=amount, method=method, collected_by=self.requester,
            reference=provider_ref)

    def _approved_refund(self, payment, amount, *, reason, request_key):
        """真实退款申请+审批：Refund APPROVED（申请/审批人分离）。"""
        refund, created = BillingService.request_refund(
            property_obj=self.prop, payment_id=payment.pk,
            amount=amount, reason=reason,
            requested_by=self.requester, idempotency_key=request_key)
        self.assertTrue(created)
        BillingService.approve_refund(
            property_obj=self.prop, refund_id=refund.pk,
            approved_by=self.approver)
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)
        return refund

    def _execute(self, refund, *, execution_reference, idempotency_key):
        return BillingService.execute_refund(
            property_obj=self.prop, refund_id=refund.pk,
            executed_by=self.executor, confirm_refund_paid=True,
            execution_reference=execution_reference,
            idempotency_key=idempotency_key)

    def _open_next_day(self):
        """模拟营业日推进：时钟 +1 天后 get_or_create 新 OPEN 营业日。"""
        patcher = mock.patch(
            "django.utils.timezone.now",
            return_value=timezone.now() + timedelta(days=1))
        now_patched = patcher.start()
        self.addCleanup(patcher.stop)
        next_day = BillingService._business_date(self.prop)
        self.assertEqual(next_day, self.today + timedelta(days=1),
                         "时钟 +1 天后营业日必须推进到次日")
        BusinessDate.objects.get_or_create(
            property=self.prop, business_date=next_day,
            defaults={"status": BusinessDate.Status.OPEN})
        return next_day

    def _refund_entry(self, refund):
        return LedgerEntry.objects.get(reference=f"refund:{refund.provider_ref}")


class RefundBusinessDayAttributionTests(RefundBusinessDayTestBase):
    """DEC-RF-01 六项行为固化（R1-02 次日归属 / R1-03 回归补齐）。"""

    # ------------------------------------------------------------------
    # ① OPEN 营业日执行：SUCCEEDED + REFUND 凭证归属当前营业日
    # ------------------------------------------------------------------
    def test_refund_executes_on_open_business_day(self):
        reservation, folio = self._make_folio("RBD-OPEN-001")
        BusinessDate.objects.create(
            property=self.prop, business_date=self.today,
            status=BusinessDate.Status.OPEN)
        payment = self._collect(reservation, "RBD-OPEN-PAY-1")
        refund = self._approved_refund(
            payment, "200.00", reason="营业日归属回归退款", request_key="rbd-open-req-1")

        executed, created = self._execute(
            refund, execution_reference="BANK-RBD-OPEN-1",
            idempotency_key="rbd-open-exec-1")

        self.assertTrue(created)
        self.assertEqual(executed.status, Refund.Status.SUCCEEDED)
        self.assertEqual(executed.executed_by_id, self.executor.pk)
        entry = self._refund_entry(executed)
        self.assertEqual(entry.entry_type, LedgerEntry.EntryType.REFUND)
        self.assertEqual(entry.business_date, self.today,
                         "DEC-RF-01①：REFUND 凭证 business_date=执行时营业日")
        self.assertEqual(entry.amount, Decimal("200.00"))
        self.assertEqual(entry.folio_id, folio.pk)

    # ------------------------------------------------------------------
    # ② 当日已关账：执行被拒，Refund 保持 APPROVED 待处理（DEC-RF-01⑤）
    # ------------------------------------------------------------------
    def test_refund_rejected_on_closed_day_keeps_pending_state(self):
        reservation, folio = self._make_folio("RBD-CLOSE-002")
        payment = self._collect(reservation, "RBD-CLOSE-PAY-2")
        refund = self._approved_refund(
            payment, "150.00", reason="关账日暂停退款回归", request_key="rbd-close-req-2")
        # 先关当日账（等价夜审 run 后的终态；任务书口径：直接构造 CLOSED 记录）
        BusinessDate.objects.create(
            property=self.prop, business_date=self.today,
            status=BusinessDate.Status.CLOSED)

        with self.assertRaises(ValueError) as ctx:
            self._execute(
                refund, execution_reference="BANK-RBD-CLOSE-2",
                idempotency_key="rbd-close-exec-2")
        self.assertIn("已关账", str(ctx.exception))

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED,
                         "DEC-RF-01⑤：关账拒执行后待处理状态保留，不得吞单")
        self.assertIsNone(refund.executed_by_id)
        self.assertIsNone(refund.executed_at)
        self.assertIsNone(refund.execution_idempotency_key)
        self.assertFalse(
            LedgerEntry.objects.filter(
                reference=f"refund:{refund.provider_ref}").exists(),
            "关账日不得产生 REFUND 凭证")

    # ------------------------------------------------------------------
    # ③ 关账次日：同一退款重试执行成功，凭证归属新营业日
    # ------------------------------------------------------------------
    def test_refund_executes_next_day_after_business_day_advances(self):
        reservation, folio = self._make_folio("RBD-NEXT-003")
        payment = self._collect(reservation, "RBD-NEXT-PAY-3")
        refund = self._approved_refund(
            payment, "300.00", reason="次日重试退款回归", request_key="rbd-next-req-3")
        BusinessDate.objects.create(
            property=self.prop, business_date=self.today,
            status=BusinessDate.Status.CLOSED)
        with self.assertRaises(ValueError):
            self._execute(
                refund, execution_reference="BANK-RBD-NEXT-3",
                idempotency_key="rbd-next-exec-3")
        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED)

        # 营业日推进：次日 OPEN 营业日建立（get_or_create），同一退款重试
        next_day = self._open_next_day()
        executed, created = self._execute(
            refund, execution_reference="BANK-RBD-NEXT-3",
            idempotency_key="rbd-next-exec-3")
        self.assertTrue(created)
        self.assertEqual(executed.status, Refund.Status.SUCCEEDED)
        entry = self._refund_entry(executed)
        self.assertEqual(entry.business_date, next_day,
                         "DEC-RF-01：次日开放营业日执行，凭证归属新营业日")
        self.assertNotEqual(entry.business_date, self.today)

    # ------------------------------------------------------------------
    # ④ 账本归属可追溯：REFUND 凭证 reference 反查退款→原支付→原订单
    #    →原收款凭证（原营业日），与退款执行营业日形成归属对照
    # ------------------------------------------------------------------
    def test_refund_ledger_attribution_traceability(self):
        reservation, folio = self._make_folio("RBD-TRACE-004")
        payment = self._collect(reservation, "RBD-TRACE-PAY-4")
        refund = self._approved_refund(
            payment, "120.00", reason="退款凭证追溯回归", request_key="rbd-trace-req-4")
        next_day = self._open_next_day()
        executed, created = self._execute(
            refund, execution_reference="BANK-RBD-TRACE-4",
            idempotency_key="rbd-trace-exec-4")
        self.assertTrue(created)

        refund_entry = self._refund_entry(executed)
        # REFUND 凭证 reference = refund:{provider_ref} → 反查退款事实
        prefix = "refund:"
        self.assertTrue(refund_entry.reference.startswith(prefix))
        traced = Refund.objects.get(provider_ref=refund_entry.reference[len(prefix):])
        self.assertEqual(traced.pk, refund.pk)
        # 退款 → 原支付 → 原订单（同一 folio/reservation 事实链）
        traced_payment = traced.payment
        self.assertEqual(traced_payment.pk, payment.pk)
        self.assertEqual(traced_payment.folio_id, folio.pk)
        self.assertEqual(traced_payment.reservation_id, reservation.pk)
        # 原收款凭证归属原营业日（收款日），与退款执行营业日可对照
        payment_entry = LedgerEntry.objects.get(
            reference=f"payment:{payment.provider_ref}")
        self.assertEqual(payment_entry.entry_type, LedgerEntry.EntryType.PAYMENT)
        self.assertEqual(payment_entry.business_date, self.today,
                         "原收款凭证 business_date=原营业日，经 reference 可溯")
        self.assertEqual(refund_entry.business_date, next_day)
        self.assertNotEqual(
            payment_entry.business_date, refund_entry.business_date,
            "原营业日（收款日）与退款执行营业日分离且均可追溯")

    # ------------------------------------------------------------------
    # ⑤ 执行幂等回归：同键重放返回已执行事实，不重复入账；异键拒绝
    # ------------------------------------------------------------------
    def test_execution_idempotency_same_key_replay(self):
        reservation, folio = self._make_folio("RBD-IDEM-005")
        payment = self._collect(reservation, "RBD-IDEM-PAY-5")
        refund = self._approved_refund(
            payment, "88.00", reason="退款执行幂等回归", request_key="rbd-idem-req-5")

        first, created = self._execute(
            refund, execution_reference="BANK-RBD-IDEM-5",
            idempotency_key="rbd-idem-exec-5")
        self.assertTrue(created)
        self.assertEqual(first.status, Refund.Status.SUCCEEDED)

        replay, replay_created = self._execute(
            refund, execution_reference="BANK-RBD-IDEM-5",
            idempotency_key="rbd-idem-exec-5")
        self.assertFalse(replay_created)
        self.assertEqual(replay.pk, first.pk)
        self.assertEqual(replay.status, Refund.Status.SUCCEEDED)
        # 幂等重放不重复追加凭证（账面=PAYMENT + REFUND 各一笔）
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=f"refund:{first.provider_ref}").count(), 1)
        self.assertEqual(
            LedgerEntry.objects.filter(folio=folio).count(), 2)

        # 已执行退款换幂等键重放 → 明确拒绝（防错键覆盖既有执行事实）
        with self.assertRaises(ValueError) as ctx:
            self._execute(
                refund, execution_reference="BANK-RBD-IDEM-5B",
                idempotency_key="rbd-idem-exec-5b")
        self.assertIn("不能使用其他幂等键重复执行", str(ctx.exception))

    # ------------------------------------------------------------------
    # ⑥ 无 OPEN 营业日（全部记录已关账、次日未建）→ 执行暂停、APPROVED 保留
    # ------------------------------------------------------------------
    def test_no_open_business_day_pauses_execution(self):
        reservation, folio = self._make_folio("RBD-PAUSE-006")
        payment = self._collect(reservation, "RBD-PAUSE-PAY-6")
        refund = self._approved_refund(
            payment, "66.00", reason="无开放营业日暂停回归", request_key="rbd-pause-req-6")
        # 物业全部营业日记录均已关账（前一日+当日），次日营业日未建：
        # 不存在任何 OPEN 营业日 → 退款执行暂停（DEC-RF-01⑤/WB-079）
        BusinessDate.objects.create(
            property=self.prop, business_date=self.today - timedelta(days=1),
            status=BusinessDate.Status.CLOSED)
        BusinessDate.objects.create(
            property=self.prop, business_date=self.today,
            status=BusinessDate.Status.CLOSED)
        self.assertFalse(
            BusinessDate.objects.filter(
                property=self.prop, status=BusinessDate.Status.OPEN).exists(),
            "前置自证：物业无任何 OPEN 营业日记录")

        with self.assertRaises(ValueError) as ctx:
            self._execute(
                refund, execution_reference="BANK-RBD-PAUSE-6",
                idempotency_key="rbd-pause-exec-6")
        self.assertIn("已关账", str(ctx.exception))

        refund.refresh_from_db()
        self.assertEqual(refund.status, Refund.Status.APPROVED,
                         "暂停期间 APPROVED 待处理状态保留，待次日开放后重试")
        self.assertFalse(
            LedgerEntry.objects.filter(
                folio=folio, entry_type=LedgerEntry.EntryType.REFUND).exists())
