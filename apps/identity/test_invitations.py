"""员工邀请激活体系测试（AUTH-14~18 + AUTH-20，方案 §5.2/§5.3/§15.1 邀请清单）。

覆盖：
  * AUTH-14：token 随机性 / 只存 SHA-256 / 约束与索引 / 绑定档案+任职不可改绑；
  * AUTH-15：创建（撤销旧邀请再新建）/ 撤销 / 重发 / 一次性消费 / 并发防线 /
    过期扫描（服务 + 管理命令）/ 统一失败文案不泄露存在性 / 审计事件完整；
  * AUTH-16：待激活账号 unusable password 不可登录；激活走 validate_password；
    激活成功 must_change_password=False 且可登录；
  * AUTH-17：员工中心邀请创建/撤销/重发 API 四态（未登录/员工/无权限/店长）
    + CSRF + 文案口径（"复制并人工发送"，无"发送成功"）+ Host 不受污染；
  * AUTH-18：激活页 GET/POST + 限流（第 6 次 429）+ 登录页成功提示；
  * 审计：invitation_created/resent/revoked/accepted + account_provision，
    明文 token / 摘要不落任何审计字段。
"""
import hashlib
from datetime import timedelta
from io import StringIO
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402

from apps.core.models import AuditEvent, Property

from .invitation_services import (
    UNIFIED_INVALID_MESSAGE,
    EmployeeInvitationService,
    InvitationError,
    token_digest,
)
from .models import Invitation, PropertyStaffMembership, StaffProfile
from .test_staff_center import _make_target_employee, _make_user

_TEST_PW_412432D8 = get_random_string(24)



STRONG_PASSWORD = "Str0ng-Pass-2026!"


def _past_expiry(invitation, hours=1):
    """模拟邀请过期：同时移动 created_at/expires_at（满足有效期约束）。"""
    now = timezone.now()
    Invitation.objects.filter(pk=invitation.pk).update(
        created_at=now - timedelta(hours=hours + 1),
        expires_at=now - timedelta(hours=hours))


class InvitationServiceTestBase(TestCase):
    """公共夹具：店长 + 未开通账号的目标员工。"""

    @classmethod
    def setUpTestData(cls):
        cls.owner = _make_user("iv_owner")
        cls.profile, cls.membership, _ = _make_target_employee(
            "IV-9001", with_account=False)

    def _invite(self, username="iv_target_user", profile=None, membership=None):
        return EmployeeInvitationService.create_invitation(
            profile or self.profile, membership or self.membership,
            self.owner, username=username)


