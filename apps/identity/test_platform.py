"""平台身份域测试（方案 §6.4）。

覆盖 /platform/ 平台管理面门禁三项不变量、会话域隔离、CSRF、
F-10（平台路径不产生 property_context_denied 审计）、/system/ 拆分
（PMS 个人页 vs 平台环境信息页）、旧 /permissions/ 302 兼容、
platform_superadmin_check 体检命令。
"""

import io
from contextlib import redirect_stdout

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.core.models import AuditEvent, Property

from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

User = get_user_model()

# 平台门禁测试专用密码：运行时随机生成，源码不落任何凭据字面量
# （Mimosa L3 门禁：测试文件同样不得写入可用凭据字面量）。
PLATFORM_TEST_PASSWORD = get_random_string(24)


def _pms_staff_user(username="pf_staff", group_name="前台", full_name="王前台"):
    """创建 PMS 员工：User + DEFAULT 任职 + 岗位角色（不使用 is_staff/全局组）。"""
    user = User.objects.create_user(username=username, password=f"{username}-pass123")
    group = Group.objects.filter(name=group_name).first()
    prop, _ = Property.objects.get_or_create(code="DEFAULT", defaults={"name": "测试酒店"})
    profile = StaffProfile.objects.create(
        user=user, full_name=full_name, employment_status="ACTIVE"
    )
    membership = PropertyStaffMembership.objects.create(
        property=prop, staff_profile=profile, employee_no=f"E-{username}", status="ACTIVE"
    )
    if group is not None:
        StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=timezone.now()
        )
    return user


class PlatformAccessTests(TestCase):
    """/platform/* 门禁：平台会话域 + 三项不变量。"""

    def setUp(self):
        self.superadmin = User.objects.create_superuser("pf_admin", password=PLATFORM_TEST_PASSWORD)

    def _platform_login(self, user):
        """模拟平台登录：force_login 后把会话键复制到两份平台 Cookie
        （platform_sessionid @ /platform/ 与 django_admin_sessionid @ /admin/，
        同一平台 Session Key，登录完整态 S1）。"""
        self.client.force_login(user)
        sessionid = self.client.cookies["sessionid"].value
        self.client.cookies["platform_sessionid"] = sessionid
        self.client.cookies["django_admin_sessionid"] = sessionid

    def test_anonymous_redirects_to_admin_login(self):
        """无平台会话 → /platform/* 全部 302 去 /admin/login/"""
        for url in ("/platform/", "/platform/permissions/", "/platform/system/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"匿名访问 {url} 必须 302")
            self.assertTrue(
                response["Location"].startswith("/admin/login/"),
                f"匿名访问 {url} 必须跳转平台登录页，实际 {response['Location']}",
            )

    def test_pms_session_only_is_not_admitted(self):
        """仅 PMS sessionid（员工登录）→ 平台页 302，不因员工登录放行"""
        staff = _pms_staff_user()
        self.client.force_login(staff)  # 只写 sessionid，无平台 Cookie
        for url in ("/platform/", "/platform/permissions/", "/platform/system/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"PMS 员工访问 {url} 必须 302")
            self.assertTrue(response["Location"].startswith("/admin/login/"))

    def test_platform_session_with_invariants_passes(self):
        """平台会话 + 三项不变量（is_active+is_superuser+is_staff）→ 200"""
        self._platform_login(self.superadmin)
        response = self.client.get("/platform/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "平台管理")
        self.assertEqual(self.client.get("/platform/permissions/").status_code, 200)
        self.assertEqual(self.client.get("/platform/system/").status_code, 200)

    def test_platform_home_has_three_management_cards_and_cross_entry_links(self):
        self._platform_login(self.superadmin)
        response = self.client.get("/platform/")
        self.assertEqual(response.status_code, 200)
        for label in (
            "权限与角色",
            "平台运行与安全",
            "数据维护（Django Admin）",
            "/reserve/",
            "/login/",
        ):
            self.assertContains(response, label)
        self.assertNotContains(response, "审计记录")

    def test_platform_session_dual_cookie_same_key(self):
        """两份平台 Cookie 同键：门户/Admin 双入口都保持登录（§5.2）"""
        self._platform_login(self.superadmin)
        self.assertEqual(self.client.get("/platform/").status_code, 200)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(
            self.client.cookies["platform_sessionid"].value,
            self.client.cookies["django_admin_sessionid"].value,
        )

    def test_superuser_without_staff_flag_gets_403_and_audit(self):
        """is_staff=False 的 superuser（半状态）→ 403 + security.platform_denied 审计"""
        self.superadmin.is_staff = False
        self.superadmin.save(update_fields=["is_staff"])
        self._platform_login(self.superadmin)
        response = self.client.get("/platform/")
        self.assertEqual(response.status_code, 403)
        event = AuditEvent.objects.filter(
            action="security.platform_denied", entity_type="User", entity_id=str(self.superadmin.pk)
        ).first()
        self.assertIsNotNone(event, "半状态超管必须落 security.platform_denied 审计")
        self.assertIn("is_staff", event.reason)

    def test_inactive_superuser_gets_403_and_audit(self):
        """is_active=False 的 superuser（半状态）→ 403 + 审计。

        默认 ModelBackend 的 get_user 对 inactive 用户直接返回匿名（302），
        因此用 RequestFactory 直接种入 request.user 验证装饰器不变量本身
        （自定义认证后端允许 inactive 会话时必须 403+审计）。"""
        from django.core.exceptions import PermissionDenied
        from django.http import HttpResponse
        from django.test import RequestFactory

        from .platform import platform_superadmin_required

        self.superadmin.is_active = False
        self.superadmin.save(update_fields=["is_active"])

        @platform_superadmin_required
        def view(request):
            return HttpResponse("should not reach")

        request = RequestFactory().get("/platform/")
        request.user = self.superadmin
        # 直调视图：PermissionDenied 等价于经 URL 处理后的 403
        with self.assertRaises(PermissionDenied):
            view(request)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="security.platform_denied", entity_id=str(self.superadmin.pk)
            ).exists()
        )
        # 会话路径（默认后端）：inactive 超管即使持平台 Cookie 也绝不放行
        self._platform_login(self.superadmin)
        self.assertNotEqual(self.client.get("/platform/").status_code, 200)

    def test_staff_non_superuser_gets_403_without_platform_audit(self):
        """非 superuser 的 staff（is_staff=True）→ 403 直拒不跳登录，且不落平台审计"""
        staff = _pms_staff_user()
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        self._platform_login(staff)
        response = self.client.get("/platform/")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AuditEvent.objects.filter(action="security.platform_denied").exists(),
            "非超管 403 属常规拒绝，不写 security.platform_denied",
        )

    def test_half_state_platform_post_also_denied(self):
        """半状态超管 POST 保存权限 → 403（写操作同样被门禁拦截，授权不变）"""
        self.superadmin.is_staff = False
        self.superadmin.save(update_fields=["is_staff"])
        self._platform_login(self.superadmin)
        group = Group.objects.get(name="前台")
        before = set(group.permissions.values_list("codename", flat=True))
        response = self.client.post(
            "/platform/permissions/save/",
            {"role": str(group.id), "perm": ["view_room", "delete_room"]},
        )
        self.assertEqual(response.status_code, 403)
        after = set(group.permissions.values_list("codename", flat=True))
        self.assertEqual(before, after, "半状态超管的写请求不得变更角色权限")


