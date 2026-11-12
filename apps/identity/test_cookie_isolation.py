"""平台/业务 Session Cookie 网络隔离专项单元测试（§11.2-11.5 单元级）。

用 Django test client（走完整 middleware 栈）验证方案 A 的核心契约：
  * 路径分类边界（resolve_session_cookie_spec 直接测）
  * 登录响应双平台 Cookie 同键同属性（§C.1）
  * 双入口同一平台 Session、交叉不读取、不回退（§5.2、A.3.3/3.4）
  * 无效/过期 Cookie → 匿名 + 双清，区分基础设施异常（S3、§C.4）
  * Admin 原生登出与 /platform/logout/ 双清 + legacy 迁移清理（§5.4、§D）
  * PMS 登录/登出与平台 Cookie 互不影响（§5.4）
  * /permissions* 兼容跳转零平台会话、零写库 + 路由唯一性（§E、H.3）
  * CSRF_USE_SESSIONS 下 Admin 登录与平台 API 保存（§7、§11.5）
"""
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver, resolve

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402
from django.views.generic import RedirectView

from apps.core.models import AuditEvent

from .test_platform import _pms_staff_user

_TEST_PW_0F916B80 = get_random_string(24)



User = get_user_model()

CSRF_MARKER = 'name="csrfmiddlewaretoken" value="'

# 端口规格表目标默认值（§B.1；设置项见 hotel/settings.py）
PORTAL_COOKIE = "platform_sessionid"
ADMIN_COOKIE = "django_admin_sessionid"
LEGACY_COOKIE = "admin_sessionid"
PORTAL_PATH = "/platform/"
ADMIN_PATH = "/admin/"
DELETE_MAX_AGE = 0


def _morsels_by_name(response):
    """返回 cookie 名 → Morsel 列表映射（含同名不同 Path 的历史组合）。

    response.cookies 是 SimpleCookie（以存储键为键）；历史组合的删除指令
    以合成键（name@path）存放，但 Morsel.key 始终是真实 Cookie 名。
    """
    by_name = {}
    for morsel in response.cookies.values():
        by_name.setdefault(morsel.key, []).append(morsel)
    return by_name


def _delete_morsels(response, name):
    """响应中 name 的过期删除指令（Max-Age=0 且值为空）。"""
    return [
        m for m in _morsels_by_name(response).get(name, [])
        if str(m["max-age"]) == str(DELETE_MAX_AGE) and m.value == ""
    ]


def _set_morsels(response, name):
    """响应中 name 的写入指令（非空值）。"""
    return [
        m for m in _morsels_by_name(response).get(name, [])
        if m.value != ""
    ]


def _assert_platform_pair_set(response, message_prefix=""):
    """登录/轮换响应：两份平台 Set-Cookie 同键、Path 精确、HttpOnly（§C.1）。"""
    portal = _set_morsels(response, PORTAL_COOKIE)
    admin = _set_morsels(response, ADMIN_COOKIE)
    assert portal and admin, (
        message_prefix + " 响应必须同时写入两份平台 Cookie，实际 %s"
        % sorted(_morsels_by_name(response)))
    portal, admin = portal[-1], admin[-1]
    assert portal.value == admin.value != "", "两份平台 Cookie 值必须相同"
    assert portal["path"] == PORTAL_PATH, portal["path"]
    assert admin["path"] == ADMIN_PATH, admin["path"]
    assert portal["httponly"] and admin["httponly"], "平台 Cookie 必须 HttpOnly"
    return portal.value


def _assert_platform_pair_deleted(response):
    """登出/无效 Cookie 响应：两份平台 Cookie 按 Path 精确删除（§5.4）。"""
    portal = _delete_morsels(response, PORTAL_COOKIE)
    admin = _delete_morsels(response, ADMIN_COOKIE)
    assert portal, "必须发送 platform_sessionid 过期删除指令"
    assert admin, "必须发送 django_admin_sessionid 过期删除指令"
    assert portal[-1]["path"] == PORTAL_PATH
    assert admin[-1]["path"] == ADMIN_PATH