class InvitationTokenSecurityTests(InvitationServiceTestBase):
    """AUTH-14/15：token 随机性、只存摘要、绑定性、重复创建撤销旧邀请。"""

    def test_token_random_entropy_and_hash_only_storage(self):
        """token ≥256bit 随机、两次生成不同；库内只有 SHA-256 摘要。"""
        inv1, raw1 = self._invite()
        inv2, raw2 = self._invite(username="iv_target_user2")
        self.assertNotEqual(raw1, raw2)
        self.assertGreaterEqual(len(raw1), 40, "token_urlsafe(32) 应≥256bit 熵")
        self.assertEqual(inv1.token_hash, hashlib.sha256(raw1.encode()).hexdigest())
        self.assertEqual(inv1.token_hash, token_digest(raw1))
        self.assertEqual(len(inv1.token_hash), 64)
        # 明文 token 不落库：模型实例字符串/全部字段扫描
        self.assertNotIn(raw1, str(inv1))
        for field_value in (inv1.token_hash, str(inv1.pk)):
            self.assertNotIn(raw1, str(field_value))
        self.assertEqual(inv2.resend_count, 0)
        self.assertIsNotNone(inv2.last_sent_at)
        # 两条记录 token_hash 互不相同（第二次创建会撤销第一条，见生命周期测试）
        self.assertNotEqual(inv1.token_hash, inv2.token_hash)
        self.assertTrue(Invitation.objects.filter(
            token_hash=inv1.token_hash).exists())
        self.assertTrue(Invitation.objects.filter(
            token_hash=inv2.token_hash,
            status=Invitation.Status.PENDING).exists())

    def test_pending_account_unusable_password_cannot_login(self):
        """§5.1/§5.4：待激活账号已预开通但 unusable password，激活前不可登录。"""
        self._invite(username="iv_pending_user")
        profile = StaffProfile.objects.get(pk=self.profile.pk)
        user = profile.user
        self.assertIsNotNone(user)
        self.assertEqual(user.username, "iv_pending_user")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(profile.must_change_password)
        self.assertIsNone(profile.last_password_change)
        # 任意密码都不能登录（unusable password 永不匹配）
        self.assertFalse(self.client.login(username="iv_pending_user",
                                           password=STRONG_PASSWORD))

    def test_create_rejects_employee_with_active_account(self):
        """已开通可用密码账号的员工 → 拒绝邀请（走密码重置而非邀请）。"""
        profile, _, _ = _make_target_employee("IV-9002")  # with_account=True
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.create_invitation(
                profile,
                PropertyStaffMembership.objects.get(staff_profile=profile),
                self.owner, username="iv_no_need")
        self.assertIn("已开通登录账号", str(ctx.exception))
        self.assertFalse(Invitation.objects.filter(staff_profile=profile).exists())

    def test_create_rejects_mismatched_membership_binding(self):
        """token 绑定性：任职关系不属于该档案 → 拒绝（不可改绑）。"""
        other_profile, _, _ = _make_target_employee("IV-9003", with_account=False)
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.create_invitation(
                other_profile, self.membership,  # 他人任职
                self.owner, username="iv_mismatch")
        self.assertIn("不匹配", str(ctx.exception))
        self.assertFalse(Invitation.objects.exists())

    def test_create_rejects_taken_username(self):
        """登录账号已被占用 → 拒绝且不留任何邀请/账号副作用。"""
        User.objects.create_user(username="iv_taken", password=_TEST_PW_412432D8)
        users_before = User.objects.count()
        with self.assertRaises(InvitationError) as ctx:
            self._invite(username="iv_taken")
        self.assertIn("已被占用", str(ctx.exception))
        self.assertEqual(User.objects.count(), users_before)
        self.assertFalse(Invitation.objects.exists())
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.user, "失败创建不得绑定账号")

    def test_recreate_revokes_previous_pending(self):
        """同一员工重复创建 → 旧 PENDING 撤销（revoked_by 落定）再新建。"""
        inv1, raw1 = self._invite()
        inv2, raw2 = self._invite()
        self.assertNotEqual(raw1, raw2)
        inv1.refresh_from_db()
        self.assertEqual(inv1.status, Invitation.Status.REVOKED)
        self.assertIsNotNone(inv1.revoked_at)
        self.assertEqual(inv1.revoked_by, self.owner)
        self.assertEqual(inv2.status, Invitation.Status.PENDING)
        # 旧 token 失效、新 token 可用（生命周期测试深入验证消费）
        with self.assertRaises(InvitationError):
            EmployeeInvitationService.consume(raw1, STRONG_PASSWORD)

    def test_provision_corrects_pending_username_on_reinvite(self):
        """待激活账号（unusable password）重发时允许更正账号名。"""
        self._invite(username="iv_typo_name")
        self._invite(username="iv_fixed_name")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.user.username, "iv_fixed_name")
        self.assertFalse(self.profile.user.has_usable_password())
        self.assertFalse(User.objects.filter(username="iv_typo_name").exists())


