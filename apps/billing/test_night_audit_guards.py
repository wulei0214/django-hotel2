# -*- coding: utf-8 -*-
"""白盒修复回归：夜审/账本守卫（WB-079/080/081/082/083/109）。

覆盖：
  * WB-079：post_entry 营业日关账守卫（CLOSED 拒绝新增；OPEN/无记录放行；
    同事实重放幂等不受影响；夜审 run 过租在关账前完成不受阻断）；
  * WB-080：夜审日期必须等于"当前物业唯一 OPEN/REOPENED 营业日"，
    未来/历史/无记录日期拒绝且不自动创建 BusinessDate；
  * WB-081：preview.stay_count 与过租窗口（check_in <= day < check_out）同口径；
  * WB-082：already_posted 内容一致判定；同引用不同事实 → 账务冲突 blocker；
  * WB-083：LedgerEntry.save() 只追加守卫（实例级不可变），QuerySet 层
    守卫行为保持不变（test_dec02 孤儿化 helper 的原生 QuerySet 旁路兼容）；
  * WB-109：夜审与退房并发仲裁——run() 过账事务内在写入任何房费前按主键
    序对涉及 Stay select_for_update()，只处理锁定后仍 IN_HOUSE 且住宿区间
    覆盖营业日的 Stay（锁序 Stay→ReservationRoom→Reservation→Folio 与
    check_out_stay 一致）。顺序语义用例 SQLite 可跑；真实行锁并发用例
    （TransactionTestCase + 双线程）仅 MySQL 执行，SQLite 跳过。

自建物业/房型/预订/Stay/Folio，不依赖 DEFAULT 物业已有数据。
"""
import threading
import time as time_module
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.db.models import QuerySet
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.billing.models import Folio, LedgerEntry, Payment
from apps.billing.night_audit import (
    NightAuditError, NightAuditService, property_date,
)
from apps.billing.services_v2 import BillingService
from apps.core.models import BusinessDate, NightAuditRun, Property
from apps.guests.models import Guest
from apps.inventory.models import RatePlan, Room, RoomType
from apps.reservations.front_desk_services import check_out_stay
from apps.reservations.models import Reservation, ReservationRoom, Stay


class NightAuditGuardBase(TestCase):
    """每用例自建物业与全链路在住数据，避免依赖 DEFAULT 物业。"""

    def setUp(self):
        self.property = Property.objects.create(
            code="NGD", name="夜审守卫测试酒店")
        self.today = property_date(self.property)
        self.operator = User.objects.create_user(username="ngd-operator")
        self.guest = Guest.objects.create(
            property=self.property, full_name="守卫测试客人",
            phone="13800009901", phone_normalized="13800009901")

    def _make_inhouse_stay(self, booking_no, price="400.00", check_in=None,
                           nights=2, room_number=None):
        """创建 CHECKED_IN 预订 + 在住 Stay + 未结主账单，返回 (stay, folio, line)。"""
        check_in = check_in if check_in is not None else self.today
        check_out = check_in + timedelta(days=nights)
        suffix = booking_no[-4:]
        room_type = RoomType.objects.create(
            property=self.property, code=f"NGD-{suffix}", name="守卫测试房型")
        rate_plan = RatePlan.objects.create(
            property=self.property, code=f"NGD-BAR-{suffix}",
            name="守卫测试门市价", valid_from=check_in)
        nightly = [
            {"date": (check_in + timedelta(days=i)).isoformat(),
             "price": price, "currency": "CNY"}
            for i in range(nights)
        ]
        reservation = Reservation.objects.create(
            property=self.property, booking_no=booking_no, guest=self.guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=check_in, check_out=check_out,
            room_count=1, adult_count=1,
            total_amount=Decimal(price) * nights)
        line = ReservationRoom.objects.create(
            property=self.property, reservation=reservation,
            room_type=room_type, rate_plan=rate_plan,
            check_in=check_in, check_out=check_out,
            nightly_rates=nightly, amount=Decimal(price) * nights)
        room = Room.objects.create(
            property=self.property, room_type=room_type,
            number=room_number or f"NGD-{suffix}",
            occupancy_status=Room.OccupancyStatus.OCCUPIED)
        stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=room,
            status=Stay.Status.IN_HOUSE)
        folio = Folio.objects.create(
            property=self.property, reservation=reservation,
            guest=self.guest, currency="CNY")
        return stay, folio, line

    def _night_reference(self, stay):
        return (
            f"night-audit:{self.property.pk}:{self.today.isoformat()}"
            f":stay:{stay.pk}")

    def _post_night_charge(self, stay, folio, amount, reference=None,
                           entry_type=None, business_date=None,
                           currency="CNY"):
        return BillingService.post_entry(
            property_obj=self.property, folio=folio, stay=stay,
            entry_type=(entry_type or LedgerEntry.EntryType.ROOM_CHARGE),
            amount=Decimal(amount), currency=currency,
            business_date=(business_date if business_date is not None
                           else self.today),
            reference=reference or self._night_reference(stay),
            posted_by=self.operator)