def _assert_legacy_deleted(response, paths=None):
    """历史 admin_sessionid 的每个 Name×Path 组合均有过期删除指令（§D.1）。"""
    from django.conf import settings
    paths = paths if paths is not None else settings.LEGACY_PLATFORM_COOKIE_PATHS
    deletes = _delete_morsels(response, LEGACY_COOKIE)
    assert {m["path"] for m in deletes} >= set(paths), (
        "历史组合删除指令不完整：实际 %s" % sorted(m["path"] for m in deletes))
    for morsel in deletes:
        assert morsel["max-age"] == DELETE_MAX_AGE or str(morsel["max-age"]) == "0"
        assert "1970" in morsel["expires"], morsel["expires"]


class _PlatformLoginMixin:
    """通过真实 /admin/login/ 登录流获取平台会话（不走 force_login）。"""

    def setUp(self):
        super().setUp()
        self.superadmin = User.objects.create_superuser(
            "ci_admin", password=_TEST_PW_0F916B80)

    @staticmethod
    def _admin_login(username="ci_admin", password=_TEST_PW_0F916B80):
        client = Client()
        response = client.post("/admin/login/", {
            "username": username, "password": password, "next": "/admin/"})
        assert response.status_code == 302, response.status_code
        return client, response


class CookieSpecClassificationTests(TestCase):
    """路径分类边界：规格表是唯一权威（§B.2 步骤 1、§F.1）。"""

    def _resolve(self, path):
        from .session_middleware import resolve_session_cookie_spec
        return resolve_session_cookie_spec(path)

    def test_platform_boundary_classification(self):
        """/platform 边界匹配；/platform-foo 不算平台（§F.1）"""
        for path in ("/platform", "/platform/", "/platform/permissions/",
                     "/platform/system/", "/platform/logout/"):
            self.assertEqual(self._resolve(path).category, "platform-portal",
                             path)
            self.assertEqual(self._resolve(path).name, PORTAL_COOKIE)
        for path in ("/platform-foo", "/platform-foo/", "/platformfoo/",
                     "/platformx/permissions/"):
            self.assertEqual(self._resolve(path).category, "pms", path)

    def test_admin_boundary_classification(self):
        """/admin 边界匹配；/administrator 不算（§F.1）"""
        for path in ("/admin", "/admin/", "/admin/login/", "/admin/logout/"):
            self.assertEqual(self._resolve(path).category, "platform-admin",
                             path)
            self.assertEqual(self._resolve(path).name, ADMIN_COOKIE)
        for path in ("/administrator", "/administrator/", "/adminx/"):
            self.assertEqual(self._resolve(path).category, "pms", path)

    def test_legacy_permissions_classification(self):
        """/permissions* 是兼容跳转分类，cleanup_only（§E.1）"""
        for path in ("/permissions", "/permissions/", "/permissions/save/",
                     "/permissions/save"):
            spec = self._resolve(path)
            self.assertEqual(spec.category, "legacy-redirect", path)
            self.assertTrue(spec.cleanup_only, path)
            self.assertFalse(spec.writable, path)

    def test_unknown_paths_default_pms(self):
        """未知路径默认 PMS——"未知即平台"禁止（§F.1）"""
        for path in ("/", "/login/", "/home/", "/system/", "/reserve/",
                     "/api/v1/unknown/", "/manage/staff/", "/invite/accept/",
                     "/permissionsx/"):
            spec = self._resolve(path)
            self.assertEqual(spec.category, "pms", path)
            self.assertEqual(spec.name, "sessionid", path)

    def test_spec_table_attributes(self):
        """规格表四行：名称/Path/host-only/HttpOnly/可写与仅清理（§B.1）"""
        from .session_middleware import (
            legacy_platform_cookie_spec,
            platform_admin_cookie_spec,
            platform_portal_cookie_spec,
            pms_session_cookie_spec,
        )
        portal = platform_portal_cookie_spec()
        admin = platform_admin_cookie_spec()
        legacy = legacy_platform_cookie_spec()
        pms = pms_session_cookie_spec()

        self.assertEqual(portal.name, PORTAL_COOKIE)
        self.assertEqual(portal.path, PORTAL_PATH)
        self.assertIsNone(portal.domain, "平台 Cookie 必须 host-only")
        self.assertTrue(portal.httponly)
        self.assertTrue(portal.writable)

        self.assertEqual(admin.name, ADMIN_COOKIE)
        self.assertEqual(admin.path, ADMIN_PATH)
        self.assertIsNone(admin.domain)
        self.assertTrue(admin.httponly)
        self.assertTrue(admin.writable)

        self.assertEqual(legacy.name, LEGACY_COOKIE)
        self.assertTrue(legacy.cleanup_only)
        self.assertFalse(legacy.writable)
        self.assertEqual(
            legacy.paths,
            ("/", "/admin/", "/platform/", "/permissions/",
             "/permissions/save/"))

        self.assertEqual(pms.name, "sessionid")
        self.assertEqual(pms.path, "/")
        self.assertTrue(pms.writable)
        self.assertFalse(pms.cleanup_only)

    def test_cookie_names_configurable_via_settings(self):
        """Cookie 名称可由 settings 覆盖（规格表随设置派生）"""
        with override_settings(
                PLATFORM_PORTAL_COOKIE_NAME="portal_x",
                ADMIN_SESSION_COOKIE_NAME="admin_x",
                LEGACY_PLATFORM_COOKIE_NAME="old_x",
                LEGACY_PLATFORM_COOKIE_PATHS=["/", "/old-admin/"]):
            portal = self._resolve("/platform/")
            admin = self._resolve("/admin/")
            legacy = self._resolve("/permissions/")
            self.assertEqual(portal.name, "portal_x")
            self.assertEqual(admin.name, "admin_x")
            self.assertEqual(legacy.name, "old_x")
            self.assertEqual(legacy.paths, ("/", "/old-admin/"))


