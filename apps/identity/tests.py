from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from apps.core.models import Property
from .authorization import AuthorizationService
from .services import EmployeeLifecycleError, EmployeeLifecycleService
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


class EmployeeLifecycleServiceTests(TestCase):
    """员工生命周期服务的关键输入校验与状态映射。"""

    def setUp(self):
        self.property_obj, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "默认测试酒店"}
        )
        self.manager = User.objects.create_user(
            username="lifecycle-manager", password="test-pass-123"
        )
        self.employee = User.objects.create_user(
            username="lifecycle-employee", password="test-pass-123"
        )
        self.profile = StaffProfile.objects.create(
            user=self.employee, full_name="同名员工", phone="13800001111",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        self.membership = PropertyStaffMembership.objects.create(
            staff_profile=self.profile, property=self.property_obj,
            employee_no="LIFE-001", status=PropertyStaffMembership.Status.ACTIVE,
        )
        self.group, _ = Group.objects.get_or_create(name="前台")
        self.assignment = StaffRoleAssignment.objects.create(
            membership=self.membership, group=self.group,
            starts_at=timezone.now() - timedelta(minutes=1),
        )

    def test_duplicate_phone_is_rejected_even_for_same_name(self):
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.create_employee(
                full_name="同名员工", employee_no="LIFE-002",
                phone=" 13800001111 ",
            )
        self.assertEqual(StaffProfile.objects.filter(phone="13800001111").count(), 1)

    def test_suspended_status_maps_to_inactive_profile_and_suspended_membership(self):
        EmployeeLifecycleService.set_employment_status(
            self.profile, status="SUSPENDED", ended_by=self.manager, reason="测试暂停"
        )
        self.profile.refresh_from_db()
        self.membership.refresh_from_db()
        self.assignment.refresh_from_db()
        self.employee.refresh_from_db()
        self.assertEqual(self.profile.employment_status, StaffProfile.EmploymentStatus.INACTIVE)
        self.assertEqual(self.membership.status, PropertyStaffMembership.Status.SUSPENDED)
        self.assertIsNone(self.assignment.ends_at)
        self.assertTrue(self.employee.is_active)

    def test_invalid_employment_status_is_rejected_without_mutation(self):
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.set_employment_status(
                self.profile, status="PAUSED", ended_by=self.manager
            )
        self.profile.refresh_from_db()
        self.membership.refresh_from_db()
        self.assertEqual(self.profile.employment_status, StaffProfile.EmploymentStatus.ACTIVE)
        self.assertEqual(self.membership.status, PropertyStaffMembership.Status.ACTIVE)

    def test_role_assignment_rejects_non_positive_effective_period(self):
        now = timezone.now()
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.assign_role(
                membership=self.membership, group=self.group,
                starts_at=now, ends_at=now, granted_by=self.manager,
            )

    def test_role_assignment_normalizes_naive_end_datetime(self):
        end_at = (timezone.localtime() + timedelta(days=1)).replace(tzinfo=None)
        future_group, _ = Group.objects.get_or_create(name="值班经理")
        assignment = EmployeeLifecycleService.assign_role(
            membership=self.membership, group=future_group,
            ends_at=end_at, granted_by=self.manager,
        )
        self.assertTrue(timezone.is_aware(assignment.ends_at))

    def test_role_assignment_requires_a_group(self):
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.assign_role(
                membership=self.membership, group=None, granted_by=self.manager
            )

    def test_role_assignment_rejects_group_outside_business_role_directory(self):
        unapproved_group = Group.objects.create(name="Unapproved Global Admin")
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.assign_role(
                membership=self.membership,
                group=unapproved_group,
                granted_by=self.manager,
            )
        self.assertFalse(
            StaffRoleAssignment.objects.filter(
                membership=self.membership, group=unapproved_group
            ).exists()
        )

    def test_role_assignment_rejects_suspended_membership(self):
        self.membership.status = PropertyStaffMembership.Status.SUSPENDED
        self.membership.save(update_fields=("status", "updated_at"))
        manager_role, _ = Group.objects.get_or_create(name="值班经理")
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.assign_role(
                membership=self.membership,
                group=manager_role,
                granted_by=self.manager,
            )

    def test_role_assignment_rejects_inactive_staff_profile(self):
        self.profile.employment_status = StaffProfile.EmploymentStatus.INACTIVE
        self.profile.save(update_fields=("employment_status", "updated_at"))
        manager_role, _ = Group.objects.get_or_create(name="值班经理")
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.assign_role(
                membership=self.membership,
                group=manager_role,
                granted_by=self.manager,
            )


class IdentityAdminReadOnlyTests(TestCase):
    """员工身份关系只能由 EmployeeLifecycleService 写入，Admin 只保留读取。"""

    def test_identity_admin_disables_direct_add_change_and_delete(self):
        from django.contrib import admin as django_admin

        root = User.objects.create_superuser(
            username="identity-admin-root",
            email="identity-admin@example.com",
            password="test-pass-123",
        )
        profile = StaffProfile.objects.create(
            full_name="只读 Admin 员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        prop, _ = Property.objects.get_or_create(
            code="DEFAULT", defaults={"name": "默认测试酒店"}
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=prop,
            employee_no="ADMIN-READ-001",
        )
        group, _ = Group.objects.get_or_create(name="前台")
        assignment = StaffRoleAssignment.objects.create(
            membership=membership, group=group
        )

        for model in (StaffProfile, PropertyStaffMembership, StaffRoleAssignment):
            model_admin = django_admin.site._registry[model]
            self.assertFalse(model_admin.has_add_permission(None))
            self.assertFalse(model_admin.has_change_permission(None))
            self.assertFalse(model_admin.has_delete_permission(None))

        self.client.force_login(root)
        change_url = f"/admin/identity/staffroleassignment/{assignment.pk}/change/"
        response = self.client.get(change_url)
        self.assertEqual(response.status_code, 200, "超级管理员仍可审计查看岗位授权")
        denied_post = self.client.post(
            change_url,
            {"_save": "保存", "ends_at": ""},
        )
        self.assertEqual(denied_post.status_code, 403, "Admin POST 不得修改岗位授权")
        assignment.refresh_from_db()
        self.assertIsNone(assignment.ends_at)