class PostEntryClosedDayGuardTests(NightAuditGuardBase):
    """WB-079：post_entry 营业日关账守卫。"""

    def test_post_entry_rejects_closed_business_date(self):
        BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.CLOSED)
        stay, folio, _ = self._make_inhouse_stay("NGD079A")
        with self.assertRaises(ValueError) as ctx:
            self._post_night_charge(stay, folio, "50.00",
                                    reference="NGD079-REF-1")
        self.assertIn("已关账", str(ctx.exception))
        self.assertIn("补记", str(ctx.exception))
        self.assertIn("授权流程", str(ctx.exception))
        self.assertFalse(
            LedgerEntry.objects.filter(reference="NGD079-REF-1").exists())

    def test_post_entry_allows_open_or_unrecorded_business_date(self):
        stay, folio, _ = self._make_inhouse_stay("NGD079B")
        BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.OPEN)
        entry, created = self._post_night_charge(
            stay, folio, "50.00", reference="NGD079-REF-2")
        self.assertTrue(created)
        # 营业日无记录（如未来日/首日）不视为 CLOSED，不拦截；也不创建记录
        future_day = self.today + timedelta(days=3)
        entry2, created2 = self._post_night_charge(
            stay, folio, "60.00", reference="NGD079-REF-3",
            business_date=future_day)
        self.assertTrue(created2)
        self.assertFalse(BusinessDate.objects.filter(
            business_date=future_day).exists())

    def test_same_facts_replay_after_close_stays_idempotent(self):
        stay, folio, _ = self._make_inhouse_stay("NGD079C")
        entry, created = self._post_night_charge(
            stay, folio, "50.00", reference="NGD079-REF-4")
        self.assertTrue(created)
        BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.CLOSED)
        replay_entry, replay_created = self._post_night_charge(
            stay, folio, "50.00", reference="NGD079-REF-4")
        self.assertFalse(replay_created)
        self.assertEqual(replay_entry.pk, entry.pk)
        self.assertEqual(LedgerEntry.objects.count(), 1)

    def _make_succeeded_payment(self, folio, provider_ref, amount="600.00",
                                status=Payment.Status.SUCCEEDED,
                                property_obj=None):
        """构造渠道回调场景的 Payment 资金事实（默认成功、CNY）。"""
        return Payment.objects.create(
            property=property_obj or self.property,
            reservation=folio.reservation, folio=folio,
            provider="mock", provider_ref=provider_ref, method="MOCK",
            amount=Decimal(amount), currency="CNY",
            status=status, paid_at=timezone.now())

    def test_channel_payment_callback_after_close_is_exempt(self):
        """合法链路豁免（WB-111）：渠道回调 PAYMENT + 显式 channel_payment
        （归属/状态/币种/金额/引用一致）关账后仍可入账。"""
        stay, folio, _ = self._make_inhouse_stay("NGD079E")
        payment = self._make_succeeded_payment(folio, "NGD079-CHANNEL-001")
        BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.CLOSED)
        entry, created = BillingService.post_entry(
            property_obj=self.property, folio=folio, stay=stay,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-600.00"), currency="CNY",
            business_date=self.today,
            reference="payment:NGD079-CHANNEL-001",
            description="渠道支付成功回调入账", posted_by=None,
            channel_payment=payment)
        self.assertTrue(created)
        self.assertEqual(entry.amount, Decimal("-600.00"))
        # 员工发起的收款凭证仍受守卫拦截（posted_by 非空）
        with self.assertRaises(ValueError):
            BillingService.post_entry(
                property_obj=self.property, folio=folio, stay=stay,
                entry_type=LedgerEntry.EntryType.PAYMENT,
                amount=Decimal("-100.00"), currency="CNY",
                business_date=self.today,
                reference="payment:NGD079-MANUAL-001",
                posted_by=self.operator)
        self.assertFalse(LedgerEntry.objects.filter(
            reference="payment:NGD079-MANUAL-001").exists())

    # ------------------------------------------------------------------
    # WB-111：关账日渠道支付豁免收紧——拒绝分支
    # ------------------------------------------------------------------
    def _closed_day(self):
        BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.CLOSED)

    def _post_channel_payment(self, folio, payment, *, amount="-600.00",
                              reference=None, channel_payment=True):
        return BillingService.post_entry(
            property_obj=self.property, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal(amount), currency="CNY",
            business_date=self.today,
            reference=(reference if reference is not None
                       else f"payment:{payment.provider_ref}"),
            description="渠道支付成功回调入账", posted_by=None,
            channel_payment=(payment if channel_payment else None))

    def test_closed_day_payment_without_channel_payment_rejected(self):
        """WB-111：CLOSED 日 posted_by=None 的 PAYMENT 未传 channel_payment → 拒绝。"""
        stay, folio, _ = self._make_inhouse_stay("NGD111A")
        self._closed_day()
        with self.assertRaises(ValueError) as ctx:
            self._post_channel_payment(
                folio, None, reference="payment:NGD111A-NO-PAYMENT",
                channel_payment=False)
        self.assertIn("已关账", str(ctx.exception))
        self.assertIn("channel_payment", str(ctx.exception))
        self.assertFalse(LedgerEntry.objects.filter(
            reference="payment:NGD111A-NO-PAYMENT").exists())

    def test_closed_day_channel_payment_cross_property_rejected(self):
        """WB-111：channel_payment 属于其他物业 → 拒绝。"""
        stay, folio, _ = self._make_inhouse_stay("NGD111B")
        other_property = Property.objects.create(
            code="NGD111-P", name="WB-111 跨物业测试酒店")
        payment = self._make_succeeded_payment(
            folio, "NGD111-CROSS-PROP", property_obj=other_property)
        self._closed_day()
        with self.assertRaises(ValueError) as ctx:
            self._post_channel_payment(folio, payment)
        self.assertIn("归属不一致", str(ctx.exception))
        self.assertFalse(LedgerEntry.objects.filter(
            reference="payment:NGD111-CROSS-PROP").exists())

    def test_closed_day_channel_payment_cross_folio_rejected(self):
        """WB-111：channel_payment 挂在其他账单（同物业）→ 拒绝。"""
        _, own_folio, _ = self._make_inhouse_stay("NGD111C1")
        _, other_folio, _ = self._make_inhouse_stay("NGD111C2")
        payment = self._make_succeeded_payment(
            other_folio, "NGD111-CROSS-FOLIO")
        self._closed_day()
        with self.assertRaises(ValueError) as ctx:
            self._post_channel_payment(own_folio, payment)
        self.assertIn("归属不一致", str(ctx.exception))
        self.assertFalse(LedgerEntry.objects.filter(
            reference="payment:NGD111-CROSS-FOLIO").exists())

    def test_closed_day_channel_payment_amount_mismatch_rejected(self):
        """WB-111：凭证金额与 channel_payment 金额不符 → 拒绝。"""
        stay, folio, _ = self._make_inhouse_stay("NGD111D")
        payment = self._make_succeeded_payment(folio, "NGD111-AMT")
        self._closed_day()
        with self.assertRaises(ValueError) as ctx:
            self._post_channel_payment(folio, payment, amount="-500.00")
        self.assertIn("金额", str(ctx.exception))
        self.assertFalse(LedgerEntry.objects.filter(
            reference="payment:NGD111-AMT").exists())

    def test_closed_day_channel_payment_pending_status_rejected(self):
        """WB-111：channel_payment 非 SUCCEEDED（如 PENDING）→ 拒绝。"""
        stay, folio, _ = self._make_inhouse_stay("NGD111E")
        payment = self._make_succeeded_payment(
            folio, "NGD111-PENDING", status=Payment.Status.PENDING)
        self._closed_day()
        with self.assertRaises(ValueError) as ctx:
            self._post_channel_payment(folio, payment)
        self.assertIn("SUCCEEDED", str(ctx.exception))
        self.assertFalse(LedgerEntry.objects.filter(
            reference="payment:NGD111-PENDING").exists())

    def test_closed_day_channel_payment_reference_mismatch_rejected(self):
        """WB-111：凭证引用与 channel_payment 渠道流水号不一致 → 拒绝。"""
        stay, folio, _ = self._make_inhouse_stay("NGD111F")
        payment = self._make_succeeded_payment(folio, "NGD111-REF")
        self._closed_day()
        with self.assertRaises(ValueError) as ctx:
            self._post_channel_payment(folio, payment,
                                       reference="payment:NGD111-OTHER")
        self.assertIn("渠道流水号", str(ctx.exception))
        self.assertFalse(LedgerEntry.objects.filter(
            reference="payment:NGD111-REF").exists())

    def test_night_audit_run_posts_charges_before_closing_passes_guard(self):
        """合法链路回归：run() 在同一事务内先过租后关账，不被 WB-079 阻断。"""
        stay, folio, _ = self._make_inhouse_stay("NGD079D")
        run, created = NightAuditService.run(
            property_obj=self.property, executed_by=self.operator,
            business_date=self.today)
        self.assertTrue(created)
        self.assertEqual(run.status, NightAuditRun.RunStatus.SUCCESS)
        self.assertTrue(LedgerEntry.objects.filter(
            reference=self._night_reference(stay),
            entry_type=LedgerEntry.EntryType.ROOM_CHARGE).exists())
        bd = BusinessDate.objects.get(
            property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)