class LoginAndSessionConsistencyTests(_PlatformLoginMixin, TestCase):
    """登录与双入口会话一致性（§11.2）。"""

    def test_admin_login_sets_both_platform_cookies(self):
        """登录响应同时设置两份平台 Cookie：值相同、Path 精确、HttpOnly（§C.1）"""
        client, response = self._admin_login()
        key = _assert_platform_pair_set(response, "登录")
        self.assertTrue(key)
        # 登录响应同时附带 legacy 迁移清理（幂等，§D）
        _assert_legacy_deleted(response)

    def test_both_entries_authenticated_with_same_session_key(self):
        """登录后 /platform/ 与 /admin/ 均保持登录，两份 Cookie 同键（§5.1）"""
        client, response = self._admin_login()
        key = client.cookies[PORTAL_COOKIE].value
        self.assertEqual(client.cookies[ADMIN_COOKIE].value, key)
        self.assertEqual(client.get("/platform/").status_code, 200)
        self.assertEqual(client.get("/admin/").status_code, 200)

    def test_cross_entry_no_fallback(self):
        """交叉不读取：仅门户 Cookie 访问 /admin/ → 302；仅 Admin Cookie 访问
        /platform/ → 302（不允许 Cookie 间回退，A.3.3）"""
        client, _ = self._admin_login()
        key = client.cookies[PORTAL_COOKIE].value

        portal_only = Client()
        portal_only.cookies[PORTAL_COOKIE] = key
        self.assertEqual(portal_only.get("/admin/").status_code, 302)
        self.assertEqual(portal_only.get("/platform/").status_code, 200)

        admin_only = Client()
        admin_only.cookies[ADMIN_COOKIE] = key
        self.assertEqual(admin_only.get("/platform/").status_code, 302)
        self.assertEqual(admin_only.get("/admin/").status_code, 200)

    def test_platform_cookies_not_read_on_pms_paths(self):
        """PMS 根路径只读 PMS sessionid：仅持平台 Cookie 的请求按匿名处理
        （服务端双向身份读取隔离，§2.1）"""
        client, _ = self._admin_login()
        key = client.cookies[PORTAL_COOKIE].value
        platform_only = Client()
        platform_only.cookies[PORTAL_COOKIE] = key
        platform_only.cookies[ADMIN_COOKIE] = key
        # /home/ 需 PMS 登录 → 302；/login/ 匿名可达 → 200
        self.assertEqual(platform_only.get("/home/").status_code, 302)
        self.assertEqual(platform_only.get("/login/").status_code, 200)

    def test_pms_staff_session_not_admitted_to_platform(self):
        """PMS 员工（仅 sessionid）访问平台入口 → 302，不因员工放行（§11.3）"""
        staff = _pms_staff_user("ci_staff", full_name="隔离测试员工")
        client = Client()
        client.force_login(staff)  # 只写 sessionid
        for url in ("/platform/", "/admin/", "/platform/permissions/"):
            response = client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith("/admin/login/"))

    def test_pms_login_response_has_no_platform_cookies(self):
        """PMS 登录响应不写平台 Cookie，也不发 legacy 清理（PMS 域行为不变）"""
        staff = _pms_staff_user("ci_staff2", full_name="隔离测试员工2")
        client = Client()
        response = client.post("/login/", {
            "username": staff.username, "password": "ci_staff2-pass123"})
        self.assertEqual(response.status_code, 302)
        names = {m.key for m in response.cookies.values()}
        self.assertIn("sessionid", names)
        self.assertNotIn(PORTAL_COOKIE, names)
        self.assertNotIn(ADMIN_COOKIE, names)
        self.assertNotIn(LEGACY_COOKIE, names)

    def test_server_side_revocation_kills_both_entries(self):
        """服务端撤销 Session 后两个平台入口均不可访问（§5.5）"""
        client, _ = self._admin_login()
        key = client.cookies[PORTAL_COOKIE].value
        Session.objects.filter(session_key=key).delete()
        self.assertEqual(client.get("/platform/").status_code, 302)
        self.assertEqual(client.get("/admin/").status_code, 302)

    def test_session_refresh_keeps_both_cookies_in_sync(self):
        """SESSION_SAVE_EVERY_REQUEST 续期：两份 Cookie 同步刷新且同键（§5.3）"""
        client, _ = self._admin_login()
        key = client.cookies[PORTAL_COOKIE].value
        with override_settings(SESSION_SAVE_EVERY_REQUEST=True):
            portal_resp = client.get("/platform/")
            admin_resp = client.get("/admin/")
        portal = _set_morsels(portal_resp, PORTAL_COOKIE)
        admin = _set_morsels(admin_resp, ADMIN_COOKIE)
        self.assertTrue(portal and admin)
        self.assertEqual(portal[-1].value, admin[-1].value, key)
        # 续期未轮换 Key，值保持不变（旧 Key 仍然有效）
        self.assertEqual(portal[-1].value, key)