class InvitationOneTimeConsumeTests(InvitationServiceTestBase):
    """AUTH-15/16：一次性消费、并发防线、绑定不可跨员工、激活效果。"""

    def test_consume_success_activates_account_and_audits(self):
        """消费成功：ACCEPTED + consumed_at + 自设密码 + must_change_password=False。"""
        inv, raw = self._invite()
        before = timezone.now()
        result = EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.assertEqual(result.pk, inv.pk)
        inv.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(inv.status, Invitation.Status.ACCEPTED)
        self.assertIsNotNone(inv.consumed_at)
        self.assertTrue(self.profile.user.check_password(STRONG_PASSWORD))
        self.assertFalse(self.profile.must_change_password,
                         "§5.4：员工自设密码后不强制首登改密")
        self.assertIsNotNone(self.profile.last_password_change)
        self.assertGreaterEqual(self.profile.last_password_change, before)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.invitation_accepted", entity_type="StaffProfile",
            entity_id=str(self.profile.pk)).exists())
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.account_provision", entity_type="StaffProfile",
            entity_id=str(self.profile.pk)).exists())

    def test_activated_employee_can_login(self):
        """激活成功后可正常登录工作台账号。"""
        _, raw = self._invite(username="iv_login_user")
        EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.client.logout()
        self.assertTrue(self.client.login(username="iv_login_user",
                                          password=STRONG_PASSWORD))

    def test_repeated_consume_rejected_unified(self):
        """一次性消费：同一 token 第二次提交 → 统一文案，状态不变。"""
        _, raw = self._invite()
        EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.assertEqual(str(ctx.exception), UNIFIED_INVALID_MESSAGE)
        self.assertEqual(Invitation.objects.filter(
            token_hash=token_digest(raw),
            status=Invitation.Status.ACCEPTED).count(), 1)

    def test_wrong_token_unified_no_accepted_audit(self):
        """错误 token → 统一文案；不产生接受审计；不影响既有邀请。"""
        inv, _ = self._invite()
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.consume("bogus-token-value", STRONG_PASSWORD)
        self.assertEqual(str(ctx.exception), UNIFIED_INVALID_MESSAGE)
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invitation.Status.PENDING)
        self.assertFalse(AuditEvent.objects.filter(
            action="identity.invitation_accepted").exists())

    def test_empty_password_rejected_but_token_kept_pending(self):
        """有效 token 未提交密码 → 明确提示；邀请保持 PENDING 可重试。"""
        _, raw = self._invite()
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.consume(raw, "")
        self.assertIn("请设置登录密码", str(ctx.exception))
        self.assertEqual(Invitation.objects.filter(
            token_hash=token_digest(raw),
            status=Invitation.Status.PENDING).count(), 1)

    def test_binding_token_cannot_activate_other_employee(self):
        """token 不能跨员工/任职使用：消费 A 的 token 只激活 A，B 不受影响。"""
        other_profile, _, _ = _make_target_employee("IV-9010", with_account=False)
        other_membership = PropertyStaffMembership.objects.get(
            staff_profile=other_profile)
        inv_b, raw_b = EmployeeInvitationService.create_invitation(
            other_profile, other_membership, self.owner,
            username="iv_other_user")
        _, raw_a = self._invite(username="iv_bind_user")
        EmployeeInvitationService.consume(raw_a, STRONG_PASSWORD)
        other_profile.refresh_from_db()
        # A 已激活；B 的账号仍是 unusable、邀请仍 PENDING，A 的密码对 B 无效
        self.assertFalse(other_profile.user.has_usable_password())
        self.assertFalse(other_profile.user.check_password(STRONG_PASSWORD))
        self.assertEqual(inv_b.status, Invitation.Status.PENDING)
        self.assertFalse(AuditEvent.objects.filter(
            action="identity.invitation_accepted",
            entity_id=str(other_profile.pk)).exists())

    def test_conditional_update_guard_allows_single_success(self):
        """并发防线（select_for_update + 状态条件更新断言）：PENDING→ACCEPTED 只放行一次。"""
        _, raw = self._invite()
        now = timezone.now()
        with transaction.atomic():
            self.assertEqual(Invitation.objects.filter(
                pk=Invitation.objects.get(token_hash=token_digest(raw)).pk,
                status=Invitation.Status.PENDING).update(
                status=Invitation.Status.ACCEPTED, consumed_at=now), 1)
            # 并发后到者：同一行条件更新命中 0 行 → 不可能二次成功
            self.assertEqual(Invitation.objects.filter(
                token_hash=token_digest(raw),
                status=Invitation.Status.PENDING).update(
                status=Invitation.Status.ACCEPTED, consumed_at=now), 0)

    def test_loser_of_consume_race_gets_unified_error(self):
        """并发消费：抢先者落定 ACCEPTED 后，后到者 consume 得到统一失败。"""
        _, raw = self._invite()
        with transaction.atomic():
            # 模拟并发对手在同一行锁串行化下先行提交（同事务内先行落定）
            Invitation.objects.filter(
                token_hash=token_digest(raw),
                status=Invitation.Status.PENDING).update(
                status=Invitation.Status.ACCEPTED,
                consumed_at=timezone.now())
            with self.assertRaises(InvitationError) as ctx:
                EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.assertEqual(str(ctx.exception), UNIFIED_INVALID_MESSAGE)


