# -*- coding: utf-8 -*-
"""白盒修复回归：夜审/账本守卫（WB-079/080/081/082/083）。

覆盖：
  * WB-079：post_entry 营业日关账守卫（CLOSED 拒绝新增；OPEN/无记录放行；
    同事实重放幂等不受影响；夜审 run 过租在关账前完成不受阻断）；
  * WB-080：夜审日期必须等于"当前物业唯一 OPEN/REOPENED 营业日"，
    未来/历史/无记录日期拒绝且不自动创建 BusinessDate；
  * WB-081：preview.stay_count 与过租窗口（check_in <= day < check_out）同口径；
  * WB-082：already_posted 内容一致判定；同引用不同事实 → 账务冲突 blocker；
  * WB-083：LedgerEntry.save() 只追加守卫（实例级不可变），QuerySet 层
    守卫行为保持不变（test_dec02 孤儿化 helper 的原生 QuerySet 旁路兼容）。

自建物业/房型/预订/Stay/Folio，不依赖 DEFAULT 物业已有数据。
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.test import TestCase

from apps.billing.models import Folio, LedgerEntry
from apps.billing.night_audit import (
    NightAuditError, NightAuditService, property_date,
)
from apps.billing.services_v2 import BillingService
from apps.core.models import BusinessDate, NightAuditRun, Property
from apps.guests.models import Guest
from apps.inventory.models import RatePlan, Room, RoomType
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

    def test_channel_payment_callback_after_close_is_exempt(self):
        """合法链路豁免：渠道回调（posted_by=None 的 PAYMENT）关账后仍可入账。"""
        stay, folio, _ = self._make_inhouse_stay("NGD079E")
        BusinessDate.objects.create(
            property=self.property, business_date=self.today,
            status=BusinessDate.Status.CLOSED)
        entry, created = BillingService.post_entry(
            property_obj=self.property, folio=folio, stay=stay,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-600.00"), currency="CNY",
            business_date=self.today,
            reference="payment:NGD079-CHANNEL-001",
            description="渠道支付成功回调入账", posted_by=None)
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
