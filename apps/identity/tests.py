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
        # Admin routes read the dedicated cookie, not PMS's sessionid.
        self.client.cookies["admin_sessionid"] = self.client.cookies["sessionid"].value
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



class NineRoleCrossModulePermissionRegressionTests(TestCase):
    """九个业务角色的跨模块关键权限隔离回归。"""

    ROLE_NAMES = (
        "前台", "值班经理", "财务", "店长", "客房服务员",
        "夜审员", "餐饮服务员", "餐饮主管", "停车安保",
    )

    def test_all_nine_business_role_groups_exist(self):
        present = set(
            Group.objects.filter(name__in=self.ROLE_NAMES)
            .values_list("name", flat=True)
        )
        self.assertEqual(present, set(self.ROLE_NAMES))

    def test_no_show_and_refund_permissions_follow_separation_of_duties(self):
        no_show = Permission.objects.get(
            content_type__app_label="hotelapp", codename="no_show_handle"
        )
        refund_request = Permission.objects.get(
            content_type__app_label="billing", codename="refund_request"
        )
        refund_approve = Permission.objects.get(
            content_type__app_label="billing", codename="refund_approve"
        )
        groups = {
            name: Group.objects.get(name=name)
            for name in self.ROLE_NAMES
        }
        no_show_roles = {
            name for name, group in groups.items()
            if group.permissions.filter(pk=no_show.pk).exists()
        }
        refund_request_roles = {
            name for name, group in groups.items()
            if group.permissions.filter(pk=refund_request.pk).exists()
        }
        refund_approve_roles = {
            name for name, group in groups.items()
            if group.permissions.filter(pk=refund_approve.pk).exists()
        }
        self.assertEqual(no_show_roles, {"值班经理", "店长"})
        self.assertEqual(refund_request_roles, {"前台", "财务"})
        self.assertEqual(refund_approve_roles, {"值班经理", "店长"})
        self.assertFalse(refund_approve_roles & refund_request_roles)

    def test_runtime_authorization_isolated_across_all_nine_roles(self):
        prop = Property.objects.create(code="RBAC-NINE", name="九角色权限回归酒店")
        permission_codes = (
            "hotelapp.view_accommodation",
            "hotelapp.change_accommodation",
            "hotelapp.view_room",
            "hotelapp.change_room",
            "hotelapp.no_show_handle",
            "hotelapp.night_audit_execute",
            "hotelapp.room_release_ready",
            "hotelapp.finance_access",
            "hotelapp.export_pii",
            "billing.refund_request",
            "billing.refund_approve",
        )
        expected = {
            "前台": {
                "hotelapp.view_accommodation", "hotelapp.change_accommodation",
                "hotelapp.view_room", "billing.refund_request",
            },
            "值班经理": {
                "hotelapp.view_accommodation", "hotelapp.change_accommodation",
                "hotelapp.view_room", "hotelapp.change_room",
                "hotelapp.no_show_handle", "hotelapp.night_audit_execute",
                "hotelapp.room_release_ready", "hotelapp.finance_access",
                "hotelapp.export_pii", "billing.refund_approve",
            },
            "财务": {
                "hotelapp.view_accommodation", "hotelapp.view_room",
                "hotelapp.finance_access", "hotelapp.export_pii",
                "billing.refund_request",
            },
            "店长": {
                "hotelapp.view_accommodation", "hotelapp.change_accommodation",
                "hotelapp.view_room", "hotelapp.change_room",
                "hotelapp.no_show_handle", "hotelapp.night_audit_execute",
                "hotelapp.room_release_ready", "hotelapp.finance_access",
                "hotelapp.export_pii", "billing.refund_approve",
            },
            "客房服务员": {
                "hotelapp.view_room", "hotelapp.change_room",
            },
            "夜审员": set(),
            "餐饮服务员": set(),
            "餐饮主管": set(),
            "停车安保": set(),
        }
        self.assertEqual(set(expected), set(self.ROLE_NAMES))

        for index, role_name in enumerate(self.ROLE_NAMES, start=1):
            user = User.objects.create_user(
                username=f"rbac-nine-{index}", password="test-pass-123"
            )
            profile = StaffProfile.objects.create(
                full_name=f"{role_name}回归账号",
                user=user,
                employment_status=StaffProfile.EmploymentStatus.ACTIVE,
            )
            membership = PropertyStaffMembership.objects.create(
                staff_profile=profile,
                property=prop,
                employee_no=f"RBAC-{index:03d}",
                status=PropertyStaffMembership.Status.ACTIVE,
                position_code="RBAC_TEST",
            )
            StaffRoleAssignment.objects.create(
                membership=membership,
                group=Group.objects.get(name=role_name),
                starts_at=timezone.now() - timedelta(minutes=1),
            )
            for code in permission_codes:
                self.assertEqual(
                    AuthorizationService.has_permission(user, prop, code),
                    code in expected[role_name],
                    msg=f"{role_name} 的运行时权限判断不符合矩阵：{code}",
                )