class InvitationLifecycleTests(InvitationServiceTestBase):
    """AUTH-15/AUTH-20：过期扫描、撤销、重发（含管理命令）。"""

    def test_expire_stale_marks_expired_and_idempotent(self):
        """过期扫描：仅 PENDING 且超时 → EXPIRED；重复执行幂等。"""
        inv1, _ = self._invite(username="iv_exp_user1")
        # 第二位员工：验证扫描只影响超时行（同一员工重复创建会撤销旧邀请，
        # 见 test_recreate_revokes_previous_pending）
        other_profile, _, _ = _make_target_employee("IV-9020", with_account=False)
        other_membership = PropertyStaffMembership.objects.get(
            staff_profile=other_profile)
        inv2, _ = EmployeeInvitationService.create_invitation(
            other_profile, other_membership, self.owner,
            username="iv_exp_user2")
        _past_expiry(inv1)
        self.assertEqual(EmployeeInvitationService.expire_stale(), 1)
        inv1.refresh_from_db()
        inv2.refresh_from_db()
        self.assertEqual(inv1.status, Invitation.Status.EXPIRED)
        self.assertEqual(inv2.status, Invitation.Status.PENDING)
        self.assertEqual(EmployeeInvitationService.expire_stale(), 0, "幂等")

    def test_expire_command_output(self):
        """AUTH-20 管理命令：expire_invitations 归档超时邀请并输出计数。"""
        inv, _ = self._invite(username="iv_cmd_user")
        _past_expiry(inv)
        out = StringIO()
        call_command("expire_invitations", stdout=out)
        self.assertIn("归档 1 条", out.getvalue())
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invitation.Status.EXPIRED)

    def test_consume_expired_marks_and_unified(self):
        """消费已过期 token → 统一文案；观察即归档为 EXPIRED。"""
        _, raw = self._invite()
        inv = Invitation.objects.get(token_hash=token_digest(raw))
        _past_expiry(inv)
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.assertEqual(str(ctx.exception), UNIFIED_INVALID_MESSAGE)
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invitation.Status.EXPIRED)

    def test_revoke_sets_fields_and_kills_token(self):
        """撤销：REVOKED + revoked_at/revoked_by + identity.invitation_revoked 审计。"""
        inv, raw = self._invite()
        EmployeeInvitationService.revoke(inv, revoked_by=self.owner)
        inv.refresh_from_db()
        self.assertEqual(inv.status, Invitation.Status.REVOKED)
        self.assertIsNotNone(inv.revoked_at)
        self.assertEqual(inv.revoked_by, self.owner)
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.assertEqual(str(ctx.exception), UNIFIED_INVALID_MESSAGE)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.invitation_revoked", entity_type="Invitation",
            entity_id=str(inv.pk), actor=self.owner).exists())

    def test_revoke_rejected_for_non_pending(self):
        """非 PENDING（已撤销/已接受）邀请不可再撤销。"""
        inv, raw = self._invite()
        EmployeeInvitationService.revoke(inv, revoked_by=self.owner)
        with self.assertRaises(InvitationError):
            EmployeeInvitationService.revoke(inv, revoked_by=self.owner)
        # 已接受的邀请同样拒绝
        inv2, raw2 = self._invite(username="iv_acc_user")
        EmployeeInvitationService.consume(raw2, STRONG_PASSWORD)
        with self.assertRaises(InvitationError):
            EmployeeInvitationService.revoke(inv2, revoked_by=self.owner)

    def test_resend_rotates_token_and_counts(self):
        """重发 = 撤销旧 token + 新 token；resend_count 链上递增 + 审计。"""
        inv1, raw1 = self._invite(username="iv_resend_user")
        inv2, raw2 = EmployeeInvitationService.resend(
            inv1, resent_by=self.owner)
        inv3, raw3 = EmployeeInvitationService.resend(
            inv2, resent_by=self.owner)
        self.assertNotIn(raw2, {raw1, raw3})
        inv1.refresh_from_db()
        inv2.refresh_from_db()
        self.assertEqual(inv1.status, Invitation.Status.REVOKED)
        self.assertEqual(inv2.status, Invitation.Status.REVOKED)
        self.assertEqual(inv3.status, Invitation.Status.PENDING)
        self.assertEqual(inv3.resend_count, 2)
        self.assertGreaterEqual(inv3.last_sent_at, inv1.last_sent_at)
        # 旧 token 全部失效，新 token 可消费
        for stale in (raw1, raw2):
            with self.assertRaises(InvitationError):
                EmployeeInvitationService.consume(stale, STRONG_PASSWORD)
        EmployeeInvitationService.consume(raw3, STRONG_PASSWORD)
        self.assertEqual(AuditEvent.objects.filter(
            action="identity.invitation_resent").count(), 2)

    def test_resend_from_expired_keeps_expired_archive(self):
        """对已过期邀请重发：旧邀请保持 EXPIRED 归档，新邀请 PENDING。"""
        inv1, raw1 = self._invite(username="iv_exp_resend")
        _past_expiry(inv1)
        inv2, raw2 = EmployeeInvitationService.resend(inv1, resent_by=self.owner)
        inv1.refresh_from_db()
        self.assertEqual(inv1.status, Invitation.Status.EXPIRED)
        self.assertIsNone(inv1.revoked_at)
        self.assertEqual(inv2.status, Invitation.Status.PENDING)
        self.assertEqual(inv2.resend_count, 1)
        with self.assertRaises(InvitationError):
            EmployeeInvitationService.consume(raw1, STRONG_PASSWORD)
        EmployeeInvitationService.consume(raw2, STRONG_PASSWORD)

    def test_resend_rejected_after_accepted(self):
        """已接受（激活完成）的邀请不可重发。"""
        inv, raw = self._invite(username="iv_resend_acc")
        EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.resend(inv, resent_by=self.owner)
        self.assertIn("已被接受", str(ctx.exception))


