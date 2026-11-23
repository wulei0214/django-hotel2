"""ARCH-07：Mock 支付必须同时受 HTTP 入口和服务层生产门禁保护。"""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.billing.models import Folio, Payment, PaymentIntent
from apps.billing.services import (
    PaymentConflict,
    create_mock_payment_intent,
    process_mock_payment_callback,
)
from apps.core.models import Property
from apps.guests.models import Guest
from apps.reservations.models import Reservation


@override_settings(DEBUG=False, DJANGO_ENV="prod", IS_PRODUCTION=True)
class MockPaymentProductionGuardTests(TestCase):
    property_obj: Property
    guest: Guest
    reservation: Reservation
    payment: Payment

    @classmethod
    def setUpTestData(cls):
        cls.property_obj = Property.objects.create(code="MOCK-PROD", name="生产门禁测试")
        cls.guest = Guest.objects.create(property=cls.property_obj, full_name="合成测试住客")
        cls.reservation = Reservation.objects.create(
            property=cls.property_obj,
            guest=cls.guest,
            booking_no="MOCK-PROD-001",
            status=Reservation.Status.PENDING,
            check_in="2027-01-01",
            check_out="2027-01-02",
            total_amount=Decimal("100.00"),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        Folio.objects.create(
            property=cls.property_obj,
            reservation=cls.reservation,
            guest=cls.guest,
        )
        cls.payment = Payment.objects.create(
            property=cls.property_obj,
            reservation=cls.reservation,
            folio=cls.reservation.get_master_folio(),
            provider="mock",
            provider_ref="MOCK-PROD-CALLBACK",
            method="MOCK",
            amount=cls.reservation.total_amount,
            currency=cls.reservation.currency,
            status=Payment.Status.PENDING,
        )

    def test_create_mock_payment_is_rejected_before_any_fact_is_written(self):
        before_payments = Payment.objects.count()
        before_intents = PaymentIntent.objects.count()
        with self.assertRaises(PaymentConflict) as ctx:
            create_mock_payment_intent(self.reservation)
        self.assertIn("生产", str(ctx.exception))
        self.assertEqual(Payment.objects.count(), before_payments)
        self.assertEqual(PaymentIntent.objects.count(), before_intents)

    def test_mock_callback_is_rejected_before_any_fact_is_written(self):
        before_payments = Payment.objects.count()
        before_intents = PaymentIntent.objects.count()
        with self.assertRaises(PaymentConflict) as ctx:
            process_mock_payment_callback(
                provider_ref=self.payment.provider_ref,
                amount="100.00",
                status="SUCCESS",
                signature_verified=True,
            )
        self.assertIn("生产", str(ctx.exception))
        self.assertEqual(Payment.objects.count(), before_payments)
        self.assertEqual(PaymentIntent.objects.count(), before_intents)
        self.payment.refresh_from_db()
        self.reservation.refresh_from_db()
        self.assertEqual(self.payment.status, Payment.Status.PENDING)
        self.assertIsNone(self.payment.paid_at)
        self.assertEqual(self.reservation.status, Reservation.Status.PENDING)