class InvalidCookieTests(_PlatformLoginMixin, TestCase):
    """无效 Cookie：S3 匿名 + 双清，区分基础设施异常（§C.4）。"""

    def test_invalid_portal_cookie_is_anonymous_and_cleans_both(self):
        """伪造 platform_sessionid → 302 匿名 + 响应双清，不 500（§C.4.3）"""
        client = Client()
        client.cookies[PORTAL_COOKIE] = "bogus-invalid-session-value"
        response = client.get("/platform/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/admin/login/"))
        _assert_platform_pair_deleted(response)
        _assert_legacy_deleted(response)

    def test_invalid_admin_cookie_is_anonymous_and_cleans_both(self):
        """伪造 django_admin_sessionid → /admin/ 302 匿名 + 双清（§C.4.3）"""
        client = Client()
        client.cookies[ADMIN_COOKIE] = "bogus-invalid-session-value"
        response = client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        _assert_platform_pair_deleted(response)

    def test_no_fallback_to_other_platform_cookie(self):
        """门户 Cookie 无效时不回退读取 Admin Cookie（A.3.3），双清（§C.4.2）"""
        client, _ = self._admin_login()
        valid_admin_key = client.cookies[ADMIN_COOKIE].value
        fallback = Client()
        fallback.cookies[PORTAL_COOKIE] = "bogus-invalid-session-value"
        fallback.cookies[ADMIN_COOKIE] = valid_admin_key
        response = fallback.get("/platform/")
        self.assertEqual(response.status_code, 302)
        _assert_platform_pair_deleted(response)

    def test_missing_platform_cookie_is_plain_anonymous(self):
        """Cookie 缺失：匿名流程，不是异常；纯匿名响应不写平台 Cookie（H.1）"""
        response = Client().get("/platform/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(_set_morsels(response, PORTAL_COOKIE))
        self.assertFalse(_set_morsels(response, ADMIN_COOKIE))
        # 未持任何平台 Cookie → 不发送平台双清指令（不凭空推断，H.1）
        self.assertFalse(_delete_morsels(response, PORTAL_COOKIE))
        self.assertFalse(_delete_morsels(response, ADMIN_COOKIE))


class LogoutTests(_PlatformLoginMixin, TestCase):
    """登出、迁移清理与双域互不影响（§5.4、§D、§C.2）。"""

    def test_admin_logout_deletes_both_platform_cookies_and_legacy(self):
        """Django Admin 原生登出：flush → 双平台 Cookie 删除 + legacy 清理；
        服务端 Session 失效（§C.2）"""
        client, _ = self._admin_login()
        response = client.post("/admin/logout/")
        self.assertIn(response.status_code, (200, 302))
        _assert_platform_pair_deleted(response)
        _assert_legacy_deleted(response)
        # 服务端平台 Session 失效：两入口均回匿名（S6）
        self.assertEqual(client.get("/platform/").status_code, 302)
        self.assertEqual(client.get("/admin/").status_code, 302)

    def test_platform_logout_endpoint_deletes_all(self):
        """POST /platform/logout/：同款 flush→双清；跳转平台登录页（§5.4）"""
        client, _ = self._admin_login()
        response = client.post("/platform/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admin/login/")
        _assert_platform_pair_deleted(response)
        _assert_legacy_deleted(response)
        self.assertEqual(client.get("/platform/").status_code, 302)
        self.assertEqual(client.get("/admin/").status_code, 302)

    def test_platform_logout_requires_post(self):
        """/platform/logout/ 仅接受 POST（防 GET 误触发登出）"""
        client, _ = self._admin_login()
        self.assertEqual(client.get("/platform/logout/").status_code, 405)

    def test_platform_logout_gate_redirects_anonymous(self):
        """匿名 POST /platform/logout/ → 平台门禁 302，不产生 Cookie 操作"""
        response = Client().post("/platform/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/admin/login/"))
        self.assertFalse(_delete_morsels(response, PORTAL_COOKIE))
        self.assertFalse(_delete_morsels(response, ADMIN_COOKIE))

    def test_platform_logout_button_rendered(self):
        """门户导航渲染"平台登出"POST 表单 + CSRF 令牌"""
        client, _ = self._admin_login()
        response = client.get("/platform/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("平台登出", content)
        self.assertIn('action="/platform/logout/"', content)
        self.assertIn('name="csrfmiddlewaretoken"', content)

    def test_pms_logout_does_not_touch_platform_cookies(self):
        """PMS 登出只清理 PMS Session/Cookie，平台双入口仍有效（§5.4）"""
        user = _pms_staff_user("ci_both", full_name="双域员工")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["is_staff", "is_superuser"])
        client = Client()
        self.assertEqual(client.post("/login/", {
            "username": "ci_both", "password": "ci_both-pass123"}).status_code,
            302)
        pms_key = client.cookies["sessionid"].value
        self.assertEqual(client.post("/admin/login/", {
            "username": "ci_both", "password": "ci_both-pass123",
            "next": "/admin/"}).status_code, 302)
        platform_key = client.cookies[PORTAL_COOKIE].value

        # PMS 登出（GET /logout/，PMS 域）
        response = client.get("/logout/")
        self.assertEqual(response.status_code, 302)
        deletes = _delete_morsels(response, "sessionid")
        self.assertTrue(deletes, "PMS 登出必须删除 sessionid")
        self.assertFalse(_delete_morsels(response, PORTAL_COOKIE))
        self.assertFalse(_delete_morsels(response, ADMIN_COOKIE))
        # 平台 Cookie 值未被触碰；平台入口仍然有效
        self.assertEqual(client.cookies[PORTAL_COOKIE].value, platform_key)
        self.assertEqual(client.get("/platform/").status_code, 200)
        self.assertEqual(client.get("/home/").status_code, 302,
                         "PMS 会话已失效")

    def test_platform_logout_does_not_touch_pms_cookie(self):
        """平台登出只失效平台会话，PMS 会话保持登录（§5.4）"""
        user = _pms_staff_user("ci_both2", full_name="双域员工2")
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["is_staff", "is_superuser"])
        client = Client()
        self.assertEqual(client.post("/login/", {
            "username": "ci_both2", "password": "ci_both2-pass123"}).status_code,
            302)
        pms_key = client.cookies["sessionid"].value
        self.assertEqual(client.post("/admin/login/", {
            "username": "ci_both2", "password": "ci_both2-pass123",
            "next": "/admin/"}).status_code, 302)

        response = client.post("/platform/logout/")
        self.assertEqual(response.status_code, 302)
        _assert_platform_pair_deleted(response)
        # PMS sessionid 不被删除/轮换
        self.assertEqual(client.cookies["sessionid"].value, pms_key)
        self.assertFalse(_delete_morsels(response, "sessionid"))
        self.assertFalse(_set_morsels(response, "sessionid"))
        self.assertEqual(client.get("/home/").status_code, 200)