class InvitationPasswordPolicyTests(InvitationServiceTestBase):
    """AUTH-16/§5.4：密码策略与激活前置状态复核。"""

    def test_weak_password_rejected_then_retry_allowed(self):
        """弱密码激活拒绝（validate_password）；邀请保持 PENDING 可重试。"""
        _, raw = self._invite(username="iv_weak_user")
        for weak in ("12345678", "password", "short1"):
            with self.assertRaises(InvitationError) as ctx:
                EmployeeInvitationService.consume(raw, weak)
            self.assertTrue(str(ctx.exception), "必须透出策略中文消息")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.user.username, "iv_weak_user")
        self.assertFalse(self.profile.user.has_usable_password())
        self.assertEqual(Invitation.objects.get(
            token_hash=token_digest(raw)).status, Invitation.Status.PENDING)
        # 改用强密码后同一 token 可成功激活
        EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.user.check_password(STRONG_PASSWORD))
        self.assertFalse(self.profile.must_change_password)

    def test_activation_blocked_when_employment_terminated(self):
        """邀请期间员工停职/离职 → 激活统一失败（不泄露具体状态）。"""
        _, raw = self._invite(username="iv_left_user")
        self.profile.employment_status = StaffProfile.EmploymentStatus.TERMINATED
        self.profile.save(update_fields=("employment_status",))
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.assertEqual(str(ctx.exception), UNIFIED_INVALID_MESSAGE)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.user.has_usable_password())

    def test_activation_blocked_when_membership_suspended(self):
        """邀请期间任职暂停 → 激活统一失败。"""
        _, raw = self._invite(username="iv_susp_user")
        self.membership.status = PropertyStaffMembership.Status.SUSPENDED
        self.membership.save(update_fields=("status",))
        with self.assertRaises(InvitationError) as ctx:
            EmployeeInvitationService.consume(raw, STRONG_PASSWORD)
        self.assertEqual(str(ctx.exception), UNIFIED_INVALID_MESSAGE)


class InvitationAuditMinimizationTests(InvitationServiceTestBase):
    """§16.2：审计事件完整且不含 token 明文/摘要。"""

    def test_full_audit_chain_without_token_material(self):
        """created→resent→revoked→accepted 全链审计；明文 token 与摘要不落审计。"""
        inv1, raw1 = self._invite(username="iv_audit_user")
        inv2, raw2 = EmployeeInvitationService.resend(inv1, resent_by=self.owner)
        EmployeeInvitationService.revoke(inv2, revoked_by=self.owner)
        inv3, raw3 = EmployeeInvitationService.resend(inv2, resent_by=self.owner)
        EmployeeInvitationService.consume(raw3, STRONG_PASSWORD)
        actions = set(AuditEvent.objects.filter(
            action__startswith="identity.invitation")
            .values_list("action", flat=True))
        self.assertEqual(actions, {
            "identity.invitation_created", "identity.invitation_resent",
            "identity.invitation_revoked", "identity.invitation_accepted"})
        blob = "".join(
            f"{e.after_json or ''}{e.before_json or ''}{e.reason or ''}"
            for e in AuditEvent.objects.all())
        for secret in (raw1, raw2, raw3, inv1.token_hash):
            self.assertNotIn(secret, blob, "token 明文/摘要不得写入审计")


