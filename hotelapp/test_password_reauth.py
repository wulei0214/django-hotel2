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

import contextlib
import threading
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

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


@contextlib.contextmanager
def _frozen_limit_window(offset_seconds=0):
    """把限流组件的**生效时钟**固定住（测试用上下文管理器，产出可控时钟对象）。

    背景：`apps/core/rate_limit.py` 的计数按**滑动窗口**累计——计数行在最后一次
    计数之后再存活 window_seconds，跨窗口边界不清零（2026-11-27 整改，堵住
    「冷却期被窗口边界截断」的绕过口子）。冷却用例要在同一计数周期内连续失败
    5 次再验证「第 6 次即使口令正确也被拦」，若时钟恰好跨过 300 秒边界，
    计数会过期 → 冷却提前解除 → 用例随机变红（2026-11-27 双引擎全量实测
    命中过一次：SQLite 首次全量 2220 项出现 1 项失败，定位为窗口边界）。

    这里不改生产语义，只在用例内把限流组件读到的时钟固定到一个**未来**时刻：
      * 固定时刻 → 全部计数落在同一计数周期内，边界不再影响判定；
      * 未来时刻 → `expires_at` 在未来，`remaining_seconds()` 返回正值，
        冷却提示文案（剩余秒数）仍可正常生成。

    `offset_seconds` 用于模拟「时间前进」；产出的时钟对象可被用例再推进，
    以构造「冷却期内 / 冷却期满」两个时点。
    """
    from apps.core import rate_limit

    base = datetime.now(dt_timezone.utc) + timedelta(hours=1) + timedelta(
        seconds=offset_seconds
    )
    clock = _FakeClock(base)
    with mock.patch.object(rate_limit, "timezone", clock):
        yield clock


