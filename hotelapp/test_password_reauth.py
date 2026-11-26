"""R5：自助改密二次认证（当前密码校验 + 防爆破冷却）回归。

背景：ARCH-05《辅助功能安全检查报告》AI 复核保留意见——「改密缺二次认证」。
仓库有两条自助改密入口：/force-password-change/（强制改密）与 /system/
（员工个人页自助改密）。本模块锁定两条入口共同的契约：

  * 写入新密码前必须提供并通过「当前密码」校验（缺当前密码不得改密）；
  * 校验失败统一提示「当前密码不正确」，不区分账号不存在与密码错误；
  * 同一会话内当前密码连续失败达阈值后进入冷却，冷却期内即使密码正确也拒绝；
  * 改密成功后收尾完整：密码已更新、must_change_password 置位、
    last_password_change 更新、当前会话仍然有效（不被登出）、审计事件已写入、
    本会话的失败计数清零。
"""

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.core.models import AuditEvent, Property
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.identity.services import (
    PASSWORD_REAUTH_COOLDOWN_SECONDS,
    PASSWORD_REAUTH_MAX_FAILURES,
    # 会话键构造器为内部实现，测试需按同一约定读写本会话的失败计数
    _reauth_session_key,
)

# 测试专用密码：运行时随机生成，源码不落可用凭据字面量（AGENTS.md 会话纪律）
_TEST_PW_CURRENT = get_random_string(24)
_TEST_PW_NEW = get_random_string(24)
_TEST_PW_WRONG = get_random_string(24)

_ERROR_TEXT = "当前密码不正确"
_COOLDOWN_TEXT = "次数过多"


def _make_staff(username, *, must_change_password=False):
    """造 PMS 员工账号：User + DEFAULT 任职 + 岗位授权（口径同 hotelapp.tests._make_user）。"""
    user = User.objects.create_user(username=username, password=_TEST_PW_CURRENT)
    group = Group.objects.filter(name="店长").first()
    prop, _ = Property.objects.get_or_create(code="DEFAULT", defaults={"name": "测试酒店"})
    profile = StaffProfile.objects.create(
        user=user,
        full_name=username,
        employment_status="ACTIVE",
        must_change_password=must_change_password,
    )
    membership = PropertyStaffMembership.objects.create(
        property=prop, staff_profile=profile, employee_no=f"E-{username}", status="ACTIVE"
    )
    if group is not None:
        StaffRoleAssignment.objects.get_or_create(
            membership=membership, group=group, defaults={"starts_at": timezone.now()}
        )
    return user, profile


class PasswordReauthPolicyTests(TestCase):
    """防爆破阈值与冷却时长不得低于安全下限（≥5 次 / ≥60 秒）。"""

    def test_threshold_and_cooldown_meet_security_floor(self):
        self.assertGreaterEqual(PASSWORD_REAUTH_MAX_FAILURES, 5)
        self.assertGreaterEqual(PASSWORD_REAUTH_COOLDOWN_SECONDS, 60)