class LegacyMigrationTests(_PlatformLoginMixin, TestCase):
    """旧 admin_sessionid 迁移清理（§D、§E）。"""

    def test_every_platform_response_carries_legacy_deletes(self):
        """平台域每个响应附加全部历史 Name×Path 组合过期删除，幂等"""
        client = Client()
        first = client.get("/platform/")
        _assert_legacy_deleted(first)
        second = client.get("/platform/")
        _assert_legacy_deleted(second)
        # /admin/ 域同样附加
        third = client.get("/admin/login/")
        _assert_legacy_deleted(third)

    def test_legacy_paths_cover_known_history(self):
        """历史组合清单至少覆盖 §D.1 已知 Path"""
        from django.conf import settings
        self.assertEqual(
            set(settings.LEGACY_PLATFORM_COOKIE_PATHS),
            {"/", "/admin/", "/platform/", "/permissions/",
             "/permissions/save/"})

    def test_legacy_redirect_carries_no_platform_cookie_ops(self):
        """/permissions/ 兼容跳转：不读平台会话、不写不删平台 Cookie（§E）"""
        client, _ = self._admin_login()
        portal_key = client.cookies[PORTAL_COOKIE].value
        probe = Client()
        probe.cookies[PORTAL_COOKIE] = portal_key
        response = probe.get("/permissions/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/")
        names = {m.key for m in response.cookies.values()}
        self.assertNotIn(PORTAL_COOKIE, names)
        self.assertNotIn(ADMIN_COOKIE, names)
        self.assertNotIn(LEGACY_COOKIE, names)


