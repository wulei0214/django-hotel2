# -*- coding: utf-8 -*-
"""员工中心写操作迁移测试（治理方案 §10.1）。

覆盖：
  * /manage/staff/ 员工生命周期工作台（店长 200 / 无 staff_manage 403 / 匿名 302 登录）；
  * 六个写操作（create / provision / assign_role / end_assignment / set_status /
    reset_password）店长成功路径：HTTP 200 + EmployeeLifecycleService DB 副作用 +
    AuditEvent 审计事件；
  * EmployeeLifecycleError 输入错 → HTTP 400 + 中文提示；
  * 迁移等价性：provision / reset 后 must_change_password=True（D-07 首登强制改密）；
  * 旧 /staff_accounts/ 写路由退役：六个旧写 URL POST → 302 /manage/staff/，
    且不再产生任何写副作用；
  * 旧 /staff_accounts/ 列表与详情页保留只读 200；
  * CSRF 缺失 → 403（页面域端点同样强制 CSRF）。
"""
from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, Property

from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from .services import EmployeeLifecycleService

DEFAULT_PROPERTY = None


def _make_user(username, group_name="店长"):
    """创建员工测试账号：User + DEFAULT Membership + RoleAssignment（同 hotelapp.tests 口径）。"""
    prop, _ = Property.objects.get_or_create(
        code="DEFAULT", defaults={"name": "测试酒店"})
    group = Group.objects.filter(name=group_name).first()
    user = User.objects.create_user(username=username,
                                    password=f"{username}-pass123")
    profile = StaffProfile.objects.create(
        user=user, full_name=username, employment_status="ACTIVE")
    membership = PropertyStaffMembership.objects.create(
        property=prop, staff_profile=profile,
        employee_no=f"E-{username}", status="ACTIVE")
    if group is not None:
        StaffRoleAssignment.objects.get_or_create(
            membership=membership, group=group,
            defaults={"starts_at": timezone.now()})
    return user


def _make_target_employee(employee_no, with_account=True):
    """被管理目标员工：店长以外，供六个写操作操作。"""
    kwargs = {}
    if with_account:
        kwargs = {"account_username": f"emp_{employee_no.replace('-', '')}",
                  "account_password": "target-original-pass"}
    return EmployeeLifecycleService.create_employee(
        property_obj=Property.objects.get(code="DEFAULT"),
        full_name=f"员工{employee_no}", employee_no=employee_no,
        role_groups=[Group.objects.get(name="前台")] if with_account else [],
        **kwargs)