class NightAuditDateScopeTests(NightAuditGuardBase):
    """WB-080：夜审只允许当前唯一 OPEN/REOPENED 营业日。"""

    def test_run_rejects_future_date_without_creating_business_date(self):
        stay, folio, _ = self._make_inhouse_stay("NGD080A")
        future_day = self.today + timedelta(days=3)
        with self.assertRaises(NightAuditError) as ctx:
            NightAuditService.run(
                property_obj=self.property, executed_by=self.operator,
                business_date=future_day)
        self.assertIn("当前营业日", str(ctx.exception))
        self.assertFalse(BusinessDate.objects.filter(
            business_date=future_day).exists())
        self.assertFalse(BusinessDate.objects.filter(
            property=self.property).exists())
        self.assertFalse(LedgerEntry.objects.exists())
        self.assertFalse(NightAuditRun.objects.exists())

    def test_run_rejects_history_date_without_open_business_date(self):
        stay, folio, _ = self._make_inhouse_stay("NGD080B")
        closed = BusinessDate.objects.create(
            property=self.property,
            business_date=self.today - timedelta(days=1),
            status=BusinessDate.Status.CLOSED)
        with self.assertRaises(NightAuditError) as ctx:
            NightAuditService.run(
                property_obj=self.property, executed_by=self.operator,
                business_date=closed.business_date)
        self.assertIn("当前营业日", str(ctx.exception))
        closed.refresh_from_db()
        self.assertEqual(closed.status, BusinessDate.Status.CLOSED)
        # 不再自动创建当日 BusinessDate / 夜审记录 / 凭证
        self.assertFalse(BusinessDate.objects.filter(
            business_date=self.today).exists())
        self.assertFalse(NightAuditRun.objects.exists())
        self.assertFalse(LedgerEntry.objects.exists())

    def test_run_rejects_date_mismatching_open_business_date(self):
        stay, folio, _ = self._make_inhouse_stay("NGD080C")
        stale = BusinessDate.objects.create(
            property=self.property,
            business_date=self.today - timedelta(days=1),
            status=BusinessDate.Status.OPEN)
        with self.assertRaises(NightAuditError) as ctx:
            NightAuditService.run(
                property_obj=self.property, executed_by=self.operator,
                business_date=self.today)
        self.assertIn(stale.business_date.isoformat(), str(ctx.exception))
        self.assertIn("当前营业日", str(ctx.exception))
        # 与 OPEN 营业日不符的日期不再自动创建记录
        self.assertFalse(BusinessDate.objects.filter(
            business_date=self.today).exists())
        self.assertFalse(NightAuditRun.objects.exists())
        self.assertFalse(LedgerEntry.objects.exists())
        # 合法链路不受阻断：显式审计当前 OPEN 营业日可以关账
        run, created = NightAuditService.run(
            property_obj=self.property, executed_by=self.operator,
            business_date=stale.business_date)
        self.assertTrue(created)
        self.assertEqual(run.status, NightAuditRun.RunStatus.SUCCESS)
        stale.refresh_from_db()
        self.assertEqual(stale.status, BusinessDate.Status.CLOSED)

    def test_preview_rejects_dates_outside_current_business_date(self):
        BusinessDate.objects.create(
            property=self.property,
            business_date=self.today - timedelta(days=1),
            status=BusinessDate.Status.CLOSED)
        stay, folio, _ = self._make_inhouse_stay("NGD080D")
        with self.assertRaises(NightAuditError):
            NightAuditService.preview(
                property_obj=self.property,
                business_date=self.today - timedelta(days=1))
        with self.assertRaises(NightAuditError):
            NightAuditService.preview(
                property_obj=self.property,
                business_date=self.today + timedelta(days=1))
        # 当前营业日预览不受影响
        data = NightAuditService.preview(property_obj=self.property)
        self.assertEqual(data["business_date"], self.today.isoformat())
        self.assertEqual(data["stay_count"], 1)