class SplitAuthenticationSessionTests(TestCase):
    """PMS and Django Admin must remain logged in independently in one browser."""

    def setUp(self):
        from django.contrib.auth.models import Permission
        from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

        self.property_obj = Property.objects.get(code="DEFAULT")
        self.pms_user = User.objects.create_user(
            username="split-pms-user", password="Pms-pass-2026!"
        )
        self.admin_user = User.objects.create_superuser(
            username="split-admin-user",
            email="split-admin@example.com",
            password="Admin-pass-2026!",
        )
        profile = StaffProfile.objects.create(
            user=self.pms_user,
            full_name="隔离会话测试员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            staff_profile=profile,
            property=self.property_obj,
            employee_no="SPLIT-SESSION-001",
            position_code="FRONT_DESK",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        front_desk = Group.objects.get(name="前台")
        permission = Permission.objects.get(
            content_type__app_label="hotelapp",
            codename="view_accommodation",
        )
        front_desk.permissions.add(permission)
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=front_desk,
            starts_at=timezone.now() - timedelta(minutes=1),
        )

    def _login_both_entry_points(self, client):
        pms_response = client.post("/login/", {
            "username": "split-pms-user",
            "password": "Pms-pass-2026!",
        })
        self.assertEqual(pms_response.status_code, 302)
        pms_session_key = client.cookies["sessionid"].value

        admin_response = client.post("/admin/login/", {
            "username": "split-admin-user",
            "password": "Admin-pass-2026!",
            "next": "/admin/",
        })
        self.assertEqual(admin_response.status_code, 302, admin_response.content)
        admin_session_key = client.cookies["admin_sessionid"].value

        self.assertTrue(pms_session_key)
        self.assertTrue(admin_session_key)
        self.assertNotEqual(pms_session_key, admin_session_key)
        return pms_session_key, admin_session_key

    def test_pms_and_admin_can_stay_logged_in_simultaneously(self):
        client = self.client
        pms_session_key, admin_session_key = self._login_both_entry_points(client)

        # The same browser sends both cookies; middleware picks one based on URL.
        self.assertEqual(client.get("/home/").status_code, 200)
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertEqual(client.get("/home/").status_code, 200)
        self.assertEqual(client.get("/admin/").status_code, 200)

        self.assertEqual(client.cookies["sessionid"].value, pms_session_key)
        self.assertEqual(client.cookies["admin_sessionid"].value, admin_session_key)

    def test_pms_logout_does_not_log_out_admin(self):
        self._login_both_entry_points(self.client)

        pms_logout = self.client.get("/logout/")
        self.assertEqual(pms_logout.status_code, 302)
        self.assertEqual(self.client.get("/home/").status_code, 302)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertTrue(self.client.cookies["admin_sessionid"].value)

    def test_admin_logout_does_not_log_out_pms(self):
        self._login_both_entry_points(self.client)

        admin_logout = self.client.post("/admin/logout/")
        self.assertIn(admin_logout.status_code, (200, 302))
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.get("/home/").status_code, 200)
        self.assertTrue(self.client.cookies["sessionid"].value)

    def test_each_entry_point_uses_its_own_session_backed_csrf_secret(self):
        import re
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        pms_login_page = client.get("/login/")
        self.assertEqual(pms_login_page.status_code, 200)
        pms_match = re.search(
            rb'name="csrfmiddlewaretoken" value="([^"]+)"',
            pms_login_page.content,
        )
        self.assertIsNotNone(pms_match, pms_login_page.content)
        pms_csrf = pms_match.group(1).decode("ascii")
        pms_session_key = client.cookies["sessionid"].value

        admin_login_page = client.get("/admin/login/")
        self.assertEqual(admin_login_page.status_code, 200, admin_login_page.content)
        admin_match = re.search(
            rb'name="csrfmiddlewaretoken" value="([^"]+)"',
            admin_login_page.content,
        )
        self.assertIsNotNone(admin_match, admin_login_page.content)
        admin_csrf = admin_match.group(1).decode("ascii")
        admin_session_key = client.cookies["admin_sessionid"].value

        self.assertNotEqual(pms_csrf, admin_csrf)
        self.assertNotEqual(pms_session_key, admin_session_key)
        self.assertEqual(client.cookies["sessionid"].value, pms_session_key)

        admin_login = client.post(
            "/admin/login/",
            {
                "username": "split-admin-user",
                "password": "Admin-pass-2026!",
                "next": "/admin/",
                "csrfmiddlewaretoken": admin_csrf,
            },
        )
        self.assertNotEqual(admin_login.status_code, 403, admin_login.content)
        self.assertIn("admin_sessionid", client.cookies)

        pms_login = client.post(
            "/login/",
            {
                "username": "split-pms-user",
                "password": "Pms-pass-2026!",
                "csrfmiddlewaretoken": pms_csrf,
            },
        )
        self.assertNotEqual(pms_login.status_code, 403, pms_login.content)
        self.assertEqual(client.get("/home/").status_code, 200)
        self.assertEqual(client.get("/admin/").status_code, 200)