class PlatformCsrfTests(TestCase):
    """平台 POST 的 CSRF 防护。"""

    def setUp(self):
        self.superadmin = User.objects.create_superuser("pf_csrf", password=PLATFORM_TEST_PASSWORD)
        self.group = Group.objects.get(name="前台")

    def _csrf_client(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.superadmin)
        sessionid = client.cookies["sessionid"].value
        client.cookies["platform_sessionid"] = sessionid
        client.cookies["django_admin_sessionid"] = sessionid
        return client

    def test_platform_post_without_csrf_token_is_403(self):
        """平台 POST 无 CSRF 令牌 → 403"""
        client = self._csrf_client()
        response = client.post(
            "/platform/permissions/save/", {"role": str(self.group.id), "perm": ["view_room"]}
        )
        self.assertEqual(response.status_code, 403)

    def test_platform_post_with_csrf_token_redirects_without_saving(self):
        """平台 POST 携带页面渲染的 CSRF 令牌 → 旧 form 保存入口 302 跳转且不变更授权。

        Phase 3（§8）起旧 form 保存已废弃为兼容跳转；唯一保存通道是
        /platform/api/permissions/<id>/save/（JSON API，其 CSRF 成功路径
        见 test_permission_center.PermissionCenterWriteGuardTests）。
        """
        client = self._csrf_client()
        page = client.get("/platform/permissions/")
        self.assertEqual(page.status_code, 200)
        content = page.content.decode("utf-8")
        marker = 'name="csrfmiddlewaretoken" value="'
        self.assertIn(marker, content, "权限矩阵页必须渲染 CSRF 令牌（JS 依赖）")
        token = content.split(marker, 1)[1].split('"', 1)[0]
        response = client.post(
            "/platform/permissions/save/",
            {"role": str(self.group.id), "csrfmiddlewaretoken": token, "perm": ["add_food"]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/")
        self.assertFalse(
            self.group.permissions.filter(codename="add_food").exists(),
            "旧 form 保存入口不得再变更角色权限",
        )


class PlatformContextNoiseTests(TestCase):
    """F-10：平台路径不做 PMS 物业上下文解析，不产生 property_context_denied 审计。"""

    def setUp(self):
        self.superadmin = User.objects.create_superuser("pf_noise", password=PLATFORM_TEST_PASSWORD)

    def _platform_login(self):
        self.client.force_login(self.superadmin)
        sessionid = self.client.cookies["sessionid"].value
        self.client.cookies["platform_sessionid"] = sessionid
        self.client.cookies["django_admin_sessionid"] = sessionid

    def test_platform_requests_write_no_property_context_denied(self):
        """无任职超管访问 /platform/* 与 /admin/* → 不写 property_context_denied"""
        self._platform_login()
        for url in ("/platform/", "/platform/permissions/", "/platform/system/"):
            self.client.get(url)
        self.client.get("/admin/")
        self.assertFalse(
            AuditEvent.objects.filter(action="security.property_context_denied").exists(),
            "平台请求不得产生 property_context_denied 审计噪声（F-10）",
        )

    def test_pms_page_without_membership_still_audits(self):
        """回归保护：PMS 路径的 property_context_denied 审计行为保持不变"""
        user = User.objects.create_user("no_job", password=get_random_string(20))
        self.client.force_login(user)
        self.client.get("/home/")
        self.assertTrue(
            AuditEvent.objects.filter(action="security.property_context_denied").exists(),
            "PMS 路径无任职拒绝审计必须保留（F-10 只消除平台路径噪声）",
        )


class LegacyPermissionRedirectTests(TestCase):
    """旧 /permissions/ 入口 302 兼容跳转。"""

    def test_legacy_permissions_redirects(self):
        response = self.client.get("/permissions/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/")

    def test_legacy_permissions_save_redirects(self):
        response = self.client.post("/permissions/save/", {"role": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/save/")


class SystemPageSplitTests(TestCase):
    """/system/ 拆分：PMS 员工个人页 vs /platform/system/ 平台环境信息页。"""

    def setUp(self):
        self.superadmin = User.objects.create_superuser("pf_split", password=PLATFORM_TEST_PASSWORD)
        self.staff = _pms_staff_user("pf_me", full_name="李测试")
        self.other = _pms_staff_user("pf_other", full_name="赵同事")

    def test_pms_system_page_shows_only_self(self):
        """/system/（PMS 会话）：只显示本人姓名/账号/角色/任职，不含同事信息"""
        self.client.force_login(self.staff)
        response = self.client.get("/system/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("李测试", content)
        self.assertIn("pf_me", content)
        self.assertIn("E-pf_me", content)
        self.assertIn("前台", content)
        self.assertNotIn("赵同事", content, "个人页不得出现其他员工 PII")
        self.assertNotIn("pf_other", content)

    def test_pms_system_page_has_no_platform_env_block(self):
        """/system/ 不再渲染平台环境配置（DEBUG/运行模式等已拆至 /platform/system/）"""
        self.client.force_login(self.staff)
        content = self.client.get("/system/").content.decode("utf-8")
        self.assertNotIn("系统环境配置信息", content)
        self.assertNotIn("运行模式", content)
        self.assertIn("修改我的密码", content)
        self.assertNotIn('href="/platform/permissions/"', content, "PMS 侧栏不得暴露平台权限入口")

    def test_pms_system_page_self_password_change(self):
        """/system/ 自助改密：POST 新密码 → 成功、审计、当前会话保持、可新密码登录"""
        self.client.force_login(self.staff)
        response = self.client.post(
            "/system/",
            {
                "new_password1": "brand-new-pass-9",
                "new_password2": "brand-new-pass-9",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("brand-new-pass-9"))
        self.assertTrue(
            AuditEvent.objects.filter(
                action="identity.password_changed", entity_type="StaffProfile"
            ).exists()
        )
        # 当前会话保持登录（update_session_auth_hash）
        self.assertEqual(self.client.get("/system/").status_code, 200)

    def test_pms_system_page_password_mismatch_rejected(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            "/system/", {"new_password1": "abcdef", "new_password2": "abcdeg"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "两次输入的密码不一致")
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("pf_me-pass123"))

    def test_platform_system_page_has_no_staff_pii(self):
        """/platform/system/（平台会话）：仅环境信息，无任何员工 PII 字段"""
        client = Client()
        client.force_login(self.superadmin)
        sessionid = client.cookies["sessionid"].value
        client.cookies["platform_sessionid"] = sessionid
        client.cookies["django_admin_sessionid"] = sessionid
        response = client.get("/platform/system/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        for marker in (
            "李测试",
            "赵同事",
            "pf_me",
            "pf_other",
            "E-pf_me",
            "AD-",
            "工号",
            "姓名",
            "任职",
        ):
            self.assertNotIn(marker, content, f"平台环境页不得包含 PII：{marker}")
        for env_marker in ("DEBUG", "运行环境", "时区", "会话", "安全", "部署"):
            self.assertIn(env_marker, content)
        for internal_value in ("API 基址", "admin_sessionid", "sessionid", "DEFAULT"):
            self.assertNotIn(internal_value, content)
        if settings.DEBUG:
            self.assertIn("pf-status-danger", content)
        else:
            self.assertIn("pf-status-ok", content)

    def test_pms_session_cannot_reach_platform_system_page(self):
        """PMS 员工会话访问 /platform/system/ → 302（域隔离保持）"""
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/platform/system/").status_code, 302)


class PlatformBoundaryTests(TestCase):
    """§6.1 明确禁止项：平台超管不自动获得 PMS 业务权限。"""

    def test_platform_access_grants_no_pms_authorization(self):
        """访问 /platform/* 不创建任职/角色，也不写全局权限"""
        from .authorization import AuthorizationService

        admin = User.objects.create_superuser("pf_bnd", password=PLATFORM_TEST_PASSWORD)
        prop, _ = Property.objects.get_or_create(code="DEFAULT", defaults={"name": "测试酒店"})
        client = Client()
        client.force_login(admin)
        sessionid = client.cookies["sessionid"].value
        client.cookies["platform_sessionid"] = sessionid
        client.cookies["django_admin_sessionid"] = sessionid
        client.get("/platform/")
        client.get("/platform/permissions/")
        client.get("/platform/system/")
        admin.refresh_from_db()
        self.assertFalse(
            StaffProfile.objects.filter(user=admin).exists(), "平台访问不得为超管创建员工档案/任职"
        )
        self.assertFalse(AuthorizationService.has_permission(admin, prop, "hotelapp.view_room"))
        self.assertFalse(Group.objects.filter(user=admin).exists(), "平台超管不得被写入业务角色组")

    def test_superuser_without_employment_still_blocked_from_pms(self):
        """平台会话不改变 PMS 门禁：无任职超管访问 PMS 业务页仍 403"""
        admin = User.objects.create_superuser("pf_bnd2", password=PLATFORM_TEST_PASSWORD)
        self.client.force_login(admin)  # PMS 会话（D-04 下超管拿不到平台外权限）
        self.assertEqual(self.client.get("/show_client/").status_code, 403)


class PlatformSuperadminCheckCommandTests(TestCase):
    """platform_superadmin_check 体检命令：只读报告与异常分类。"""

    def _run(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            call_command("platform_superadmin_check", *args, stdout=buf)
        return buf.getvalue()

    def test_report_classifies_healthy_and_broken_superusers(self):
        healthy = User.objects.create_superuser("chk_ok", password=PLATFORM_TEST_PASSWORD)
        broken = User.objects.create_superuser("chk_broken", password=PLATFORM_TEST_PASSWORD)
        broken.is_staff = False
        broken.save(update_fields=["is_staff"])
        inactive = User.objects.create_superuser("chk_inactive", password=PLATFORM_TEST_PASSWORD)
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        output = self._run("--dry-run")
        self.assertIn("chk_ok", output)
        self.assertIn("OK", output)
        self.assertIn("chk_broken", output)
        self.assertIn("STAFF_FLAG_MISSING", output)
        self.assertIn("chk_inactive", output)
        self.assertIn("INACTIVE_SUPERUSER", output)
        self.assertIn("异常 2", output)

    def test_dry_run_does_not_modify_data(self):
        broken = User.objects.create_superuser("chk_readonly", password=PLATFORM_TEST_PASSWORD)
        broken.is_staff = False
        broken.save(update_fields=["is_staff"])
        self._run("--dry-run")
        broken.refresh_from_db()
        self.assertTrue(broken.is_superuser, "体检不得自动修改任何账号")
        self.assertFalse(broken.is_staff, "体检不得自动修复 is_staff")

    def test_without_dry_run_still_read_only(self):
        broken = User.objects.create_superuser("chk_plain", password=PLATFORM_TEST_PASSWORD)
        broken.is_staff = False
        broken.save(update_fields=["is_staff"])
        output = self._run()
        self.assertIn("chk_plain", output)
        broken.refresh_from_db()
        self.assertFalse(broken.is_staff, "不带 --dry-run 也必须只读")

    def test_empty_platform_has_zero_anomalies(self):
        output = self._run("--dry-run")
        self.assertIn("合计 0 个 superuser", output)
        self.assertIn("全部超管满足平台身份不变量", output)