class PreviewStayCountScopeTests(NightAuditGuardBase):
    """WB-081：stay_count 只统计覆盖目标营业日的在住 Stay。"""

    def test_stay_count_counts_only_stays_covering_business_date(self):
        stay_a, folio_a, _ = self._make_inhouse_stay(
            "NGD081A", room_number="NGD-101")
        # legacy 风格在住：check_in 晚于目标营业日，不在当晚过租窗口内
        stay_b, folio_b, _ = self._make_inhouse_stay(
            "NGD081B", check_in=self.today + timedelta(days=5), nights=2,
            room_number="NGD-102")
        data = NightAuditService.preview(property_obj=self.property)
        self.assertEqual(data["stay_count"], 1)
        self.assertEqual(data["charge_count"], 1)
        self.assertEqual(data["blocker_count"], 0)
        self.assertEqual(
            [row["stay_id"] for row in data["stays"]], [stay_a.pk])
        self.assertEqual(data["charge_total"], "400.00")


class AlreadyPostedFactMatchTests(NightAuditGuardBase):
    """WB-082：already_posted 内容一致判定与账务冲突 blocker。"""

    def test_matching_entry_is_treated_as_already_posted(self):
        stay, folio, _ = self._make_inhouse_stay("NGD082A")
        _, created = self._post_night_charge(stay, folio, "400.00")
        self.assertTrue(created)
        data = NightAuditService.preview(property_obj=self.property)
        self.assertTrue(data["stays"][0]["already_posted"])
        self.assertEqual(data["already_posted_count"], 1)
        self.assertEqual(data["charge_count"], 0)
        self.assertEqual(data["charge_total"], "0.00")
        self.assertEqual(data["blocker_count"], 0)
        self.assertTrue(data["can_run"])

    def test_amount_mismatch_becomes_critical_blocker(self):
        stay, folio, _ = self._make_inhouse_stay("NGD082B")
        self._post_night_charge(stay, folio, "350.00")
        data = NightAuditService.preview(property_obj=self.property)
        self.assertEqual(data["blocker_count"], 1)
        blocker = data["blockers"][0]
        self.assertEqual(blocker["code"], "ALREADY_POSTED_CONFLICT")
        self.assertEqual(blocker["severity"], "critical")
        self.assertEqual(blocker["entity"]["type"], "Stay")
        self.assertEqual(blocker["entity"]["id"], stay.pk)
        self.assertIn("金额", blocker["message"])
        self.assertFalse(data["can_run"])
        # 冲突阻断夜审执行，且不写任何新凭证
        entries_before = LedgerEntry.objects.count()
        with self.assertRaises(NightAuditError):
            NightAuditService.run(
                property_obj=self.property, executed_by=self.operator,
                business_date=self.today)
        self.assertEqual(LedgerEntry.objects.count(), entries_before)

    def test_business_date_mismatch_becomes_blocker(self):
        stay, folio, _ = self._make_inhouse_stay("NGD082C")
        self._post_night_charge(
            stay, folio, "400.00",
            business_date=self.today - timedelta(days=1))
        data = NightAuditService.preview(property_obj=self.property)
        self.assertEqual(data["blocker_count"], 1)
        self.assertEqual(data["blockers"][0]["code"], "ALREADY_POSTED_CONFLICT")
        self.assertIn("营业日", data["blockers"][0]["message"])
        self.assertFalse(data["can_run"])

    def test_folio_mismatch_becomes_blocker(self):
        stay, folio, _ = self._make_inhouse_stay("NGD082D1",
                                                 room_number="NGD-201")
        _, other_folio, _ = self._make_inhouse_stay("NGD082D2",
                                                    room_number="NGD-202")
        # 同引用凭证挂到了另一预订的主账单上
        self._post_night_charge(stay, other_folio, "400.00")
        data = NightAuditService.preview(property_obj=self.property)
        self.assertEqual(data["blocker_count"], 1)
        self.assertEqual(data["blockers"][0]["code"], "ALREADY_POSTED_CONFLICT")
        self.assertIn("主账单", data["blockers"][0]["message"])
        self.assertFalse(data["can_run"])

    def test_entry_type_mismatch_becomes_blocker(self):
        stay, folio, _ = self._make_inhouse_stay("NGD082E")
        self._post_night_charge(
            stay, folio, "400.00",
            entry_type=LedgerEntry.EntryType.ADJUSTMENT)
        data = NightAuditService.preview(property_obj=self.property)
        self.assertEqual(data["blocker_count"], 1)
        self.assertEqual(data["blockers"][0]["code"], "ALREADY_POSTED_CONFLICT")
        self.assertIn("凭证类型", data["blockers"][0]["message"])
        self.assertFalse(data["can_run"])

    def test_post_entry_keeps_original_error_on_facts_mismatch(self):
        """执行阶段兜底路径不变：幂等命中但事实不符 → 原有 ValueError。"""
        stay, folio, _ = self._make_inhouse_stay("NGD082F")
        self._post_night_charge(stay, folio, "400.00")
        with self.assertRaises(ValueError) as ctx:
            self._post_night_charge(stay, folio, "500.00")
        self.assertIn("已用于不同账务事实", str(ctx.exception))
        self.assertEqual(
            LedgerEntry.objects.filter(reference=self._night_reference(stay)
                                       ).count(), 1)


