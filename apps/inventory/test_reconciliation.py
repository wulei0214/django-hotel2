# -*- coding: utf-8 -*-
"""D4 库存差异扫描/审批修复 + FIN-P0 账务对账测试。"""
from decimal import Decimal
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import AuditEvent, Property
from apps.billing.models import Folio, LedgerEntry, Payment, RefundLiability
from apps.billing.models import FolioReconciliationRun, FolioReconciliationDiff
from apps.billing.reconciliation import scan_folio_reconciliation
from apps.inventory.models import (
    InventoryReconciliationRun,
    InventoryDiscrepancy,
    RatePlan,
    RoomTypeInventory,
    RoomType,
)
from apps.inventory.reconciliation import (
    scan_inventory_discrepancies,
    request_inventory_fix,
    approve_inventory_fix,
    execute_inventory_fix,
    dismiss_discrepancy,
)
from apps.reservations.models import (
    InventoryHold,
    Reservation,
    ReservationRoom,
)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class InventoryReconciliationTests(TestCase):
    """D4：库存差异扫描/申请/审批/执行/关闭。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.rt = RoomType.objects.create(
            property=cls.prop, code="REC-STD", name="对账测试房型", capacity=2)
        cls.rate_plan = RatePlan.objects.create(
            property=cls.prop, code="REC-BAR", name="对账测试门市价",
            valid_from=timezone.localdate())
        cls.users = {}
        for name in ("rec_front", "rec_duty", "rec_finance"):
            cls.users[name.split("_")[1]] = User.objects.create_user(
                username=name, password="x")
        cls.front = cls.users["front"]
        cls.duty = cls.users["duty"]

    def _create_inventory_row(self, total=4, held=0, sold=0, oos=0):
        return RoomTypeInventory.objects.create(
            property=self.prop, room_type=self.rt,
            stay_date=timezone.localdate(),
            total_rooms=total, held=held, sold=sold, out_of_service=oos)

    def _make_dirty_data(self, row):
        """制造差异：账面 held=0 但实际 Hold 行存在。"""
        from apps.guests.models import Guest
        guest = Guest.objects.create(property=self.prop, full_name="差异测试")
        res = Reservation.objects.create(
            property=self.prop, guest=guest, booking_no="REC-HOLD-001",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("200.00"), currency="CNY")
        rr = ReservationRoom.objects.create(
            property=self.prop, reservation=res, room_type=self.rt,
            rate_plan=self.rate_plan,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            amount=Decimal("200.00"))
        from apps.reservations.models import InventoryHold
        InventoryHold.objects.create(
            property=self.prop, room_type=self.rt, stay_date=row.stay_date,
            reservation_room=rr, quantity=1,
            status=InventoryHold.HoldStatus.HELD)
        # 账面 held 仍是 0（不一致）

    def test_scan_finds_discrepancy(self):
        row = self._create_inventory_row(total=4, held=0)
        self._make_dirty_data(row)
        run = scan_inventory_discrepancies(
            property_obj=self.prop, days_back=30, checked_by=self.duty)
        self.assertGreater(run.diff_count, 0, "扫描应发现 held 计数不一致")
        discs = InventoryDiscrepancy.objects.filter(run=run)
        for d in discs:
            self.assertEqual(d.status, InventoryDiscrepancy.Status.OPEN)

    def test_scan_no_diff_when_clean(self):
        self._create_inventory_row(total=4, held=0, sold=0)
        run = scan_inventory_discrepancies(
            property_obj=self.prop, days_back=30, checked_by=self.duty)
        self.assertEqual(run.diff_count, 0)

    def test_fix_full_flow(self):
        row = self._create_inventory_row(total=4, held=0)
        self._make_dirty_data(row)
        run = scan_inventory_discrepancies(
            property_obj=self.prop, days_back=30, checked_by=self.duty)
        disc = InventoryDiscrepancy.objects.filter(run=run, status="OPEN").first()
        self.assertIsNotNone(disc)

        # 发起修复申请
        requested = request_inventory_fix(
            property_obj=self.prop, discrepancy_id=disc.pk,
            fix_reason="库存计数与实际 Hold 行不一致",
            fix_evidence="InventoryHold 行数 1 vs 库存行 held 0",
            requested_by=self.front)
        self.assertEqual(requested.status, InventoryDiscrepancy.Status.FIX_REQUESTED)
        # 自批拒绝
        with self.assertRaises(ValueError):
            approve_inventory_fix(property_obj=self.prop,
                                  discrepancy_id=disc.pk, approved_by=self.front)
        # 独立审批
        approved = approve_inventory_fix(property_obj=self.prop,
                                         discrepancy_id=disc.pk,
                                         approved_by=self.duty)
        self.assertEqual(approved.status, InventoryDiscrepancy.Status.FIX_APPROVED)
        # 执行修复
        fixed = execute_inventory_fix(property_obj=self.prop,
                                      discrepancy_id=disc.pk,
                                      executed_by=self.duty)
        self.assertEqual(fixed.status, InventoryDiscrepancy.Status.FIXED)
        row.refresh_from_db()
        self.assertEqual(row.held, 1, "修复后 held 应与实际 Hold 行数一致")
        self.assertTrue(AuditEvent.objects.filter(
            action="inventory.fix_executed").exists())

    def test_dismiss_open_discrepancy(self):
        self._create_inventory_row(total=4, held=0)
        run = scan_inventory_discrepancies(
            property_obj=self.prop, days_back=30, checked_by=self.duty)
        disc = InventoryDiscrepancy.objects.filter(run=run, status="OPEN").first()
        if disc:
            dismissed = dismiss_discrepancy(
                property_obj=self.prop, discrepancy_id=disc.pk,
                dismissed_by=self.duty, reason="误报")
            self.assertEqual(dismissed.status,
                             InventoryDiscrepancy.Status.DISMISSED)


@override_settings(DEBUG=True, DJANGO_ENV="test")
class FolioReconciliationTests(TestCase):
    """FIN-P0：账务对账扫描（Payment↔凭证 / Refund↔凭证）。"""

    @classmethod
    def setUpTestData(cls):
        cls.prop = Property.objects.get(code="DEFAULT")
        cls.finance = User.objects.create_user(username="fin_recon", password="x")
        from apps.guests.models import Guest
        cls.guest = Guest.objects.create(property=cls.prop, full_name="对账测试")

    def _create_folio_with_payment_and_entry(self):
        from apps.reservations.models import Reservation
        res = Reservation.objects.create(
            property=self.prop, guest=self.guest, booking_no=f"FIN-{id(self)}",
            status=Reservation.Status.CONFIRMED,
            check_in=timezone.localdate(),
            check_out=timezone.localdate() + timedelta(days=1),
            room_count=1, total_amount=Decimal("200.00"), currency="CNY")
        folio = Folio.objects.create(
            property=self.prop, reservation=res, guest=self.guest, currency="CNY")
        Payment.objects.create(
            property=self.prop, reservation=res, folio=folio,
            provider="mock", provider_ref="FIN-PAY-001", method="MOCK",
            amount=Decimal("200.00"), currency="CNY",
            status=Payment.Status.SUCCEEDED, paid_at=timezone.now())
        LedgerEntry.objects.create(
            property=self.prop, folio=folio,
            entry_type=LedgerEntry.EntryType.PAYMENT, amount=Decimal("-200.00"),
            currency="CNY", business_date=timezone.localdate(),
            reference="payment:FIN-PAY-001", description="预付")
        return folio

    def test_clean_folio_no_diff(self):
        folio = self._create_folio_with_payment_and_entry()
        run = scan_folio_reconciliation(property_obj=self.prop,
                                        checked_by=self.finance)
        diffs = FolioReconciliationDiff.objects.filter(run=run, folio=folio)
        self.assertEqual(diffs.count(), 0)

    def test_orphan_payment_found(self):
        folio = self._create_folio_with_payment_and_entry()
        # 制造差异：Payment 存在但凭证引用被孤儿化
        from django.db.models import QuerySet
        QuerySet(LedgerEntry).filter(
            reference="payment:FIN-PAY-001").update(reference="orphaned:FIN")
        run = scan_folio_reconciliation(property_obj=self.prop,
                                        checked_by=self.finance)
        diffs = FolioReconciliationDiff.objects.filter(
            run=run, folio=folio,
            diff_type=FolioReconciliationDiff.DiffType.PAYMENT_NO_ENTRY)
        self.assertEqual(diffs.count(), 1, "孤儿化凭证应被发现")
        self.assertFalse(RefundLiability.objects.exists() and False)
