"""R5：自助改密二次认证（当前密码校验 + 防爆破冷却）回归。

背景：ARCH-05《辅助功能安全检查报告》AI 复核保留意见——「改密缺二次认证」。
仓库有两条自助改密入口：/force-password-change/（强制改密）与 /system/
（员工个人页自助改密）。本模块锁定两条入口共同的契约：

  * 写入新密码前必须提供并通过「当前密码」校验（缺当前密码不得改密）；
  * 校验失败统一提示「当前密码不正确」，不区分账号不存在与密码错误；
  * 当前密码连续失败达阈值后进入冷却，冷却期内即使密码正确也拒绝；
  * 失败计数是 DB 原子计数（RateLimitCounter）：跨会话、跨进程有效，
    并发下不丢计数（2026-11-26 复核修正的实现缺陷，见下方计数落点用例）；
  * 改密成功后收尾完整：密码已更新、must_change_password 置位、
    last_password_change 更新、当前会话仍然有效（不被登出）、审计事件已写入、
    失败计数清零。
"""

import threading

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from apps.core.models import AuditEvent, Property, RateLimitCounter
from apps.core.rate_limit import DEFAULT_LIMITS
from apps.identity.models import (
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)
from apps.identity.services import (
    PASSWORD_REAUTH_COOLDOWN_SECONDS,
    PASSWORD_REAUTH_MAX_FAILURES,
    PASSWORD_REAUTH_RATE_LIMIT_ACTION,
    # 会话键构造器为历史键（保留仅为兼容旧会话数据），测试用它反证
    # 「失败计数不再落在会话里」
    _reauth_session_key,
)

# 测试专用密码：运行时随机生成，源码不落可用凭据字面量（AGENTS.md 会话纪律）
_TEST_PW_CURRENT = get_random_string(24)
_TEST_PW_NEW = get_random_string(24)
_TEST_PW_WRONG = get_random_string(24)

_ERROR_TEXT = "当前密码不正确"
_COOLDOWN_TEXT = "次数过多"


def _make_staff(username, *, must_change_password=False):
    """造 PMS 员工账号：User + DEFAULT 任职 + 岗位授权（口径同 hotelapp.tests._make_user）。

    角色组**自建**（不依赖迁移种出的「店长」组）：TransactionTestCase 的 flush 只
    重建 ContentType/Permission、不重建 Group——依赖迁移种子组的断言会因同批次
    其它用例的执行顺序与引擎跳过项不同而时绿时红（本轮在全量里实际踩到：
    前序 TransactionTestCase 已 flush 掉「店长」组 → 账号无有效岗位授权 →
    staff_required 直接拦下 → 改密请求根本没进到校验逻辑）。
    """
    user = User.objects.create_user(username=username, password=_TEST_PW_CURRENT)
    group, _ = Group.objects.get_or_create(name="r5-reauth-group")
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
    StaffRoleAssignment.objects.get_or_create(
        membership=membership, group=group, defaults={"starts_at": timezone.now()}
    )
    return user, profile


def _reauth_counter_rows():
    """当前「当前密码」失败计数的 DB 行（RateLimitCounter）。"""
    return RateLimitCounter.objects.filter(action=PASSWORD_REAUTH_RATE_LIMIT_ACTION)


def _reauth_counter_count():
    """失败计数总量（各桶计数之和）。"""
    return sum(row.count for row in _reauth_counter_rows())