class LedgerEntryImmutabilityTests(NightAuditGuardBase):
    """WB-083：LedgerEntry.save() 只追加守卫与 QuerySet 层守卫共存。"""

    def _make_entry(self, reference="NGD083-1", amount="88.00"):
        stay, folio, _ = self._make_inhouse_stay("NGD083A")
        entry, created = BillingService.post_entry(
            property_obj=self.property, folio=folio, stay=stay,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal(amount), currency="CNY",
            business_date=self.today, reference=reference,
            posted_by=self.operator)
        self.assertTrue(created)
        return entry

    def test_save_rejects_modifying_loaded_instance(self):
        entry = self._make_entry()
        entry.description = "篡改摘要"
        entry.amount = Decimal("1.00")
        with self.assertRaises(ValueError) as ctx:
            entry.save()
        self.assertIn("不可变", str(ctx.exception))
        entry.refresh_from_db()
        self.assertEqual(entry.description, "")
        self.assertEqual(entry.amount, Decimal("88.00"))

    def test_save_rejects_new_instance_with_existing_pk(self):
        entry = self._make_entry()
        forged = LedgerEntry(
            property=self.property, folio_id=entry.folio_id,
            entry_type=LedgerEntry.EntryType.ADJUSTMENT,
            amount=Decimal("9.99"), currency="CNY",
            business_date=self.today, reference="NGD083-FORGED",
            pk=entry.pk)
        with self.assertRaises(ValueError):
            forged.save()
        self.assertFalse(LedgerEntry.objects.filter(
            reference="NGD083-FORGED").exists())

    def test_create_still_allowed_and_queryset_guards_unchanged(self):
        entry = self._make_entry(reference="NGD083-2")
        self.assertTrue(
            LedgerEntry.objects.filter(reference="NGD083-2").exists())
        # 业务 QuerySet 层守卫不受模型 save 守卫影响，仍拒绝 update
        with self.assertRaises(ValidationError):
            LedgerEntry.objects.filter(pk=entry.pk).update(description="x")
        # 测试专用原生 QuerySet 旁路（test_dec02 孤儿化 helper 依赖）仍可用
        QuerySet(LedgerEntry).filter(pk=entry.pk).update(
            description="orphan-sim")
        entry.refresh_from_db()
        self.assertEqual(entry.description, "orphan-sim")
        QuerySet(LedgerEntry).filter(pk=entry.pk).update(description="")




