"""MC-005 员工档案资料维护：复用 EmployeeLifecycleService，旧事实不变。"""

from django.test import TestCase

from apps.core.models import AuditEvent, Property
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

from .test_staff_center import _make_user


class StaffProfileUpdateTests(TestCase):
    def setUp(self):
        self.property_obj = Property.objects.get(code="DEFAULT")
        self.owner = _make_user("sc_profile_owner")
        self.target = _make_user("sc_profile_target", "前台")
        self.target_profile = StaffProfile.objects.get(user=self.target)
        self.client.force_login(self.owner)

    def test_update_profile_does_not_change_membership_or_roles(self):
        membership = PropertyStaffMembership.objects.get(
            staff_profile=self.target_profile, property=self.property_obj
        )
        original_no = membership.employee_no
        original_status = membership.status
        role_count = StaffRoleAssignment.objects.filter(membership=membership).count()
        response = self.client.post(
            f"/manage/staff/actions/{self.target_profile.pk}/update_profile/",
            {
                "full_name": "更正姓名",
                "phone": "13800009999",
                "position_code": "FO-Reception-Lead",
                "reason": "入职资料录入有误，更正姓名与岗位编码",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["changed"])
        self.target_profile.refresh_from_db()
        membership.refresh_from_db()
        self.assertEqual(self.target_profile.full_name, "更正姓名")
        self.assertEqual(self.target_profile.phone, "13800009999")
        self.assertEqual(membership.position_code, "FO-Reception-Lead")
        self.assertEqual(membership.employee_no, original_no)
        self.assertEqual(membership.status, original_status)
        self.assertEqual(
            StaffRoleAssignment.objects.filter(membership=membership).count(), role_count
        )
        self.assertTrue(
            AuditEvent.objects.filter(
                property=self.property_obj,
                action="identity.employee_profile_update",
                entity_type="StaffProfile",
                entity_id=self.target_profile.pk,
            ).exists()
        )

    def test_reason_is_required(self):
        response = self.client.post(
            f"/manage/staff/actions/{self.target_profile.pk}/update_profile/",
            {"full_name": "无原因改名", "reason": ""},
        )
        self.assertEqual(response.status_code, 400)

    def test_front_desk_cannot_update_profile(self):
        self.client.force_login(self.target)
        response = self.client.post(
            f"/manage/staff/actions/{self.target_profile.pk}/update_profile/",
            {"full_name": "越权改名", "reason": "前台越权测试"},
        )
        self.assertEqual(response.status_code, 403)

    def test_staff_center_template_exposes_profile_action(self):
        response = self.client.get("/manage/staff/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/update_profile/")
        self.assertContains(response, "档案资料维护")