class PasswordReauthPolicyTests(TestCase):
    """防爆破阈值与冷却时长不得低于安全下限（≥5 次 / ≥60 秒）。"""

    def test_threshold_and_cooldown_meet_security_floor(self):
        self.assertGreaterEqual(PASSWORD_REAUTH_MAX_FAILURES, 5)
        self.assertGreaterEqual(PASSWORD_REAUTH_COOLDOWN_SECONDS, 60)

    def test_rate_limit_config_matches_policy_constants(self):
        """限流桶的阈值/窗口必须与策略常量一致（否则计数与冷却会各走各的）。"""
        rules = DEFAULT_LIMITS[PASSWORD_REAUTH_RATE_LIMIT_ACTION]
        per_ip = [r for r in rules if r[0] == ("ip", "username")]
        self.assertTrue(per_ip, "必须有 IP+username 维度（与登录限流同口径）")
        _dims, limit, window = per_ip[0]
        self.assertEqual(limit, PASSWORD_REAUTH_MAX_FAILURES)
        self.assertEqual(window, PASSWORD_REAUTH_COOLDOWN_SECONDS)
        # 另外必须存在 username 维度的全局桶，防分布式换 IP 爆破
        self.assertTrue(
            any(r[0] == ("username",) for r in rules),
            "缺少 username 全局桶：换源地址即可绕过 IP 维度桶",
        )


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
        self.assertGreater(_reauth_counter_count(), 0, "达到阈值必须留下计数行")
        # 模拟冷却窗口过期：计数行随窗口到期被清理
        _reauth_counter_rows().delete()
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
        """改密成功后清零该用户的失败计数。"""
        self._post_change(_TEST_PW_WRONG)
        self.assertGreater(_reauth_counter_count(), 0, "失败必须留下计数")
        self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(_reauth_counter_count(), 0, "改密成功后失败计数必须清零")

    def test_failure_counter_is_not_stored_in_session(self):
        """计数落 DB 而不是会话（会话级读-改-写在并发下会丢计数）。"""
        self._post_change(_TEST_PW_WRONG)
        fail_key = _reauth_session_key(self.user, "fail")
        self.assertIsNone(
            self.client.session.get(fail_key),
            "失败计数不得落在会话里：并发请求各自读到旧值、最后写者胜出，计数会丢",
        )
        self.assertGreater(_reauth_counter_count(), 0, "失败计数必须落在 DB 计数行")

    def test_new_session_does_not_reset_failure_count(self):
        """换会话（换浏览器/清 cookie）不得重置失败计数。"""
        for _ in range(PASSWORD_REAUTH_MAX_FAILURES - 1):
            self._post_change(_TEST_PW_WRONG)
        before = _reauth_counter_count()
        other = Client()
        other.force_login(self.user)
        other.post("/system/", {
            "current_password": _TEST_PW_WRONG,
            "new_password1": _TEST_PW_NEW,
            "new_password2": _TEST_PW_NEW,
        })
        self.assertGreater(
            _reauth_counter_count(), before,
            "新会话必须共享同一失败计数（否则换会话即可无限重试）",
        )

    def test_failure_writes_audit_event(self):
        """二次认证失败必须落审计事件（事后可区分探测与正常改密）。"""
        self._post_change(_TEST_PW_WRONG)
        self.assertTrue(
            AuditEvent.objects.filter(
                action="auth.password_reauth_failure",
                entity_type="User",
                entity_id=str(self.user.pk),
                actor=self.user,
                reason="wrong_current_password",
            ).exists(),
            "二次认证失败必须写 auth.password_reauth_failure 审计",
        )

    def test_audit_event_records_request_path(self):
        """审计事件必须带请求链路信息（IP / request_id），否则无法定位来源。"""
        self._post_change(_TEST_PW_WRONG)
        event = AuditEvent.objects.filter(
            action="auth.password_reauth_failure").order_by("-id").first()
        self.assertIsNotNone(event)
        self.assertIsNotNone(event.ip, "审计事件必须记录来源 IP")
        self.assertTrue(event.request_id, "审计事件必须记录 request_id")


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


class PasswordReauthConcurrencyTests(TransactionTestCase):
    """并发失败不得丢计数（2026-11-26 复核实证缺陷的回归）。

    原实现把失败计数写在 session 里（整行覆盖保存），并发请求各自读到同一个
    旧值、各自写 fail+1，最后写者胜出 → 每批只 +1，5 次阈值在并发下形同虚设。
    现在计数落在 RateLimitCounter（DB 内 F() 原子自增），并发下必须一条不丢。

    仅在 MySQL 上跑：SQLite 的共享内存库在多线程写入下会返回
    "database is locked"，测的是引擎特性而非本模块契约。
    """

    reset_sequences = True

    def setUp(self):
        if connection.vendor != "mysql":
            self.skipTest("并发写计数用例只在 MySQL 上执行（SQLite 共享内存库不支持并发写）")
        self.user, self.profile = _make_staff("r5_concurrent")
        client = Client()
        client.force_login(self.user)
        self.session_key = client.cookies[settings.SESSION_COOKIE_NAME].value

    def _post_wrong(self):
        client = Client()
        client.cookies[settings.SESSION_COOKIE_NAME] = self.session_key
        client.post("/system/", {
            "current_password": _TEST_PW_WRONG,
            "new_password1": _TEST_PW_NEW,
            "new_password2": _TEST_PW_NEW,
        })

    def test_concurrent_failures_do_not_lose_counter(self):
        parallel = 5
        threads = [threading.Thread(target=self._post_wrong) for _ in range(parallel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertGreaterEqual(
            _reauth_counter_count(), parallel,
            "并发失败计数不得丢（旧实现每批只 +1，阈值可被并行绕过）",
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(_TEST_PW_CURRENT), "并发失败不得改密")