class NightAuditArbitrationSequentialTests(NightAuditGuardBase):
    """WB-109 顺序语义（SQLite 可跑）：夜审只结算锁定后仍 IN_HOUSE 的 Stay，
    退房按差额口径与夜审已过租部分互补、不重复计费。"""

    def test_run_skips_stay_checked_out_before_audit(self):
        """退房先提交 → 夜审跳过已退房 Stay，不产生夜审过租凭证。"""
        stay, folio, _ = self._make_inhouse_stay(
            "NGD109S", price="300.00",
            check_in=self.today - timedelta(days=1), nights=2)
        BillingService.post_entry(
            property_obj=self.property, folio=folio, stay=stay,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-300.00"), currency="CNY",
            business_date=self.today, reference="NGD109S-PREPAY",
            posted_by=self.operator)
        checked_out, changed = check_out_stay(
            property_obj=self.property, stay_id=stay.pk,
            checked_out_by=self.operator)
        self.assertTrue(changed)

        run, created = NightAuditService.run(
            property_obj=self.property, executed_by=self.operator,
            business_date=self.today)
        self.assertTrue(created)
        self.assertEqual(run.status, NightAuditRun.RunStatus.SUCCESS)
        charges = LedgerEntry.objects.filter(
            folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE)
        self.assertEqual(charges.count(), 1)
        self.assertEqual(
            charges.get().reference, f"stay-room-charge:{stay.pk}")
        self.assertFalse(LedgerEntry.objects.filter(
            reference=self._night_reference(stay)).exists())
        bd = BusinessDate.objects.get(
            property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

    def test_checkout_after_night_audit_settles_by_difference(self):
        """夜审先过租 → 退房按“实际晚数快照 − 已过租”差额结算，只收一次。"""
        stay, folio, _ = self._make_inhouse_stay(
            "NGD109T", price="300.00",
            check_in=self.today - timedelta(days=1), nights=2)
        BillingService.post_entry(
            property_obj=self.property, folio=folio, stay=stay,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-300.00"), currency="CNY",
            business_date=self.today, reference="NGD109T-PREPAY",
            posted_by=self.operator)
        run, created = NightAuditService.run(
            property_obj=self.property, executed_by=self.operator,
            business_date=self.today)
        self.assertTrue(created)
        self.assertEqual(run.status, NightAuditRun.RunStatus.SUCCESS)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference=self._night_reference(stay)).count(), 1)

        checked_out, changed = check_out_stay(
            property_obj=self.property, stay_id=stay.pk,
            checked_out_by=self.operator)
        self.assertTrue(changed)
        # 夜审 1 张过租凭证；退房差额为 0，不补记房费也不冲回
        self.assertEqual(
            LedgerEntry.objects.filter(
                folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE
            ).count(), 1)
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"stay-room-charge:{stay.pk}").exists())
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"checkout-adjust:{stay.pk}").exists())
        balance = sum(
            (entry.amount for entry in folio.entries.all()), Decimal("0.00"))
        self.assertEqual(balance, Decimal("0.00"))