class _FakeClock:
    """可控时钟：只提供限流组件用到的 timezone.now()。"""

    def __init__(self, start):
        self.now_value = start

    def now(self):
        return self.now_value


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

    def test_new_password_equal_to_current_password_rejected(self):
        """新密码与当前密码相同 → 拒绝改密（不得回「已修改」却没换口令）。

        复核实证缺陷：改密入口原只校验「两次一致 + Django 校验器」，提交
        current_password == new_password1 时照样改密成功——口令实际未变，
        却写 identity.password_changed 审计，变更记录与真实凭据不一致。
        """
        before = self.user.password
        response = self.client.post("/system/", {
            "current_password": _TEST_PW_CURRENT,
            "new_password1": _TEST_PW_CURRENT,
            "new_password2": _TEST_PW_CURRENT,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "新密码不能与当前密码相同")
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "同值改密不得改写口令哈希")
        self.assertTrue(self.user.check_password(_TEST_PW_CURRENT))
        self.assertFalse(
            AuditEvent.objects.filter(
                action="identity.password_changed", actor=self.user
            ).exists(),
            "口令未实际变更时不得写 identity.password_changed 审计",
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
        """连续失败达阈值后，冷却期内即使口令正确也不得改密。

        时钟固定（见 `_frozen_limit_window`）：本用例必须确定性地覆盖「冷却
        生效」这一契约，不受 300 秒计数周期边界影响随机变红。
        """
        before = self.user.password
        with _frozen_limit_window():
            for _ in range(PASSWORD_REAUTH_MAX_FAILURES):
                response = self._post_change(_TEST_PW_WRONG)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, _ERROR_TEXT)
            response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, _COOLDOWN_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "冷却期内不得改密")

    def test_failure_counter_reaches_threshold_in_one_window(self):
        """失败计数必须在一个窗口内累计到阈值（DB 原子计数确实在累加）。

        这条断言不依赖窗口边界：它只看计数行的**总量**是否达到阈值。
        """
        with _frozen_limit_window():
            for _ in range(PASSWORD_REAUTH_MAX_FAILURES):
                self._post_change(_TEST_PW_WRONG)
            total = _reauth_counter_count()
        self.assertGreaterEqual(
            total,
            PASSWORD_REAUTH_MAX_FAILURES,
            "失败计数未累计到阈值（DB 原子计数丢计数或窗口被拆分）",
        )

    def test_cooldown_is_300s_from_last_failure_not_window_boundary(self):
        """冷却期自**最后一次失败**起算满 300 秒，不受限流计数窗口边界影响。

        合同口径（既有文档基线，非本轮新增策略）：
          * `apps/identity/services.py::check_current_password` 约定「连续失败达
            阈值后进入 PASSWORD_REAUTH_COOLDOWN_SECONDS 秒冷却，冷却期内即使
            密码正确也拒绝校验」；
          * 《前端opt-in与三开发线复核修复报告》记「5 次失败后进入 300 秒
            不可重试窗口」；
          * 《文档处置清单_重复过期冲突》记「5 次失败后进入 300 秒冷却」。

        整改前的实现把计数放在**纪元对齐的固定窗口**里，冷却实际只到「当前
        窗口结束」为止（0~300 秒不等）——冷却时长短于合同约定的 300 秒，且在
        窗口末尾失败时几乎立刻失效，形成绕过口子。2026-11-27 改为滑动窗口后，
        冷却自最后一次失败起算满 300 秒。

        本用例把合同口径确定性钉住：冷却期内（最后一次失败后第 299 秒）口令
        正确仍被拦下；冷却期满（第 301 秒）才放行。
        """
        before = self.user.password
        with _frozen_limit_window() as clock:
            for _ in range(PASSWORD_REAUTH_MAX_FAILURES):
                response = self._post_change(_TEST_PW_WRONG)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, _ERROR_TEXT)

            # 冷却期内（最后一次失败后第 299 秒）：口令正确也必须被拦下
            clock.now_value += timedelta(seconds=PASSWORD_REAUTH_COOLDOWN_SECONDS - 1)
            blocked = self._post_change(_TEST_PW_CURRENT)
            self.assertEqual(
                blocked.status_code,
                200,
                "冷却期内即使口令正确也不得放行（合同：300 秒冷却）",
            )
            self.assertContains(blocked, _COOLDOWN_TEXT)

            # 冷却期满（最后一次失败后第 301 秒）：此时才放行
            clock.now_value += timedelta(seconds=2)
            released = self._post_change(_TEST_PW_CURRENT)

        self.assertEqual(
            released.status_code,
            302,
            "冷却期满（最后一次失败后满 300 秒）必须放行",
        )
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.password, before, "放行后必须完成改密")
        self.assertTrue(self.user.check_password(_TEST_PW_NEW), "新口令必须已写入")

    def test_new_password_equal_to_current_password_rejected(self):
        """强制改密同样拒绝「新密码 == 当前密码」（口径与 /system/ 一致）。"""
        before = self.user.password
        response = self.client.post("/force-password-change/", {
            "current_password": _TEST_PW_CURRENT,
            "new_password1": _TEST_PW_CURRENT,
            "new_password2": _TEST_PW_CURRENT,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "新密码不能与当前密码相同")
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "同值改密不得改写口令哈希")
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.must_change_password, "强制改密标记不得被清除")
        self.assertFalse(
            AuditEvent.objects.filter(
                action="identity.password_changed", actor=self.user
            ).exists(),
            "口令未实际变更时不得写 identity.password_changed 审计",
        )


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