class StaffCenterAccessTests(TestCase):
    """§10.1 步骤 1：员工中心页面门禁（PMS 会话域 + identity.staff_manage）。"""

    @classmethod
    def setUpTestData(cls):
        cls.owner = _make_user("sc_owner")
        cls.fd = _make_user("sc_front", "前台")

    def test_center_page_manager_ok(self):
        """店长（identity.staff_manage）打开员工中心 → 200 且使用专属模板。"""
        self.client.force_login(self.owner)
        response = self.client.get("/manage/staff/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pms/staff_center.html")
        self.assertContains(response, "/api/v1/staff")

    def test_center_page_forbidden_without_staff_manage(self):
        """无 identity.staff_manage（前台岗位）→ 403。"""
        self.client.force_login(self.fd)
        response = self.client.get("/manage/staff/")
        self.assertEqual(response.status_code, 403)

    def test_center_page_anonymous_redirects_to_login(self):
        """匿名访问 → 302 跳登录页（PMS 会话域不用平台 anonymous 契约）。"""
        response = self.client.get("/manage/staff/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_action_anonymous_redirects_to_login(self):
        """匿名 POST 写端点 → 302 登录（测试客户端默认不强制 CSRF）。"""
        response = self.client.post("/manage/staff/actions/create/", {
            "full_name": "匿名者", "employee_no": "F-9001"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])


class StaffCenterWriteTests(TestCase):
    """§10.1 六个写操作：成功路径（DB 副作用 + AuditEvent）与 400 中文校验。"""

    @classmethod
    def setUpTestData(cls):
        cls.owner = _make_user("sc_writer")

    def setUp(self):
        self.client.force_login(self.owner)
        self.property_obj = Property.objects.get(code="DEFAULT")

    def test_create_employee_success_and_audit(self):
        """a. 新增员工：建档 + DEFAULT 任职 + 初始角色 + identity.employee_create 审计。"""
        response = self.client.post("/manage/staff/actions/create/", {
            "full_name": "中心新员工", "employee_no": "F-8801",
            "position_code": "FO", "phone": "", "roles": ["前台"],
        })
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["data"]["message"])
        profile = StaffProfile.objects.get(full_name="中心新员工")
        membership = PropertyStaffMembership.objects.get(
            staff_profile=profile, property=self.property_obj)
        self.assertEqual(membership.employee_no, "F-8801")
        self.assertEqual(membership.status, PropertyStaffMembership.Status.ACTIVE)
        self.assertTrue(StaffRoleAssignment.objects.filter(
            membership=membership, group__name="前台", ends_at__isnull=True).exists())
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.employee_create", entity_type="StaffProfile",
            entity_id=str(profile.pk), actor=self.owner).exists())

    def test_create_employee_generated_password_shown_once(self):
        """a-可选开通账号：留空密码 → 服务端生成强初始密码，仅本次响应显示一次。"""
        response = self.client.post("/manage/staff/actions/create/", {
            "full_name": "自动密码员工", "employee_no": "F-8802",
            "username": "emp_gen01", "password": "",
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("initial_password", data, "生成式初始密码必须随本次响应返回")
        user = User.objects.get(username="emp_gen01")
        self.assertTrue(user.check_password(data["initial_password"]))
        profile = StaffProfile.objects.get(full_name="自动密码员工")
        self.assertTrue(profile.must_change_password, "开通账号必须强制首登改密（D-07）")
        self.assertFalse(user.is_staff, "D-04：员工中心开账号不发放 is_staff")
        # 明文只出现在本次响应，不落库不落审计（Python 侧扫描，兼容 SQLite）
        for event in AuditEvent.objects.all():
            blob = f"{event.after_json or ''}{event.before_json or ''}{event.reason or ''}"
            self.assertNotIn(data["initial_password"], str(blob),
                             "初始密码明文不得写入任何审计字段")

    def test_create_employee_duplicate_no_rejected_400_chinese(self):
        """a-校验：同店工号重复 → 400 + 中文提示（EmployeeLifecycleError 转换）。"""
        self.client.post("/manage/staff/actions/create/", {
            "full_name": "工号占用甲", "employee_no": "F-8803"})
        response = self.client.post("/manage/staff/actions/create/", {
            "full_name": "工号占用乙", "employee_no": "F-8803"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("已存在", response.json()["error"]["message"])
        self.assertEqual(StaffProfile.objects.filter(
            full_name__in=["工号占用甲", "工号占用乙"]).count(), 1,
            "失败的重复建档不得留下第二条档案")

    def test_create_employee_auto_number_when_left_blank(self):
        """WB-037：工号留空 + 部门代码 → DEC-EMP-02 自动编号（FO-0001 起递增）。"""
        response = self.client.post("/manage/staff/actions/create/", {
            "full_name": "自动编号员工一", "employee_no": "",
            "department_code": "FO", "position_code": "FO",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["employee_no"], "FO-0001")
        # 同部门第二名员工流水号原子递增
        response2 = self.client.post("/manage/staff/actions/create/", {
            "full_name": "自动编号员工二", "employee_no": "",
            "department_code": "FO",
        })
        self.assertEqual(response2.status_code, 200)
        self.assertEqual(response2.json()["data"]["employee_no"], "FO-0002")
        membership = PropertyStaffMembership.objects.get(
            staff_profile=StaffProfile.objects.get(full_name="自动编号员工二"),
            property=self.property_obj)
        self.assertEqual(membership.employee_no, "FO-0002")
        # 工号与部门代码都缺失且岗位不可映射 → 400 中文提示
        response3 = self.client.post("/manage/staff/actions/create/", {
            "full_name": "无部门员工", "employee_no": "",
            "position_code": "UNASSIGNED",
        })
        self.assertEqual(response3.status_code, 400)
        self.assertIn("部门代码", response3.json()["error"]["message"])
        self.assertFalse(StaffProfile.objects.filter(
            full_name="无部门员工").exists())

    def test_provision_account_success_must_change_and_audit(self):
        """b. 开通账号：账号可认证 + must_change_password=True + identity.account_provision 审计。"""
        profile, _, _ = _make_target_employee("F-8804", with_account=False)
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/provision/", {
            "username": "emp_8804", "password": "provision-pass1"})
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="emp_8804")
        profile.refresh_from_db()
        self.assertEqual(profile.user, user)
        self.assertTrue(profile.must_change_password,
                        "迁移等价性：provision 后必须强制改密")
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.account_provision", entity_type="StaffProfile",
            entity_id=str(profile.pk), actor=self.owner).exists())

    def test_provision_account_duplicate_username_400(self):
        """b-校验：登录账号被占用 → 400 中文提示，且不产生新 User。"""
        profile, _, _ = _make_target_employee("F-8805", with_account=False)
        before = User.objects.count()
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/provision/", {
            "username": "sc_writer", "password": "another-pass1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("已被占用", response.json()["error"]["message"])
        self.assertEqual(User.objects.count(), before)

    def test_assign_role_success_and_audit(self):
        """c. 角色分配：授权生效 + identity.role_assign 审计。"""
        profile, _, _ = _make_target_employee("F-8806")
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/assign_role/", {
            "role": "客房服务员", "reason": "客房支援"})
        self.assertEqual(response.status_code, 200)
        membership = PropertyStaffMembership.objects.get(staff_profile=profile)
        assignment = StaffRoleAssignment.objects.filter(
            membership=membership, group__name="客房服务员",
            ends_at__isnull=True).first()
        self.assertIsNotNone(assignment)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.role_assign", entity_type="StaffProfile",
            entity_id=str(profile.pk)).exists())

    def test_assign_role_invalid_role_400_chinese(self):
        """c-校验：目录外角色（伪造 Group）→ 400 中文提示，不产生授权。"""
        profile, _, _ = _make_target_employee("F-8807")
        Group.objects.create(name="Unapproved Global Admin")
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/assign_role/", {
            "role": "Unapproved Global Admin", "reason": "伪造角色提交"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("岗位目录", response.json()["error"]["message"])
        membership = PropertyStaffMembership.objects.get(staff_profile=profile)
        self.assertFalse(StaffRoleAssignment.objects.filter(
            membership=membership, group__name="Unapproved Global Admin").exists())

    def test_end_assignment_success_and_audit(self):
        """d. 结束任职授权：ends_at 落定 + identity.role_end 审计 + 历史保留。"""
        profile, _, _ = _make_target_employee("F-8808")
        membership = PropertyStaffMembership.objects.get(staff_profile=profile)
        assignment = StaffRoleAssignment.objects.get(
            membership=membership, group__name="前台")
        response = self.client.post(
            f"/manage/staff/actions/{profile.pk}/end_assignment/{assignment.pk}/",
            {"reason": "调岗至客房"})
        self.assertEqual(response.status_code, 200)
        assignment.refresh_from_db()
        self.assertIsNotNone(assignment.ends_at, "结束授权必须写 ends_at（历史不删除）")
        self.assertTrue(StaffRoleAssignment.objects.filter(pk=assignment.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.role_end", entity_type="StaffProfile",
            entity_id=str(profile.pk)).exists())

    def test_set_status_suspend_then_restore(self):
        """e. 任职状态：暂停 → Membership SUSPENDED；恢复 → ACTIVE。"""
        profile, _, _ = _make_target_employee("F-8809")
        suspend = self.client.post(f"/manage/staff/actions/{profile.pk}/set_status/", {
            "status": "SUSPENDED", "reason": "停职检查"})
        self.assertEqual(suspend.status_code, 200)
        membership = PropertyStaffMembership.objects.get(staff_profile=profile)
        profile.refresh_from_db()
        self.assertEqual(membership.status, PropertyStaffMembership.Status.SUSPENDED)
        self.assertEqual(profile.employment_status, "INACTIVE")
        restore = self.client.post(f"/manage/staff/actions/{profile.pk}/set_status/", {
            "status": "ACTIVE", "reason": "复职"})
        self.assertEqual(restore.status_code, 200)
        membership.refresh_from_db()
        profile.refresh_from_db()
        self.assertEqual(membership.status, PropertyStaffMembership.Status.ACTIVE)
        self.assertEqual(profile.employment_status, "ACTIVE")
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.employment_status", entity_type="StaffProfile",
            entity_id=str(profile.pk), actor=self.owner).count() >= 2)

    def test_set_status_invalid_status_400(self):
        """e-校验：未知状态 → 400，档案不变。"""
        profile, _, _ = _make_target_employee("F-8810")
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/set_status/", {
            "status": "WHATEVER"})
        self.assertEqual(response.status_code, 400)
        profile.refresh_from_db()
        self.assertEqual(profile.employment_status, "ACTIVE")

    def test_reset_password_must_change_and_old_password_invalid(self):
        """f. 密码重置：must_change_password=True + 旧密码失效（旧会话失效语义）+ 审计。"""
        profile, _, user = _make_target_employee("F-8811")
        self.assertTrue(user.check_password("target-original-pass"))
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/reset_password/", {
            "password": "brand-new-pass9"})
        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        user.refresh_from_db()
        self.assertTrue(profile.must_change_password,
                        "迁移等价性：reset 后必须强制改密（旧会话失效语义）")
        self.assertFalse(user.check_password("target-original-pass"))
        self.assertTrue(user.check_password("brand-new-pass9"))
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.password_reset", entity_type="StaffProfile",
            entity_id=str(profile.pk), actor=self.owner).exists())

    def test_reset_password_without_account_400_chinese(self):
        """f-校验：未开通账号的员工 → 400 中文提示。"""
        profile, _, _ = _make_target_employee("F-8812", with_account=False)
        response = self.client.post(f"/manage/staff/actions/{profile.pk}/reset_password/", {
            "password": "whatever-pass1"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未开通登录账号", response.json()["error"]["message"])


class OldStaffAccountsRetiredTests(TestCase):
    """§10.1 步骤 7：旧 /staff_accounts/ 写入口退役，只读页保留。"""

    @classmethod
    def setUpTestData(cls):
        cls.owner = _make_user("sc_retire_owner")
        cls.profile, cls.membership, cls.user = _make_target_employee("F-8890")
        cls.assignment = StaffRoleAssignment.objects.get(
            membership=cls.membership, group__name="前台")

    OLD_WRITE_URLS = [
        "/staff_accounts/create/",
        "/staff_accounts/{pk}/provision/",
        "/staff_accounts/{pk}/assign_role/",
        "/staff_accounts/{pk}/end_assignment/{assignment_id}/",
        "/staff_accounts/{pk}/set_status/",
        "/staff_accounts/{pk}/reset_password/",
    ]

    def _urls(self):
        return [u.format(pk=self.profile.pk, assignment_id=self.assignment.pk)
                for u in self.OLD_WRITE_URLS]

    def test_old_write_routes_redirect_302_to_staff_center(self):
        """六个旧写路由 POST → 302 /manage/staff/。"""
        self.client.force_login(self.owner)
        for url in self._urls():
            response = self.client.post(url, {"status": "TERMINATED"})
            self.assertEqual(response.status_code, 302, url)
            self.assertEqual(response["Location"], "/manage/staff/", url)

    def test_old_write_routes_have_no_write_side_effect(self):
        """旧写路由不再有直接写行为：全量 POST 后员工/授权/账号状态不变。"""
        self.client.force_login(self.owner)
        users_before = User.objects.count()
        profiles_before = StaffProfile.objects.count()
        for url in self._urls():
            self.client.post(url, {
                "full_name": "旧入口幽灵员工", "employee_no": "F-GHOST",
                "username": "ghost_user", "password": "ghost-pass1",
                "status": "TERMINATED", "role": "店长"})
        self.user.refresh_from_db()
        self.profile.refresh_from_db()
        self.assignment.refresh_from_db()
        self.assertEqual(User.objects.count(), users_before)
        self.assertEqual(StaffProfile.objects.count(), profiles_before)
        self.assertFalse(StaffProfile.objects.filter(full_name="旧入口幽灵员工").exists())
        self.assertFalse(User.objects.filter(username="ghost_user").exists())
        self.assertIsNone(self.assignment.ends_at, "旧入口不得结束授权")
        self.assertEqual(self.profile.employment_status, "ACTIVE")
        self.assertTrue(self.user.is_active)
        self.assertFalse(self.user.check_password("ghost-pass1"))

    def test_old_list_and_detail_pages_remain_readonly_200(self):
        """/staff_accounts/ 列表与详情保留只读 200（标注已迁移）。"""
        self.client.force_login(self.owner)
        response = self.client.get("/staff_accounts/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/manage/staff/")
        detail = self.client.get(f"/staff_accounts/{self.profile.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "只读存档")
        self.assertNotContains(detail, "end_assignment/")

    def test_old_list_forbidden_without_staff_manage(self):
        """无权限者访问旧只读页仍 403（门禁与迁移前一致）。"""
        fd = _make_user("sc_retire_front", "前台")
        self.client.force_login(fd)
        self.assertEqual(self.client.get("/staff_accounts/").status_code, 403)


class StaffCenterSecurityTests(TestCase):
    """§10.1 门禁与 CSRF：页面域写端点强制 CSRF、越权 403、越店 404。"""

    @classmethod
    def setUpTestData(cls):
        cls.owner = _make_user("sc_sec_owner")
        cls.fd = _make_user("sc_sec_front", "前台")

    def test_csrf_missing_returns_403(self):
        """CSRF 缺失 → 403（PMS 会话域端点不接受无令牌写请求）。"""
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        response = strict.post("/manage/staff/actions/create/", {
            "full_name": "无令牌员工", "employee_no": "F-8820"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(StaffProfile.objects.filter(full_name="无令牌员工").exists())

    def test_action_forbidden_without_staff_manage(self):
        """无 identity.staff_manage 的 PMS 员工调用写端点 → 403。"""
        self.client.force_login(self.fd)
        response = self.client.post("/manage/staff/actions/create/", {
            "full_name": "越权员工", "employee_no": "F-8821"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(StaffProfile.objects.filter(full_name="越权员工").exists())

    def test_action_cross_property_profile_404(self):
        """不属于当前物业的员工 → 404（数据范围隔离）。"""
        self.client.force_login(self.owner)
        other_prop = Property.objects.create(
            code="OTHER-SC", name="其他物业", status=Property.Status.ACTIVE)
        other_profile = StaffProfile.objects.create(
            user=None, full_name="别店员工", employment_status="ACTIVE")
        PropertyStaffMembership.objects.create(
            property=other_prop, staff_profile=other_profile,
            employee_no="OTHER-001", status="ACTIVE")
        response = self.client.post(
            f"/manage/staff/actions/{other_profile.pk}/reset_password/", {
                "password": "cross-prop-pass1"})
        self.assertEqual(response.status_code, 404,
                         "当前物业之外的员工档案不得通过员工中心重置")


class TerminatedEmployeeResetGuardTests(TestCase):
    """离职/停用员工不能重置密码（用户报告优化：按钮置灰 + 服务端守卫）。"""

    def setUp(self):
        self.prop = Property.objects.get(code="DEFAULT")
        self.manager = _make_user("term-mgr", "店长")

    def _make_terminated(self, username):
        user = User.objects.create_user(username=username, password="OldPass-2026!")
        user.is_active = False
        user.save(update_fields=("is_active",))
        profile = StaffProfile.objects.create(
            user=user, full_name="离职员工", employment_status="TERMINATED")
        PropertyStaffMembership.objects.create(
            property=self.prop, staff_profile=profile,
            employee_no="E-" + username, status="TERMINATED")
        return profile, user

    def test_service_rejects_terminated_employee(self):
        from apps.identity.services import (
            EmployeeLifecycleError, EmployeeLifecycleService)
        profile, user = self._make_terminated("term-reset-1")
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.reset_password(
                profile, property_obj=self.prop,
                new_password="Str0ng!New2026", reset_by=self.manager)
        user.refresh_from_db()
        self.assertTrue(user.check_password("OldPass-2026!"),
                        "被拒绝的重置不得改变旧密码")

    def test_service_rejects_deactivated_account(self):
        from apps.identity.services import (
            EmployeeLifecycleError, EmployeeLifecycleService)
        profile, user = self._make_terminated("term-reset-2")
        profile.employment_status = "ACTIVE"
        profile.save(update_fields=("employment_status",))
        with self.assertRaises(EmployeeLifecycleError):
            EmployeeLifecycleService.reset_password(
                profile, property_obj=self.prop,
                new_password="Str0ng!New2026", reset_by=self.manager)

    def test_view_returns_400_for_terminated_employee(self):
        self.client.force_login(self.manager)
        profile, user = self._make_terminated("term-reset-3")
        response = self.client.post(
            "/manage/staff/actions/%d/reset_password/" % profile.pk,
            {"password": "Str0ng!New2026"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("已离职", response.json()["error"]["message"])