class NightAuditCheckoutConcurrencyBase(TransactionTestCase):
    """WB-109 并发仲裁基座（仅 MySQL 生效）：TransactionTestCase + 双线程。"""

    def setUp(self):
        self.property = Property.objects.create(
            code="NGDCC", name="夜审并发仲裁测试酒店")
        self.today = property_date(self.property)
        self.operator = User.objects.create_user(username="ngdcc-operator")
        self.guest = Guest.objects.create(
            property=self.property, full_name="并发仲裁测试客人",
            phone="13800009911", phone_normalized="13800009911")

    def _make_inhouse_stay(self, booking_no, check_in, nights=2,
                           price="300.00"):
        check_out = check_in + timedelta(days=nights)
        suffix = booking_no[-4:]
        room_type = RoomType.objects.create(
            property=self.property, code=f"NGDCC-{suffix}", name="并发测试房型")
        rate_plan = RatePlan.objects.create(
            property=self.property, code=f"NGDCC-BAR-{suffix}",
            name="并发测试门市价", valid_from=check_in)
        nightly = [
            {"date": (check_in + timedelta(days=i)).isoformat(),
             "price": price, "currency": "CNY"}
            for i in range(nights)
        ]
        reservation = Reservation.objects.create(
            property=self.property, booking_no=booking_no, guest=self.guest,
            status=Reservation.Status.CHECKED_IN,
            check_in=check_in, check_out=check_out,
            room_count=1, adult_count=1,
            total_amount=Decimal(price) * nights)
        line = ReservationRoom.objects.create(
            property=self.property, reservation=reservation,
            room_type=room_type, rate_plan=rate_plan,
            check_in=check_in, check_out=check_out,
            nightly_rates=nightly, amount=Decimal(price) * nights)
        room = Room.objects.create(
            property=self.property, room_type=room_type,
            number=f"NGDCC-{suffix}",
            occupancy_status=Room.OccupancyStatus.OCCUPIED)
        stay = Stay.objects.create(
            property=self.property, reservation_room=line, room=room,
            status=Stay.Status.IN_HOUSE)
        folio = Folio.objects.create(
            property=self.property, reservation=reservation,
            guest=self.guest, currency="CNY")
        return stay, folio, line

    def _post_prepay(self, stay, folio, tag, amount="300.00"):
        BillingService.post_entry(
            property_obj=self.property, folio=folio, stay=stay,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-" + amount), currency="CNY",
            business_date=self.today, reference=f"{tag}-PREPAY",
            posted_by=self.operator)

    def _join(self, threads, timeout=60):
        for thread in threads:
            thread.join(timeout=timeout)
            self.assertFalse(thread.is_alive(), "工作线程超时未结束（疑似死锁）")

    def _room_charges(self, folio):
        return LedgerEntry.objects.filter(
            folio=folio, entry_type=LedgerEntry.EntryType.ROOM_CHARGE)


@unittest.skipUnless(
    connection.vendor == "mysql",
    "WB-109 并发仲裁依赖 MySQL 行锁（select_for_update 在 SQLite 下为 "
    "no-op，无法构造真实锁等待），SQLite 测试环境跳过。")
