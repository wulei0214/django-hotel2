"""FIN-P0 对账反向扫描回归：PMS2 Payment 与历史迁移凭证分流。"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Folio, FolioReconciliationDiff, LedgerEntry
from apps.billing.reconciliation import scan_folio_reconciliation
from apps.core.models import LegacyRecordLink, Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation


@override_settings(DEBUG=True, DJANGO_ENV="test")
class FolioReconciliationReverseScanTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.property = Property.objects.create(code="RECON", name="对账测试酒店")
        self.guest = Guest.objects.create(property=self.property, full_name="对账测试住客")
        self.reservation = Reservation.objects.create(
            property=self.property,
            guest=self.guest,
            booking_no="RECON-TEST-001",
            status=Reservation.Status.CONFIRMED,
            check_in=self.today,
            check_out=self.today + timedelta(days=1),
            room_count=1,
            total_amount=Decimal("100.00"),
            currency="CNY",
        )
        self.folio = Folio.objects.create(
            property=self.property,
            reservation=self.reservation,
            guest=self.guest,
            currency="CNY",
        )

    def _payment_entry(self, reference):
        return LedgerEntry.objects.create(
            property=self.property,
            folio=self.folio,
            entry_type=LedgerEntry.EntryType.PAYMENT,
            amount=Decimal("-100.00"),
            currency="CNY",
            business_date=self.today,
            reference=reference,
        )

    def test_orphan_pms2_payment_entry_generates_entry_no_payment(self):
        entry = self._payment_entry("payment:missing-provider-ref")

        run = scan_folio_reconciliation(property_obj=self.property)

        self.assertTrue(
            FolioReconciliationDiff.objects.filter(
                run=run,
                folio=self.folio,
                diff_type=FolioReconciliationDiff.DiffType.ENTRY_NO_PAYMENT,
                entity_type="LedgerEntry",
                entity_id=entry.pk,
            ).exists()
        )
        self.assertEqual(run.diff_count, 1)

    def test_legacy_payment_entry_without_matching_link_generates_entry_no_payment(self):
        entry = self._payment_entry("legacy:Folio:9002")

        run = scan_folio_reconciliation(property_obj=self.property)

        diff = FolioReconciliationDiff.objects.get(
            run=run,
            diff_type=FolioReconciliationDiff.DiffType.ENTRY_NO_PAYMENT,
            entity_id=entry.pk,
        )
        self.assertIn("LegacyRecordLink", diff.detail)

    def test_linked_legacy_payment_entries_are_not_misclassified(self):
        entries = (
            ("Folio", "9001"),
            ("FinanceRecord", "7001"),
        )
        for source_model, source_pk in entries:
            entry = self._payment_entry(f"legacy:{source_model}:{source_pk}")
            LegacyRecordLink.objects.create(
                property=self.property,
                source_system="hotelapp",
                source_model=source_model,
                source_pk=source_pk,
                target_model="LedgerEntry",
                target_pk=str(entry.pk),
                mapping_status=LegacyRecordLink.MappingStatus.LINKED,
                match_method="legacy_migration",
                migration_batch="RECON-LEGACY-TEST",
            )

        ledger_count_before = LedgerEntry.objects.count()
        run = scan_folio_reconciliation(property_obj=self.property)

        self.assertFalse(
            FolioReconciliationDiff.objects.filter(
                run=run,
                diff_type=FolioReconciliationDiff.DiffType.ENTRY_NO_PAYMENT,
            ).exists()
        )
        self.assertEqual(run.diff_count, 0)
        self.assertEqual(LedgerEntry.objects.count(), ledger_count_before)