class PasswordChangeUnifiedEntryTests(TestCase):
    """ARCH-05：两条改密入口必须共用同一套校验规则。

    整改前 `/force-password-change/` 与 `/system/` 各自复制了一份校验分支
    （长度 → 两次一致 → Django 校验器），强制改密侧还多一段重复校验，两条
    路径的口径随时可能漂移。本组用例锁定：
      * 两条入口对同一组输入给出**完全相同**的错误文案；
      * 两条入口共用 `validate_password_change` 唯一入口；
      * 同值策略由 PASSWORD_POLICY_FORBID_REUSE 开关控制（口径待负责人裁定，
        开关关闭即回到整改前行为）。
    """

    def setUp(self):
        # 两条入口的前置条件不同：/system/ 面向在职员工，/force-password-change/
        # 只对 must_change_password=True 的账号开放（否则直接跳 /home/）。
        self.user, self.profile = _make_staff("r5_unified")
        self.forced_user, self.forced_profile = _make_staff(
            "r5_unified_forced", must_change_password=True
        )
        self.client.force_login(self.user)

    def _post(self, path, current, new1, new2):
        return self.client.post(path, {
            "current_password": current,
            "new_password1": new1,
            "new_password2": new2,
        })

    def _post_forced(self, current, new1, new2):
        self.client.force_login(self.forced_user)
        try:
            return self._post("/force-password-change/", current, new1, new2)
        finally:
            self.client.force_login(self.user)

    def _error_text(self, response):
        """从渲染结果里取出错误文案（模板以 error 变量展示）。"""
        return response.content.decode("utf-8")

    def test_both_entries_return_identical_error_for_wrong_current_password(self):
        """当前密码错误时两条入口口径一致（不得一条放行、一条拦截）。"""
        forced_response = self._post_forced(
            _TEST_PW_WRONG, _TEST_PW_NEW, _TEST_PW_NEW
        )
        self.assertEqual(forced_response.status_code, 200)
        self.assertContains(forced_response, _ERROR_TEXT)

        self_response = self._post(
            "/system/", _TEST_PW_WRONG, _TEST_PW_NEW, _TEST_PW_NEW
        )
        self.assertEqual(self_response.status_code, 200)
        self.assertContains(self_response, _ERROR_TEXT)

    def test_both_entries_return_identical_error_for_mismatched_confirmation(self):
        """两次输入不一致时两条入口口径一致。"""
        forced_response = self._post_forced(
            _TEST_PW_CURRENT, _TEST_PW_NEW, _TEST_PW_WRONG
        )
        self.assertContains(forced_response, "两次输入的密码不一致")
        self_response = self._post(
            "/system/", _TEST_PW_CURRENT, _TEST_PW_NEW, _TEST_PW_WRONG
        )
        self.assertContains(self_response, "两次输入的密码不一致")

    def test_both_entries_return_identical_error_for_short_password(self):
        """长度不足时两条入口口径一致。"""
        forced_response = self._post_forced(_TEST_PW_CURRENT, "ab1", "ab1")
        self.assertContains(forced_response, "密码至少 6 位")
        self_response = self._post(
            "/system/", _TEST_PW_CURRENT, "ab1", "ab1"
        )
        self.assertContains(self_response, "密码至少 6 位")

    def test_unified_entry_is_the_single_validation_source(self):
        """两条入口必须调用同一个服务函数（防止再次各自复制分支）。"""
        import inspect

        from apps.identity import services

        source = inspect.getsource(services)
        self.assertIn("def validate_password_change(", source)
        # View 层不得再出现「长度 / 两次一致 / validate_password」的手写分支
        view_source = inspect.getsource(__import__("hotelapp.views", fromlist=["views"]))
        self.assertIn("validate_password_change(", view_source)
        self.assertNotIn("两次输入的密码不一致", view_source)
        self.assertNotIn("密码至少 6 位", view_source)

    def test_policy_switch_allows_same_password_when_disabled(self):
        """开关关闭 → 允许同值改密（负责人若否决该策略，一处回退即可）。"""
        with self.settings(PASSWORD_POLICY_FORBID_REUSE=False):
            response = self._post(
                "/system/", _TEST_PW_CURRENT, _TEST_PW_CURRENT, _TEST_PW_CURRENT
            )
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(_TEST_PW_CURRENT))

    def test_policy_switch_blocks_same_password_when_enabled(self):
        """开关开启 → 拒绝同值改密（默认口径）。"""
        with self.settings(PASSWORD_POLICY_FORBID_REUSE=True):
            response = self._post(
                "/system/", _TEST_PW_CURRENT, _TEST_PW_CURRENT, _TEST_PW_CURRENT
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "新密码不能与当前密码相同")