class SelfServicePasswordReauthTests(TestCase):
    """/system/ 员工个人页自助改密的二次认证契约。"""

    def setUp(self):
        self.user, self.profile = _make_staff("r5_me")
        self.client.force_login(self.user)

    def _post_change(self, current_password, new_password=_TEST_PW_NEW):
        return self.client.post("/system/", {
            "current_password": current_password,
            "new_password1": new_password,
            "new_password2": new_password,
        })

    def test_correct_current_password_changes_password(self):
        """当前密码正确 → 改密成功，数据库中的密码已更新。"""
        response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/system/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(_TEST_PW_NEW), "新密码必须已写入")
        self.assertFalse(self.user.check_password(_TEST_PW_CURRENT), "旧密码必须失效")

    def test_wrong_current_password_rejected_and_password_unchanged(self):
        """当前密码错误 → 拒绝，且数据库里的密码没有被改动。"""
        before = self.user.password
        response = self._post_change(_TEST_PW_WRONG)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _ERROR_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "当前密码错误时密码哈希不得变化")
        self.assertTrue(self.user.check_password(_TEST_PW_CURRENT))
        self.assertFalse(AuditEvent.objects.filter(
            action="identity.password_changed").exists(), "失败不得写改密审计")

    def test_missing_current_password_rejected(self):
        """未填当前密码 → 拒绝（空值不得绕过二次认证闸门）。"""
        before = self.user.password
        response = self.client.post("/system/", {
            "new_password1": _TEST_PW_NEW, "new_password2": _TEST_PW_NEW})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _ERROR_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "未填当前密码时密码哈希不得变化")

    def test_cooldown_blocks_even_correct_password_after_threshold(self):
        """连续失败达阈值 → 进入冷却，后续即使密码正确也拒绝。"""
        before = self.user.password
        for _ in range(PASSWORD_REAUTH_MAX_FAILURES):
            response = self._post_change(_TEST_PW_WRONG)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, _ERROR_TEXT)
        response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _COOLDOWN_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "冷却期内不得改密")
        self.assertTrue(self.user.check_password(_TEST_PW_CURRENT))

    def test_cooldown_expires_and_allows_retry(self):
        """冷却到期后重新给足配额（避免员工被永久锁死）。"""
        for _ in range(PASSWORD_REAUTH_MAX_FAILURES):
            self._post_change(_TEST_PW_WRONG)
        session = self.client.session
        lock_key = _reauth_session_key(self.user, "lock_until")
        self.assertGreater(session.get(lock_key) or 0, 0, "达到阈值必须写入冷却到期时间")
        session[lock_key] = timezone.now().timestamp() - 1  # 模拟冷却已过
        session.save()
        response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(response.status_code, 302, "冷却到期后应允许改密")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(_TEST_PW_NEW))

    def test_session_survives_successful_change(self):
        """改密成功后当前会话仍然有效（不被登出）。"""
        response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session, "改密后不得丢失登录态")
        self.assertEqual(self.client.get("/system/").status_code, 200)

    def test_audit_event_written_on_success(self):
        """改密成功后审计事件已写入。"""
        self._post_change(_TEST_PW_CURRENT)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.password_changed",
            entity_type="StaffProfile",
            entity_id=str(self.profile.pk),
            actor=self.user).exists(), "改密成功必须写 identity.password_changed 审计")

    def test_profile_flags_updated_on_success(self):
        """改密成功后 must_change_password 置位、last_password_change 更新。"""
        self._post_change(_TEST_PW_CURRENT)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.must_change_password)
        self.assertIsNotNone(self.profile.last_password_change)

    def test_failure_counter_cleared_after_successful_change(self):
        """改密成功后清零该用户在本会话中的失败计数。"""
        fail_key = _reauth_session_key(self.user, "fail")
        self._post_change(_TEST_PW_WRONG)
        self.assertEqual(self.client.session.get(fail_key), 1)
        self._post_change(_TEST_PW_CURRENT)
        self.assertIsNone(self.client.session.get(fail_key), "改密成功后失败计数必须清零")


class ForcedPasswordChangeReauthTests(TestCase):
    """/force-password-change/ 强制改密入口的二次认证契约。"""

    def setUp(self):
        self.user, self.profile = _make_staff("r5_forced", must_change_password=True)
        self.client.force_login(self.user)

    def _post_change(self, current_password, new_password=_TEST_PW_NEW):
        return self.client.post("/force-password-change/", {
            "current_password": current_password,
            "new_password1": new_password,
            "new_password2": new_password,
        })

    def test_wrong_current_password_rejected_and_password_unchanged(self):
        before = self.user.password
        response = self._post_change(_TEST_PW_WRONG)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _ERROR_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "当前密码错误时密码哈希不得变化")

    def test_missing_current_password_rejected(self):
        before = self.user.password
        response = self.client.post("/force-password-change/", {
            "new_password1": _TEST_PW_NEW, "new_password2": _TEST_PW_NEW})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _ERROR_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "未填当前密码时密码哈希不得变化")

    def test_correct_current_password_completes_forced_change(self):
        """当前密码正确 → 强制改密完成：标记清除、时间更新、审计写入、会话保留。"""
        response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/home/")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(_TEST_PW_NEW), "新密码必须已写入")
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.must_change_password)
        self.assertIsNotNone(self.profile.last_password_change)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.password_changed",
            entity_type="StaffProfile",
            entity_id=str(self.profile.pk),
            actor=self.user).exists(), "强制改密完成必须写审计事件")
        # 当前会话仍然有效（未被登出）：标记已清除后可访问个人页
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.get("/system/").status_code, 200)

    def test_cooldown_blocks_even_correct_password_after_threshold(self):
        before = self.user.password
        for _ in range(PASSWORD_REAUTH_MAX_FAILURES):
            response = self._post_change(_TEST_PW_WRONG)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, _ERROR_TEXT)
        response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _COOLDOWN_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "冷却期内不得改密")