class PermissionsRouteTests(_PlatformLoginMixin, TestCase):
    """/permissions* 兼容路由冻结契约（§E、H.3）。"""

    def test_permissions_save_forged_post_changes_nothing(self):
        """伪造 POST 提交 role/permissions → 302 + Group.permissions 零变化 +
        无权限变更审计（H.3 防回归）"""
        group = Group.objects.get(name="前台")
        before = set(group.permissions.values_list("codename", flat=True))
        response = Client().post("/permissions/save/", {
            "role": str(group.id), "perm": ["view_room", "delete_room"]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/save/")
        group.refresh_from_db()
        after = set(group.permissions.values_list("codename", flat=True))
        self.assertEqual(before, after, "旧入口 POST 不得变更角色权限")
        self.assertFalse(AuditEvent.objects.filter(
            action="permissions.matrix_saved").exists())

    def test_permissions_routes_resolve_to_redirectview_uniquely(self):
        """resolve('/permissions/save/') 命中 RedirectView 且同名 path 仅一条
        URL pattern（§E.2 防回归）"""

        def collect_routes(resolver, prefix=""):
            routes = []
            for pattern in resolver.url_patterns:
                if isinstance(pattern, URLPattern):
                    routes.append(prefix + str(pattern.pattern))
                elif isinstance(pattern, URLResolver):
                    routes.extend(
                        collect_routes(pattern, prefix + str(pattern.pattern)))
            return routes

        routes = collect_routes(get_resolver(None))
        self.assertEqual(routes.count("permissions/"), 1)
        self.assertEqual(routes.count("permissions/save/"), 1)
        self.assertIs(resolve("/permissions/").func.view_class, RedirectView)
        self.assertIs(resolve("/permissions/save/").func.view_class,
                      RedirectView)

    def test_platform_permissions_save_gate_then_redirect(self):
        """/platform/permissions/save/（POST）经平台门禁后 302 回矩阵页，
        不执行权限保存（H.3 冻结契约）"""
        client, _ = self._admin_login()
        group = Group.objects.get(name="前台")
        before = set(group.permissions.values_list("codename", flat=True))
        response = client.post("/platform/permissions/save/", {
            "role": str(group.id), "perm": ["view_room", "delete_room"]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/platform/permissions/")
        group.refresh_from_db()
        self.assertEqual(
            before, set(group.permissions.values_list("codename", flat=True)))


class CsrfIsolationTests(_PlatformLoginMixin, TestCase):
    """CSRF_USE_SESSIONS=True 下 Admin 与平台表单（§7、§11.5）。"""

    def _enforced_admin_login(self, username="ci_admin",
                              password=_TEST_PW_0F916B80):
        """CSRF 强制开启的真实 Admin 登录：GET 取令牌 → POST 登录。"""
        client = Client(enforce_csrf_checks=True)
        page = client.get("/admin/login/")
        self.assertEqual(page.status_code, 200)
        token = page.content.decode("utf-8").split(CSRF_MARKER, 1)[1].split(
            '"', 1)[0]
        response = client.post("/admin/login/", {
            "username": username, "password": password,
            "next": "/admin/", "csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 302, response.content)
        return client, response

    def test_admin_login_post_with_csrf_succeeds(self):
        """/admin/login/ POST CSRF 成功：CSRF Secret 存于 Admin 路径选中的
        平台 Session，登录后双 Cookie 同步（§7）"""
        client, response = self._enforced_admin_login()
        key = _assert_platform_pair_set(response, "CSRF 登录")
        self.assertEqual(client.cookies[PORTAL_COOKIE].value, key)
        self.assertEqual(client.get("/platform/").status_code, 200)

    def test_platform_logout_post_with_csrf_succeeds(self):
        """门户登出表单携带页面渲染令牌 → POST 成功双清（§11.5）"""
        client, _ = self._enforced_admin_login()
        page = client.get("/platform/")
        self.assertEqual(page.status_code, 200)
        token = page.content.decode("utf-8").split(CSRF_MARKER, 1)[1].split(
            '"', 1)[0]
        response = client.post("/platform/logout/",
                               {"csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 302)
        _assert_platform_pair_deleted(response)

    def test_platform_api_save_with_csrf_via_real_login_succeeds(self):
        """平台权限 API 保存 POST 带 CSRF 成功（JSON API 唯一保存通道，
        复用 test_permission_center 模式；登录走真实 /admin/login/ 流）"""
        client, _ = self._enforced_admin_login()
        group = Group.objects.get(name="客房服务员")
        read = client.get(f"/platform/api/permissions/{group.id}/")
        self.assertEqual(read.status_code, 200)
        payload = read.json()
        page = client.get("/platform/permissions/")
        token = page.content.decode("utf-8").split(CSRF_MARKER, 1)[1].split(
            '"', 1)[0]
        save = client.post(
            f"/platform/api/permissions/{group.id}/save/",
            data=json.dumps({
                "permissions": payload["permissions"],
                "version": payload["version"],
                "reason": "Cookie 隔离专项 CSRF 验证",
            }),
            content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(save.status_code, 200)
        self.assertTrue(save.json()["saved"])

    def test_platform_api_save_without_csrf_still_403(self):
        """缺失 CSRF Token 仍返回拒绝，不因专项放宽（§11.5）"""
        client, _ = self._enforced_admin_login()
        group = Group.objects.get(name="客房服务员")
        read = client.get(f"/platform/api/permissions/{group.id}/")
        payload = read.json()
        response = client.post(
            f"/platform/api/permissions/{group.id}/save/",
            data=json.dumps({
                "permissions": payload["permissions"],
                "version": payload["version"],
                "reason": "无令牌请求",
            }),
            content_type="application/json")
        self.assertEqual(response.status_code, 403)


class PmsBehaviorNoRegressionTests(TestCase):
    """既有 PMS 行为不回归（登录/登出/改密，§11.5）。"""

    def test_pms_login_logout_password_change_flow(self):
        staff = _pms_staff_user("ci_pms", full_name="回归员工")
        client = Client()
        # 登录
        response = client.post("/login/", {
            "username": "ci_pms", "password": "ci_pms-pass123"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(client.cookies["sessionid"].value)
        # /system/ 自助改密
        response = client.post("/system/", {
            "new_password1": "brand-new-pms-9", "new_password2": "brand-new-pms-9"})
        self.assertEqual(response.status_code, 302)
        staff.refresh_from_db()
        self.assertTrue(staff.check_password("brand-new-pms-9"))
        # 当前会话保持（update_session_auth_hash）
        self.assertEqual(client.get("/system/").status_code, 200)
        # 登出 → 302 去 /login/
        self.assertEqual(client.get("/logout/").status_code, 302)
        self.assertEqual(client.get("/system/").status_code, 302)

    def test_pms_logout_response_deletes_only_sessionid(self):
        staff = _pms_staff_user("ci_pms2", full_name="回归员工2")
        client = Client()
        client.force_login(staff)
        response = client.get("/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(_delete_morsels(response, "sessionid"))
        names = {m.key for m in response.cookies.values()}
        self.assertNotIn(PORTAL_COOKIE, names)
        self.assertNotIn(ADMIN_COOKIE, names)
        self.assertNotIn(LEGACY_COOKIE, names)


class SettingsDefaultsSanityTests(TestCase):
    """防呆：规格表目标默认名与设置一致（避免测试漂移）。"""

    def test_settings_match_target_defaults(self):
        from django.conf import settings
        self.assertEqual(settings.PLATFORM_PORTAL_COOKIE_NAME, PORTAL_COOKIE)
        self.assertEqual(settings.ADMIN_SESSION_COOKIE_NAME, ADMIN_COOKIE)
        self.assertEqual(settings.LEGACY_PLATFORM_COOKIE_NAME, LEGACY_COOKIE)