class PasswordReauthCooldownWindowBoundaryTests(TestCase):
    """冷却期不得因限流计数窗口边界而失效。

    背景（2026-11-27 同基线全量取证）：`ForcedPasswordChangeReauthTests.
    test_cooldown_blocks_even_correct_password_after_threshold` 在全量里
    偶发失败（302 != 200），单独复跑又必绿。根因不是用例写法，而是限流计数
    落在**以纪元对齐的固定窗口**里：连续失败达阈值后，只要跨过窗口边界，
    新窗口内计数归零，此时即使用户提供的是**正确**口令也会被放行改密——
    冷却期被窗口边界截断，恰好落在窗口末尾时几乎立刻失效。

    冷却期的语义是「阈值达成后 300 秒内即使口令正确也拒绝校验」，窗口边界
    不得成为绕过口子。本组用例把时钟固定在窗口末尾，跨边界后复现该绕过，
    修复后必须仍被冷却拦下。
    """

    def setUp(self):
        self.user, self.profile = _make_staff("r5_boundary", must_change_password=True)
        self.client.force_login(self.user)

    def _post_change(self, current_password, new_password=_TEST_PW_NEW):
        return self.client.post("/force-password-change/", {
            "current_password": current_password,
            "new_password1": new_password,
            "new_password2": new_password,
        })

    def test_cooldown_holds_across_limit_window_boundary(self):
        before = self.user.password
        window = PASSWORD_REAUTH_COOLDOWN_SECONDS
        # 把时钟固定到「窗口最后一秒」：下一个窗口边界就在 1 秒之后
        epoch = int(timezone.now().timestamp())
        base = datetime.fromtimestamp(
            epoch - (epoch % window) + (window - 1), tz=dt_timezone.utc
        )
        clock = _FakeClock(base)
        with mock.patch("apps.core.rate_limit.timezone", clock):
            for _ in range(PASSWORD_REAUTH_MAX_FAILURES):
                response = self._post_change(_TEST_PW_WRONG)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, _ERROR_TEXT)
            # 跨过限流计数窗口边界（进入下一个窗口）
            clock.now_value = base + timedelta(seconds=2)
            response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(
            response.status_code, 200,
            "冷却期内即使口令正确也不得放行改密（限流窗口边界不得成为绕过口子）",
        )
        self.assertContains(response, _COOLDOWN_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "冷却期内不得改密")

    def test_cooldown_still_blocks_when_window_boundary_passed_within_same_second(self):
        """边界落在两次失败之间同样不得放行（计数不得随窗口切换而清零）。"""
        before = self.user.password
        window = PASSWORD_REAUTH_COOLDOWN_SECONDS
        epoch = int(timezone.now().timestamp())
        base = datetime.fromtimestamp(
            epoch - (epoch % window) + (window - 1), tz=dt_timezone.utc
        )
        clock = _FakeClock(base)
        with mock.patch("apps.core.rate_limit.timezone", clock):
            for index in range(PASSWORD_REAUTH_MAX_FAILURES):
                if index == PASSWORD_REAUTH_MAX_FAILURES - 1:
                    # 最后一次失败发生在窗口切换之后
                    clock.now_value = base + timedelta(seconds=2)
                response = self._post_change(_TEST_PW_WRONG)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, _ERROR_TEXT)
            response = self._post_change(_TEST_PW_CURRENT)
        self.assertEqual(
            response.status_code, 200,
            "冷却期内即使口令正确也不得放行改密（计数不得随窗口切换而清零）",
        )
        self.assertContains(response, _COOLDOWN_TEXT)
        self.user.refresh_from_db()
        self.assertEqual(self.user.password, before, "冷却期内不得改密")
