"""改密写路径收口回归（2026-11-28 结构审查 P1 整改）。

历史实现里 `/force-password-change/`（D-07 强制改密）与 `/system/`（员工个人页
自助改密）各自复制了一份「set_password → 清强制改密标记 → 审计 → 保会话 →
清失败计数」写块——违反 `apps/identity/services.py` 头注自宣告的写边界
（「View/API 不应直接写 User/Profile」）。收口后唯一写入口是
`apps.identity.services.apply_password_change`。本模块锁定两件事：

  ① 服务写副作用的完整语义（含缺档与无会话两个边界）；
  ② 两个视图必须经由服务落盘——视图源码里再出现直写即测试失败（守卫）。

行为级等价性（审计/标记/计数/会话存活）由 hotelapp.test_password_reauth 的
既有用例覆盖，本模块不重复断言同一行为。
"""

import inspect

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase
from django.utils.crypto import get_random_string
from hotelapp import views as hotelapp_views

from apps.core.models import AuditEvent
from apps.identity.models import StaffProfile
from apps.identity.services import apply_password_change

User = get_user_model()

# 测试专用密码：运行时随机生成，源码不落可用凭据字面量（AGENTS.md 会话纪律）
_TEST_PW_NEW = get_random_string(24)


class ApplyPasswordChangeServiceTests(TestCase):
    """apply_password_change 服务单测：写副作用语义与边界。"""

    def setUp(self):
        self.user = User.objects.create_user(  # type: ignore[attr-defined]
            username=get_random_string(12), password=get_random_string(24)
        )
        self.profile = StaffProfile.objects.create(
            user=self.user,
            full_name="收口测试",
            employment_status="ACTIVE",
            must_change_password=True,
        )

    def _make_request(self):
        """带已登录 DB 会话的请求（RequestFactory 不自带 session）。

        会话预置 _auth_user_id/_auth_user_backend/_auth_user_hash 三键，模拟
        「已登录员工」——update_session_auth_hash 轮换后 get_user 应仍取回本用户。
        """
        request = RequestFactory().post("/force-password-change/")
        session = SessionStore()
        session["_auth_user_id"] = str(self.user.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session["_auth_user_hash"] = self.user.get_session_auth_hash()
        session.save()
        request.session = session
        request.user = self.user
        return request

    def test_full_side_effects_written(self):
        """改密落盘：口令哈希更新、强制标记清除、审计写入、会话保活、计数复位。"""
        request = self._make_request()
        old_password_hash = self.user.password
        before_audit = AuditEvent.objects.filter(action="identity.password_changed").count()

        apply_password_change(request, self.user, _TEST_PW_NEW, reason="unit_test")

        self.user.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertNotEqual(self.user.password, old_password_hash, "口令哈希必须更新")
        self.assertTrue(self.user.check_password(_TEST_PW_NEW), "新口令必须生效")
        self.assertFalse(self.profile.must_change_password, "强制改密标记必须清除")
        self.assertIsNotNone(self.profile.last_password_change)
        event = AuditEvent.objects.filter(action="identity.password_changed").latest("id")
        self.assertEqual(
            AuditEvent.objects.filter(action="identity.password_changed").count(),
            before_audit + 1,
            "必须恰好写一条改密审计",
        )
        self.assertEqual(event.entity_type, "StaffProfile")
        self.assertEqual(event.entity_id, str(self.profile.pk))
        self.assertEqual(event.reason, "unit_test")
        self.assertEqual(event.actor_id, self.user.pk)
        # 会话保活：session_auth_hash 轮换后当前会话仍映射到本用户
        from django.contrib.auth import get_user

        request.session.save()
        self.assertEqual(get_user(request).pk, self.user.pk, "会话必须保持登录态")

    def test_missing_profile_falls_back_to_user_pk(self):
        """无 StaffProfile 时不得报错：审计 entity 退回 User 主键（自助改密历史口径）。"""
        self.profile.delete()
        request = self._make_request()

        apply_password_change(request, self.user, _TEST_PW_NEW, reason="no_profile")

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(_TEST_PW_NEW))
        event = AuditEvent.objects.filter(action="identity.password_changed").latest("id")
        self.assertEqual(event.entity_id, str(self.user.pk))
        self.assertEqual(event.entity_type, "StaffProfile")

    def test_request_none_skips_session_side_effects(self):
        """request=None（非 HTTP 调用方）不触碰会话与限流，落盘与审计照常。"""
        old_hash = self.user.password

        apply_password_change(None, self.user, _TEST_PW_NEW, reason="headless")

        self.user.refresh_from_db()
        self.assertNotEqual(self.user.password, old_hash)
        self.assertTrue(self.user.check_password(_TEST_PW_NEW))
        self.assertTrue(
            AuditEvent.objects.filter(
                action="identity.password_changed", reason="headless"
            ).exists()
        )
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.must_change_password)


class PasswordWritePathGuardTests(TestCase):
    """视图源码守卫：两个改密视图必须经由服务落盘，不得再出现直写。"""

    def test_force_password_change_delegates_to_service(self):
        source = inspect.getsource(hotelapp_views.force_password_change)
        self.assertNotIn("set_password", source, "强制改密视图不得直写口令")
        self.assertIn("apply_password_change", source, "强制改密视图必须走服务唯一入口")

    def test_system_info_delegates_to_service(self):
        source = inspect.getsource(hotelapp_views.system_info)
        self.assertNotIn("set_password", source, "自助改密视图不得直写口令")
        self.assertIn("apply_password_change", source, "自助改密视图必须走服务唯一入口")
        self.assertNotIn("update_session_auth_hash", source, "会话保活必须由服务统一处理")

    def test_views_module_has_no_password_write_left(self):
        """整模块兜底：hotelapp/views.py 不得残留任何 set_password 调用点。"""
        source = inspect.getsource(hotelapp_views)
        self.assertNotIn(
            "set_password",
            source,
            "hotelapp/views.py 已无合法口令直写点（写路径全部收口到 identity 服务）",
        )
