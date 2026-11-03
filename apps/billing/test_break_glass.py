# -*- coding: utf-8 -*-
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

from apps.billing.night_audit import NightAuditError, NightAuditService
from apps.core.models import AuditEvent, BreakGlassRecord, BusinessDate, Property
from apps.identity.models import (
    PropertyStaffMembership, StaffProfile, StaffRoleAssignment,
)


class BreakGlassServiceTests(TestCase):
    """Q-06：单一经理紧急重开、限时执行、独立事后复核。"""

    def setUp(self):
        self.property = Property.objects.create(code="BG", name="紧急重开测试酒店")
        self.day = timezone.localdate() - timedelta(days=1)
        self.business_date = BusinessDate.objects.create(
            property=self.property,
            business_date=self.day,
            status=BusinessDate.Status.CLOSED,
        )
        self.actor = User.objects.create_user(
            username="breakglass-actor", password="BreakGlass@123"
        )
        self.actor_group = Group.objects.get(name="值班经理")
        reopen_permission = Permission.objects.get(
            content_type__app_label="hotelapp",
            codename="business_date_reopen",
        )
        self.actor_group.permissions.add(reopen_permission)
        self.actor_membership = self._add_staff(
            self.actor, "BG-001", self.actor_group
        )

    def _add_staff(self, user, employee_no, group):
        profile = StaffProfile.objects.create(
            user=user,
            full_name=employee_no,
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property,
            employee_no=employee_no,
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        return membership

    def _request(self):
        return NightAuditService.request_break_glass(
            property_obj=self.property,
            business_date_id=self.business_date.pk,
            requested_by=self.actor,
            password="BreakGlass@123",
            reason="夜审发现紧急账务差错，需要修正营业日",
        )

    def test_break_glass_requires_second_password_verification(self):
        with self.assertRaises(NightAuditError):
            NightAuditService.request_break_glass(
                property_obj=self.property,
                business_date_id=self.business_date.pk,
                requested_by=self.actor,
                password="wrong-password",
                reason="夜审发现紧急账务差错，需要修正营业日",
            )
        self.assertEqual(BreakGlassRecord.objects.count(), 0)

    def test_break_glass_is_denied_when_another_manager_can_approve(self):
        second_manager = User.objects.create_user(
            username="breakglass-second-manager", password="Manager@123"
        )
        self._add_staff(second_manager, "BG-002", self.actor_group)
        with self.assertRaises(NightAuditError):
            self._request()
        self.assertEqual(BreakGlassRecord.objects.count(), 0)

    def test_authorization_is_time_limited_bound_to_actor_and_single_use(self):
        record = self._request()
        self.assertEqual(record.status, BreakGlassRecord.Status.GRANTED)
        self.assertEqual(record.expires_at - record.password_verified_at,
                         timedelta(minutes=10))

        wrong_actor = User.objects.create_user(
            username="breakglass-wrong-actor", password="Wrong@123"
        )
        with self.assertRaises(NightAuditError):
            NightAuditService.execute_break_glass(
                property_obj=self.property,
                business_date_id=self.business_date.pk,
                record_id=record.pk,
                executed_by=wrong_actor,
            )

        reopened = NightAuditService.execute_break_glass(
            property_obj=self.property,
            business_date_id=self.business_date.pk,
            record_id=record.pk,
            executed_by=self.actor,
        )
        self.assertEqual(reopened.status, BusinessDate.Status.REOPENED)
        self.assertEqual(reopened.reopened_by_id, self.actor.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, BreakGlassRecord.Status.USED)

        with self.assertRaises(NightAuditError):
            NightAuditService.execute_break_glass(
                property_obj=self.property,
                business_date_id=self.business_date.pk,
                record_id=record.pk,
                executed_by=self.actor,
            )

    def test_expired_authorization_cannot_reopen_business_date(self):
        record = self._request()
        record.expires_at = timezone.now() - timedelta(seconds=1)
        record.save(update_fields=("expires_at",))
        result = NightAuditService.execute_break_glass(
            property_obj=self.property,
            business_date_id=self.business_date.pk,
            record_id=record.pk,
            executed_by=self.actor,
        )
        self.assertIsNone(result)
        record.refresh_from_db()
        self.business_date.refresh_from_db()
        self.assertEqual(record.status, BreakGlassRecord.Status.EXPIRED)
        self.assertEqual(self.business_date.status, BusinessDate.Status.CLOSED)

    def test_independent_manager_must_review_after_execution(self):
        record = self._request()
        NightAuditService.execute_break_glass(
            property_obj=self.property,
            business_date_id=self.business_date.pk,
            record_id=record.pk,
            executed_by=self.actor,
        )

        reviewer = User.objects.create_user(
            username="breakglass-reviewer", password="Reviewer@123"
        )
        reviewer_group, _ = Group.objects.get_or_create(name="店长-事后复核")
        review_permission = Permission.objects.get(
            content_type__app_label="core",
            codename="breakglass_review",
        )
        reviewer_group.permissions.add(review_permission)
        self._add_staff(reviewer, "BG-REVIEW", reviewer_group)

        with self.assertRaises(NightAuditError):
            NightAuditService.review_break_glass(
                property_obj=self.property,
                record_id=record.pk,
                reviewed_by=self.actor,
                decision=BreakGlassRecord.ReviewDecision.CONFIRMED,
                reason="本人复核不符合独立复核要求",
            )

        reviewed = NightAuditService.review_break_glass(
            property_obj=self.property,
            record_id=record.pk,
            reviewed_by=reviewer,
            decision=BreakGlassRecord.ReviewDecision.CONFIRMED,
            reason="已核对审批原因、营业日及对应账务调整证据",
        )
        self.assertEqual(reviewed.status, BreakGlassRecord.Status.REVIEWED)
        self.assertEqual(reviewed.reviewed_by_id, reviewer.pk)
        self.assertEqual(
            AuditEvent.objects.filter(
                action="night_audit.break_glass_reviewed",
                entity_id=str(record.pk),
            ).count(),
            1,
        )