class InviteAcceptPageTests(InvitationServiceTestBase):
    """AUTH-18：/invite/accept/ 匿名激活页（GET 校验 / POST 消费 / 限流）。"""

    def _page_get(self, token):
        return self.client.get(f"/invite/accept/?token={token}")

    def _page_post(self, token, password=STRONG_PASSWORD, confirm=None):
        return self.client.post("/invite/accept/", {
            "token": token, "password": password,
            "password_confirm": confirm if confirm is not None else password,
        })

    def test_get_valid_token_renders_password_form(self):
        """有效 token：渲染设置密码表单并展示姓名/账号/有效期。"""
        _, raw = self._invite(username="iv_page_user")
        response = self._page_get(raw)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "identity/invite_accept.html")
        self.assertContains(response, "设置登录密码")
        self.assertContains(response, "iv_page_user")
        self.assertContains(response, "员工")
        self.assertContains(response, "完成激活")

    def test_get_invalid_tokens_unified_message_without_form(self):
        """无效/过期/撤销/已消费 token GET → 统一文案、无表单、不泄露存在性。"""
        _, raw = self._invite(username="iv_invalid_user")
        EmployeeInvitationService.revoke(
            Invitation.objects.get(token_hash=token_digest(raw)),
            revoked_by=self.owner)
        expired_inv, expired_raw = self._invite(username="iv_invalid_user2")
        _past_expiry(expired_inv)
        consumed_inv, consumed_raw = self._invite(username="iv_invalid_user3")
        EmployeeInvitationService.consume(consumed_raw, STRONG_PASSWORD)
        responses = {
            "garbage": self._page_get("totally-bogus-token"),
            "revoked": self._page_get(raw),
            "expired": self._page_get(expired_raw),
            "consumed": self._page_get(consumed_raw),
        }
        for name, response in responses.items():
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, UNIFIED_INVALID_MESSAGE)
            self.assertNotContains(response, "完成激活")
            self.assertNotContains(response, "iv_invalid_user", msg_prefix=name)

    def test_post_success_redirects_login_with_banner(self):
        """POST 激活成功 → 302 登录页；登录页展示成功提示；密码可用。"""
        _, raw = self._invite(username="iv_post_user")
        response = self._page_post(raw)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response["Location"]).path, "/login/")
        self.assertEqual(parse_qs(urlparse(response["Location"]).query)
                         .get("activated"), ["1"])
        banner = self.client.get("/login/?activated=1")
        self.assertContains(banner, "账号已激活")
        self.assertTrue(self.client.login(username="iv_post_user",
                                          password=STRONG_PASSWORD))

    def test_post_weak_password_keeps_form_and_pending(self):
        """POST 弱密码 → 400 + 策略消息；邀请未消费可重试。"""
        _, raw = self._invite(username="iv_weak_page")
        response = self._page_post(raw, password="12345678")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "密码", status_code=400)
        self.assertEqual(Invitation.objects.get(
            token_hash=token_digest(raw)).status, Invitation.Status.PENDING)
        # 随后强密码成功
        self.assertEqual(self._page_post(raw).status_code, 302)

    def test_post_confirm_mismatch_not_consumed(self):
        """两次密码不一致 → 表单回显错误；不消费邀请。"""
        _, raw = self._invite(username="iv_mismatch_page")
        response = self._page_post(raw, confirm="Different-Pass-1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "两次输入的密码不一致")
        self.assertEqual(Invitation.objects.get(
            token_hash=token_digest(raw)).status, Invitation.Status.PENDING)

    def test_post_invalid_token_unified(self):
        """POST 无效 token → 统一文案，不产生账号变化。"""
        users_before = User.objects.count()
        response = self._page_post("bogus-token")
        self.assertContains(response, UNIFIED_INVALID_MESSAGE)
        self.assertNotContains(response, "完成激活")
        self.assertEqual(User.objects.count(), users_before)

    def test_rate_limit_sixth_post_returns_429(self):
        """限流（§6.3 public.invitation_activate：IP 5 次/15 分钟）：第 6 次 429。"""
        for _ in range(5):
            response = self._page_post("bogus-token")
            self.assertNotEqual(response.status_code, 429)
        sixth = self._page_post("bogus-token")
        self.assertEqual(sixth.status_code, 429)
        self.assertIn("Retry-After", sixth)
        self.assertTrue(sixth["Retry-After"].isdigit())
        self.assertContains(sixth, "尝试次数过多", status_code=429)

    def test_rate_limit_fail_closed_on_get(self):
        """GET 验证同样限流（fail-closed）：第 6 次 429 + Retry-After。"""
        for _ in range(5):
            self.assertEqual(self._page_get("bogus-token").status_code, 200)
        sixth = self._page_get("bogus-token")
        self.assertEqual(sixth.status_code, 429)
        self.assertIn("Retry-After", sixth)
        # 429 与公共面限流同口径：public.rate_limited 审计事件
        self.assertTrue(AuditEvent.objects.filter(
            action="public.rate_limited", entity_type="PublicEndpoint",
            entity_id="public.invitation_activate").exists())

    def test_untrusted_host_rejected_on_activation_page(self):
        """§5.3/§15.1：激活页只允许受信任 Host（ALLOWED_HOSTS 之外 → 400）。"""
        response = self.client.get("/invite/accept/?token=whatever",
                                   HTTP_HOST="evil.example")
        self.assertEqual(response.status_code, 400)


class StaffCenterInvitationAPITests(InvitationServiceTestBase):
    """AUTH-17：员工中心邀请创建/撤销/重发 API——四态门禁、CSRF、审计、限流。"""

    def _invite_url(self, profile=None):
        return f"/manage/staff/actions/{(profile or self.profile).pk}/invite/"

    def test_invite_anonymous_redirects_to_login(self):
        """未登录 POST 创建/撤销/重发 → 302 登录页。"""
        for url in (self._invite_url(),
                    self._invite_url() + "revoke/", self._invite_url() + "resend/"):
            response = self.client.post(url, {"username": "anon_user"})
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/login/", response["Location"], url)
        self.assertFalse(Invitation.objects.exists())

    def test_invite_without_permission_403(self):
        """无 identity.staff_manage（前台）→ 403，且无任何写副作用。"""
        fd = _make_user("iv_front", "前台")
        self.client.force_login(fd)
        for url in (self._invite_url(),
                    self._invite_url() + "revoke/", self._invite_url() + "resend/"):
            response = self.client.post(url, {"username": "front_user"})
            self.assertEqual(response.status_code, 403, url)
        self.assertFalse(Invitation.objects.exists())
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.user)

    def test_invite_cross_property_profile_404(self):
        """越店员工档案 → 404（数据范围隔离）。"""
        self.client.force_login(self.owner)
        other_prop = Property.objects.create(
            code="OTHER-IV", name="其他物业-IV", status=Property.Status.ACTIVE)
        other_profile = StaffProfile.objects.create(
            user=None, full_name="别店员工IV", employment_status="ACTIVE")
        PropertyStaffMembership.objects.create(
            property=other_prop, staff_profile=other_profile,
            employee_no="OTHER-IV-1", status="ACTIVE")
        response = self.client.post(self._invite_url(other_profile),
                                    {"username": "cross_user"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Invitation.objects.exists())

    def test_invite_manager_success_copy_semantics_and_audit(self):
        """店长创建邀请：一次性链接 + 文案口径 + 待激活账号 + 审计。"""
        self.client.force_login(self.owner)
        response = self.client.post(self._invite_url(), {"username": "iv_api_user"})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        # §5.3：只允许"复制并人工发送"语义，禁止"发送成功"
        self.assertIn("邀请链接已生成，请复制并人工发送", data["message"])
        self.assertNotIn("发送成功", data["message"])
        self.assertNotIn("发送成功", response.content.decode())
        parsed = urlparse(data["invite_url"])
        self.assertEqual(parsed.path, "/invite/accept/")
        raw_token = parse_qs(parsed.query)["token"][0]
        invitation = Invitation.objects.get(pk=data["invitation_id"])
        self.assertEqual(invitation.token_hash, token_digest(raw_token))
        self.assertEqual(invitation.created_by, self.owner)
        self.assertEqual(invitation.status, Invitation.Status.PENDING)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.invitation_created", entity_type="Invitation",
            entity_id=str(invitation.pk), actor=self.owner).exists())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.user.username, "iv_api_user")
        self.assertFalse(self.profile.user.has_usable_password())

    def test_invite_url_ignores_forwarded_host(self):
        """§15.1：不可信 Host/X-Forwarded-Host 不能污染邀请链接。"""
        self.client.force_login(self.owner)
        response = self.client.post(
            self._invite_url(), {"username": "iv_host_user"},
            HTTP_X_FORWARDED_HOST="evil.example")
        self.assertEqual(response.status_code, 200)
        invite_url = response.json()["data"]["invite_url"]
        parsed = urlparse(invite_url)
        self.assertNotIn("evil.example", invite_url)
        self.assertEqual(parsed.netloc, "testserver")
        self.assertTrue(parsed.path.startswith("/invite/accept"))

    @override_settings(IDENTITY_INVITATION_CANONICAL_HOST="pms.example.com")
    def test_invite_url_prefers_canonical_host(self):
        """配置规范 Host 时优先使用（§5.3 canonical host）。"""
        self.client.force_login(self.owner)
        response = self.client.post(self._invite_url(),
                                    {"username": "iv_canon_user"})
        invite_url = response.json()["data"]["invite_url"]
        self.assertTrue(invite_url.startswith("http://pms.example.com/invite/accept"))

    def test_revoke_endpoint_success(self):
        """撤销端点：待激活邀请 → REVOKED + 审计；原 token 失效。"""
        self.client.force_login(self.owner)
        _, raw = self._invite(username="iv_revoke_api")
        response = self.client.post(self._invite_url() + "revoke/", {})
        self.assertEqual(response.status_code, 200)
        invitation = Invitation.objects.get(token_hash=token_digest(raw))
        self.assertEqual(invitation.status, Invitation.Status.REVOKED)
        self.assertEqual(invitation.revoked_by, self.owner)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.invitation_revoked",
            entity_id=str(invitation.pk)).exists())
        with self.assertRaises(InvitationError):
            EmployeeInvitationService.consume(raw, STRONG_PASSWORD)

    def test_revoke_endpoint_without_pending_404(self):
        """无待激活邀请时撤销 → 404。"""
        self.client.force_login(self.owner)
        response = self.client.post(self._invite_url() + "revoke/", {})
        self.assertEqual(response.status_code, 404)

    def test_resend_endpoint_success(self):
        """重发端点：旧 token 失效 + 新一次性链接 + resend_count 递增。"""
        self.client.force_login(self.owner)
        _, old_raw = self._invite(username="iv_resend_api")
        response = self.client.post(self._invite_url() + "resend/", {})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertIn("邀请链接已重新生成，请复制并人工发送", data["message"])
        self.assertNotIn("发送成功", data["message"])
        new_raw = parse_qs(urlparse(data["invite_url"]).query)["token"][0]
        self.assertNotEqual(old_raw, new_raw)
        self.assertEqual(data["resend_count"], 1)
        old_inv = Invitation.objects.get(token_hash=token_digest(old_raw))
        new_inv = Invitation.objects.get(token_hash=token_digest(new_raw))
        self.assertEqual(old_inv.status, Invitation.Status.REVOKED)
        self.assertEqual(new_inv.status, Invitation.Status.PENDING)
        self.assertTrue(AuditEvent.objects.filter(
            action="identity.invitation_resent",
            entity_id=str(new_inv.pk)).exists())

    def test_resend_endpoint_without_invitation_404(self):
        """无任何邀请记录时重发 → 404（提示先创建邀请）。"""
        self.client.force_login(self.owner)
        response = self.client.post(self._invite_url() + "resend/", {})
        self.assertEqual(response.status_code, 404)

    def test_csrf_missing_returns_403(self):
        """CSRF 缺失 → 403（页面域端点强制 CSRF）。"""
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        response = strict.post(self._invite_url(), {"username": "iv_csrf_user"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Invitation.objects.exists())

    @override_settings(PUBLIC_RATE_LIMITS={
        "identity.invitation_manage": [(("ip",), 2, 600)]})
    def test_invite_manage_rate_limited(self):
        """§5.3：邀请创建接口限流——超限 429 RATE_LIMITED。"""
        self.client.force_login(self.owner)
        for i in range(2):
            response = self.client.post(
                self._invite_url(), {"username": f"iv_rate_user{i}"})
            self.assertEqual(response.status_code, 200)
        third = self.client.post(self._invite_url(), {"username": "iv_rate_user9"})
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.json()["error"]["code"], "RATE_LIMITED")

    def test_staff_detail_exposes_invitation_status(self):
        """读 API：员工详情带邀请状态展示与 password_pending 标记。"""
        self.client.force_login(self.owner)
        self._invite(username="iv_dto_user")
        detail = self.client.get(f"/api/v1/staff/{self.profile.pk}")
        self.assertEqual(detail.status_code, 200)
        data = detail.json()["data"]
        self.assertEqual(data["invitation"]["status"], "PENDING")
        self.assertEqual(data["invitation"]["status_label"], "待激活")
        self.assertTrue(data["account"]["password_pending"])
        self.assertEqual(data["account"]["username"], "iv_dto_user")
