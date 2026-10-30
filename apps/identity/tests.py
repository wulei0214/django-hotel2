from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Property
from .authorization import AuthorizationService
from .models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)


class AuthorizationServiceTests(TestCase):
    def setUp(self):
        self.property_a = Property.objects.create(code="TEST-A", name="测试酒店 A")
        self.property_b = Property.objects.create(code="TEST-B", name="测试酒店 B")
        self.user = User.objects.create_user(username="front-test", password="test-pass-123")
        self.profile = StaffProfile.objects.create(full_name="测试前台", user=self.user)
        self.membership_a = PropertyStaffMembership.objects.create(
            staff_profile=self.profile,
            property=self.property_a,
            employee_no="A001",
            position_code="FRONT_DESK",
        )
        self.group = Group.objects.create(name="TEST_FRONT_DESK")
        content_type = ContentType.objects.get_for_model(StaffProfile)
        self.permission, _ = Permission.objects.get_or_create(
            content_type=content_type,
            codename="test_reservation_create",
            defaults={"name": "Can create test reservation"},
        )
        self.group.permissions.add(self.permission)
        StaffRoleAssignment.objects.create(
            membership=self.membership_a,
            group=self.group,
        )

    def test_permission_is_scoped_to_property_membership(self):
        code = f"{self.permission.content_type.app_label}.{self.permission.codename}"
        self.assertTrue(
            AuthorizationService.has_permission(self.user, self.property_a, code)
        )
        self.assertFalse(
            AuthorizationService.has_permission(self.user, self.property_b, code)
        )

    def test_property_role_does_not_become_global_user_permission(self):
        code = f"{self.permission.content_type.app_label}.{self.permission.codename}"
        self.assertFalse(self.user.has_perm(code))
        self.assertTrue(
            AuthorizationService.has_permission(self.user, self.property_a, code)
        )

    def test_expired_role_assignment_is_rejected(self):
        StaffRoleAssignment.objects.filter(membership=self.membership_a).update(
            starts_at=timezone.now() - timedelta(days=3),
            ends_at=timezone.now() - timedelta(days=1),
        )
        code = f"{self.permission.content_type.app_label}.{self.permission.codename}"
        self.assertFalse(
            AuthorizationService.has_permission(self.user, self.property_a, code)
        )

    def test_object_from_another_property_is_rejected(self):
        code = f"{self.permission.content_type.app_label}.{self.permission.codename}"
        foreign_object = SimpleNamespace(property_id=self.property_b.pk)
        self.assertFalse(
            AuthorizationService.has_permission(
                self.user, self.property_a, code, obj=foreign_object
            )
        )

    def test_inactive_membership_is_rejected(self):
        self.membership_a.status = PropertyStaffMembership.Status.SUSPENDED
        self.membership_a.save(update_fields=("status", "updated_at"))
        code = f"{self.permission.content_type.app_label}.{self.permission.codename}"
        self.assertFalse(
            AuthorizationService.has_permission(self.user, self.property_a, code)
        )