class NightAuditCheckoutConcurrencyTests(NightAuditCheckoutConcurrencyBase):
    """WB-109：夜审与退房并发仲裁（MySQL 专项）。

    夜审先锁：run() 过账事务内在写入任何房费前按主键序锁定涉及 Stay，
    并发退房在 Stay 行锁上等待，夜审提交后退房按差额口径结算 → 只收一次房费。
    退房先锁：夜审的锁定读在退房提交后评估 → Stay 不再 IN_HOUSE → 跳过。
    """

    def test_night_audit_locks_first_checkout_waits_single_charge(self):
        """夜审先锁 → 退房等待 → 只收一次房费。"""
        check_in = self.today - timedelta(days=1)
        stay, folio, line = self._make_inhouse_stay(
            "NGD109A", check_in=check_in, nights=2)
        # 预收 300：夜审过账当日夜 300 后，退房差额为 0，无需再入账
        self._post_prepay(stay, folio, f"NGD109A-STAY{stay.pk}")

        audit_posted = threading.Event()
        real_post_entry = BillingService.post_entry
        errors = []
        results = {}

        def hooked_post_entry(*args, **kwargs):
            entry, created = real_post_entry(*args, **kwargs)
            if str(kwargs.get("reference", "")).startswith("night-audit:"):
                # 此刻夜审已持有全部涉及 Stay 的行锁（锁定先于任何房费写入）
                audit_posted.set()
                time_module.sleep(0.5)
            return entry, created

        def _audit_worker():
            try:
                run, created = NightAuditService.run(
                    property_obj=self.property, executed_by=self.operator,
                    business_date=self.today)
                results["run_status"] = run.status
                results["run_created"] = created
            except Exception as exc:
                errors.append(exc)
            finally:
                connections["default"].close()

        def _checkout_worker():
            try:
                self.assertTrue(audit_posted.wait(timeout=30),
                                "夜审未在超时内进入过租事务")
                time_module.sleep(0.1)
                stay_out, changed = check_out_stay(
                    property_obj=self.property, stay_id=stay.pk,
                    checked_out_by=self.operator)
                results["checkout"] = (str(stay_out.status), changed)
            except Exception as exc:
                errors.append(exc)
            finally:
                connections["default"].close()

        with mock.patch.object(BillingService, "post_entry",
                               side_effect=hooked_post_entry):
            audit_thread = threading.Thread(target=_audit_worker)
            checkout_thread = threading.Thread(target=_checkout_worker)
            audit_thread.start()
            checkout_thread.start()
            self._join([audit_thread, checkout_thread])

        self.assertEqual(errors, [], f"并发执行出现异常: {errors}")
        self.assertEqual(results.get("run_status"),
                         NightAuditRun.RunStatus.SUCCESS)
        self.assertTrue(results.get("run_created"))
        self.assertEqual(results.get("checkout"), ("CHECKED_OUT", True))
        # 只收一次房费：唯一 ROOM_CHARGE=夜审过租 300；退房零差额不补记
        charges = self._room_charges(folio)
        self.assertEqual(charges.count(), 1)
        self.assertEqual(
            charges.get().reference,
            f"night-audit:{self.property.pk}:{self.today.isoformat()}"
            f":stay:{stay.pk}")
        self.assertEqual(charges.get().amount, Decimal("300.00"))
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"stay-room-charge:{stay.pk}").exists())
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"checkout-adjust:{stay.pk}").exists())
        balance = sum(
            (entry.amount for entry in folio.entries.all()), Decimal("0.00"))
        self.assertEqual(balance, Decimal("0.00"))
        bd = BusinessDate.objects.get(
            property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)

    def test_checkout_locks_first_night_audit_skips_checked_out_stay(self):
        """退房先锁/先提交 → 夜审跳过已退房 Stay，不重复计费。"""
        check_in = self.today - timedelta(days=1)
        stay, folio, line = self._make_inhouse_stay(
            "NGD109B", check_in=check_in, nights=2)
        # 预收 300：退房补记昨夜房费 300 后余额 0 结清
        self._post_prepay(stay, folio, f"NGD109B-STAY{stay.pk}")

        checkout_posted = threading.Event()
        real_post_entry = BillingService.post_entry
        errors = []
        results = {}

        def hooked_post_entry(*args, **kwargs):
            entry, created = real_post_entry(*args, **kwargs)
            if str(kwargs.get("reference", "")).startswith("stay-room-charge:"):
                # 此刻退房已持有 Stay/明细/预订/账单行锁（与夜审锁序一致）
                checkout_posted.set()
                time_module.sleep(0.5)
            return entry, created

        def _checkout_worker():
            try:
                stay_out, changed = check_out_stay(
                    property_obj=self.property, stay_id=stay.pk,
                    checked_out_by=self.operator)
                results["checkout"] = (str(stay_out.status), changed)
            except Exception as exc:
                errors.append(exc)
            finally:
                connections["default"].close()

        def _audit_worker():
            try:
                self.assertTrue(checkout_posted.wait(timeout=30),
                                "退房未在超时内进入过账事务")
                time_module.sleep(0.1)
                run, created = NightAuditService.run(
                    property_obj=self.property, executed_by=self.operator,
                    business_date=self.today)
                results["run_status"] = run.status
                results["run_created"] = created
            except Exception as exc:
                errors.append(exc)
            finally:
                connections["default"].close()

        with mock.patch.object(BillingService, "post_entry",
                               side_effect=hooked_post_entry):
            checkout_thread = threading.Thread(target=_checkout_worker)
            audit_thread = threading.Thread(target=_audit_worker)
            checkout_thread.start()
            audit_thread.start()
            self._join([checkout_thread, audit_thread])

        self.assertEqual(errors, [], f"并发执行出现异常: {errors}")
        self.assertEqual(results.get("checkout"), ("CHECKED_OUT", True))
        self.assertEqual(results.get("run_status"),
                         NightAuditRun.RunStatus.SUCCESS)
        self.assertTrue(results.get("run_created"))
        # 只收一次房费：唯一 ROOM_CHARGE=退房补记 300；夜审跳过已退房 Stay
        charges = self._room_charges(folio)
        self.assertEqual(charges.count(), 1)
        self.assertEqual(charges.get().reference,
                         f"stay-room-charge:{stay.pk}")
        self.assertEqual(charges.get().amount, Decimal("300.00"))
        self.assertFalse(LedgerEntry.objects.filter(
            reference=f"night-audit:{self.property.pk}:"
                      f"{self.today.isoformat()}:stay:{stay.pk}").exists())
        bd = BusinessDate.objects.get(
            property=self.property, business_date=self.today)
        self.assertEqual(bd.status, BusinessDate.Status.CLOSED)
