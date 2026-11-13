"""AUTH-15：员工邀请激活状态机与 token 安全服务（方案 §5.2/§5.3）。

职责边界（方案 §4.2 / services.py 头注）：
  * 本模块只负责邀请对象的状态机与 token 生命周期（生成/摘要/撤销/重发/消费/
    过期扫描）；Invitation 的写入一律收口到 EmployeeInvitationService；
  * User 的开通、绑定、密码策略与设置一律经 EmployeeLifecycleService 事务方法
    （provision_invitation_account / activate_invitation_account），
    本模块绝不直接 create_user / set_password；
  * 明文 token 由 secrets.token_urlsafe(32) 生成（256 bit 熵，§5.3），仅在
    创建/重发的返回值中出现一次；数据库与审计只允许出现 SHA-256 摘要（§16.2）；
  * 对外失败信息统一为"邀请链接无效或已过期"，不泄露员工/邀请存在性；
    仅密码策略错误透出策略中文消息（不涉及任何员工是否存在的信息）；
  * 审计事件名固定（§16.1）：identity.invitation_created / invitation_resent /
    invitation_revoked / invitation_accepted；事件内容不含明文 token 与摘要。
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditEvent, Property

from .models import Invitation, PropertyStaffMembership, StaffProfile
from .services import EmployeeLifecycleError, EmployeeLifecycleService

audit = logging.getLogger("hotel.audit")

# §5.3：token 无效/过期/撤销/重复消费/绑定失效统一文案，不泄露存在性
UNIFIED_INVALID_MESSAGE = "邀请链接无效或已过期"


class InvitationError(Exception):
    """邀请业务失败；message 面向页面/操作者，禁止包含 token 明文或摘要。"""


def token_digest(raw_token):
    """token 的 SHA-256 十六进制摘要（§5.3：数据库只存安全 hash）。"""
    return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()


def invitation_ttl():
    """邀请有效期（§5.3 默认 24 小时；settings.IDENTITY_INVITATION_TTL_HOURS 可配）。"""
    return timedelta(hours=int(getattr(settings, "IDENTITY_INVITATION_TTL_HOURS", 24)))


def invite_accept_url(request, raw_token):
    """生成激活页绝对 URL（§5.3：Host 只取 request.get_host()，不受污染）。

    Django 的 get_host() 受 ALLOWED_HOSTS 校验，且默认配置
    （USE_X_FORWARDED_HOST=False）不读取 X-Forwarded-Host——转发头不能污染
    邀请链接；部署配置 IDENTITY_INVITATION_CANONICAL_HOST 时优先使用规范 Host。
    token 为 urlsafe base64 字符集（[A-Za-z0-9_-]），可安全拼入查询串。
    """
    canonical = (getattr(settings, "IDENTITY_INVITATION_CANONICAL_HOST", "") or "").strip()
    host = canonical or request.get_host()
    scheme = "https" if request.is_secure() else "http"
    # 末尾斜杠与 URLconf 一致，避免 APPEND_SLASH 301 跳转
    return f"{scheme}://{host}/invite/accept/?token={raw_token}"


class EmployeeInvitationService:
    """邀请状态机唯一写入口；View / 管理命令 / Admin 均不得直接写 Invitation。"""

    # ---------- 绑定与前置校验（内部） ----------
    @staticmethod
    def _validate_pair(profile, membership):
        """token 绑定性（§5.3）：任职必须属于该员工档案，不可改绑。"""
        if membership.staff_profile_id != profile.pk:
            raise InvitationError("任职关系与员工档案不匹配，不能发起邀请")

    @staticmethod
    def _validate_invite_allowed(profile, membership):
        """状态机转换 1 前置：员工在职、任职有效、物业有效。"""
        if profile.employment_status != StaffProfile.EmploymentStatus.ACTIVE:
            raise InvitationError("员工档案未处于在职状态，不能发起邀请")
        if (
            membership.status != PropertyStaffMembership.Status.ACTIVE
            or membership.property.status != Property.Status.ACTIVE
        ):
            raise InvitationError("当前任职或物业未处于有效状态，不能发起邀请")

    @staticmethod
    def _reject_when_account_active(profile):
        if profile.user and profile.user.has_usable_password():
            raise InvitationError("该员工已开通登录账号，无需邀请激活")

    @classmethod
    def _close_stale_pending(cls, profile, *, revoked_by=None):
        """发起/重发前关闭该员工全部 PENDING 邀请（§5.2 转换 3/4/5）。

        已超时的 PENDING 归档为 EXPIRED，未超时的标记 REVOKED；
        返回被撤销（REVOKED）的邀请 id 列表。
        """
        now = timezone.now()
        revoked_ids = []
        for invitation in Invitation.objects.select_for_update().filter(
            staff_profile=profile, status=Invitation.Status.PENDING
        ):
            if invitation.expires_at <= now:
                invitation.status = Invitation.Status.EXPIRED
            else:
                invitation.status = Invitation.Status.REVOKED
                invitation.revoked_at = now
                invitation.revoked_by = revoked_by
                revoked_ids.append(invitation.pk)
            invitation.save(update_fields=("status", "revoked_at", "revoked_by"))
        return revoked_ids

    # ---------- 创建（转换 1：未开通/待激活 → 新 PENDING） ----------
    @classmethod
    @transaction.atomic
    def create_invitation(cls, profile, membership, created_by, *, username, request=None):
        """发起邀请：撤销旧 PENDING → 预开通待激活账号 → 新建 PENDING 邀请。

        返回 (invitation, raw_token)；raw_token 明文仅本次返回，由调用方用
        invite_accept_url 拼接一次性链接（§5.3：明文只在创建时返回一次）。
        同一员工重复创建 → 撤销旧邀请再新建（任务口径 / §5.2 转换 5）。
        """
        cls._validate_pair(profile, membership)
        cls._validate_invite_allowed(profile, membership)
        cls._reject_when_account_active(profile)
        cls._close_stale_pending(profile, revoked_by=created_by)
        try:
            EmployeeLifecycleService.provision_invitation_account(
                profile, username=username, granted_by=created_by, property_obj=membership.property
            )
        except EmployeeLifecycleError as exc:
            # 账号开通失败（账号名占用等）→ 统一邀请业务错误；事务回滚
            raise InvitationError(str(exc)) from exc
        raw_token = secrets.token_urlsafe(32)  # 32 字节 = 256 bit 熵（§5.3）
        now = timezone.now()
        invitation = Invitation.objects.create(
            staff_profile=profile,
            membership=membership,
            token_hash=token_digest(raw_token),
            status=Invitation.Status.PENDING,
            expires_at=now + invitation_ttl(),
            created_by=created_by,
            last_sent_at=now,
        )
        AuditEvent.record(
            action="identity.invitation_created",
            entity_type="Invitation",
            entity_id=invitation.pk,
            actor=created_by,
            property_obj=membership.property,
            after={
                "profile_id": profile.pk,
                "membership_id": membership.pk,
                "account": profile.user.username,
                "resend_count": 0,
                "expires_at": invitation.expires_at.isoformat(),
            },
            reason="创建邀请；明文 token 与摘要不落审计",
            request=request,
        )
        audit.info(
            "邀请创建 user=%s 员工=%s(profile %s) 邀请=%s 账号=%s",
            getattr(created_by, "username", "system"),
            profile.full_name,
            profile.pk,
            invitation.pk,
            profile.user.username,
        )
        return invitation, raw_token

    # ---------- 重发（撤销旧 token + 新 token；resend_count 链上 +1） ----------
    @classmethod
    @transaction.atomic
    def resend(cls, invitation, *, resent_by, request=None):
        """重发邀请（§5.3：重发时撤销所有旧 token，生成新 token）。

        允许对 PENDING / EXPIRED / REVOKED 邀请重发；ACCEPTED（已激活）拒绝。
        返回 (new_invitation, raw_token)。
        """
        invitation = (
            Invitation.objects.select_for_update()
            .select_related("staff_profile", "membership__property")
            .get(pk=invitation.pk)
        )
        profile = invitation.staff_profile
        membership = invitation.membership
        if invitation.status == Invitation.Status.ACCEPTED:
            raise InvitationError("该邀请已被接受，账号已激活，无需重发")
        cls._validate_pair(profile, membership)
        cls._validate_invite_allowed(profile, membership)
        cls._reject_when_account_active(profile)
        cls._close_stale_pending(profile, revoked_by=resent_by)
        raw_token = secrets.token_urlsafe(32)
        now = timezone.now()
        new_invitation = Invitation.objects.create(
            staff_profile=profile,
            membership=membership,
            token_hash=token_digest(raw_token),
            status=Invitation.Status.PENDING,
            expires_at=now + invitation_ttl(),
            created_by=resent_by,
            resend_count=invitation.resend_count + 1,
            last_sent_at=now,
        )
        AuditEvent.record(
            action="identity.invitation_resent",
            entity_type="Invitation",
            entity_id=new_invitation.pk,
            actor=resent_by,
            property_obj=membership.property,
            after={
                "profile_id": profile.pk,
                "resend_count": new_invitation.resend_count,
                "revoked_invitation_id": invitation.pk,
                "expires_at": new_invitation.expires_at.isoformat(),
            },
            reason="重发邀请：旧 token 全部失效；明文 token 不落审计",
            request=request,
        )
        audit.info(
            "邀请重发 user=%s 员工=%s(profile %s) 旧邀请=%s 新邀请=%s",
            getattr(resent_by, "username", "system"),
            profile.full_name,
            profile.pk,
            invitation.pk,
            new_invitation.pk,
        )
        return new_invitation, raw_token

    # ---------- 撤销（转换 4：PENDING → REVOKED） ----------
    @classmethod
    @transaction.atomic
    def revoke(cls, invitation, *, revoked_by, request=None, reason=""):
        """管理员撤销：仅未超时的 PENDING 可撤销（超时的直接重发新邀请）。"""
        invitation = (
            Invitation.objects.select_for_update()
            .select_related("staff_profile", "membership__property")
            .get(pk=invitation.pk)
        )
        if invitation.status != Invitation.Status.PENDING:
            raise InvitationError("该邀请已失效或已使用，无需撤销")
        if invitation.expires_at <= timezone.now():
            raise InvitationError("该邀请已过期失效，可直接重发新邀请")
        now = timezone.now()
        invitation.status = Invitation.Status.REVOKED
        invitation.revoked_at = now
        invitation.revoked_by = revoked_by
        invitation.save(update_fields=("status", "revoked_at", "revoked_by"))
        AuditEvent.record(
            action="identity.invitation_revoked",
            entity_type="Invitation",
            entity_id=invitation.pk,
            actor=revoked_by,
            property_obj=invitation.membership.property,
            after={"profile_id": invitation.staff_profile_id, "status": Invitation.Status.REVOKED},
            reason=reason or "管理员撤销邀请；原链接立即失效",
            request=request,
        )
        audit.info(
            "邀请撤销 user=%s 员工=%s(profile %s) 邀请=%s",
            getattr(revoked_by, "username", "system"),
            invitation.staff_profile.full_name,
            invitation.staff_profile.pk,
            invitation.pk,
        )
        return invitation

    # ---------- 消费（转换 2：PENDING → ACCEPTED，一次性） ----------
    @staticmethod
    def _archive_if_stale(raw_token):
        """观察即归档（转换 3）：PENDING 且已超时 → EXPIRED（独立原子更新）。

        必须在主消费事务之外执行：主事务内"先归档再抛错"会被 savepoint
        回滚（无法持久化归档）。条件更新原子幂等，且只有真正超时的
        PENDING 行会被触碰；并发下与消费互斥（超时 token 不可能成功）。
        """
        Invitation.objects.filter(
            token_hash=token_digest(raw_token),
            status=Invitation.Status.PENDING,
            expires_at__lte=timezone.now(),
        ).update(status=Invitation.Status.EXPIRED)

    @classmethod
    def consume(cls, token, password, request=None):
        """一次性消费（§5.3）：行锁 + 状态条件更新，并发下只有一个成功。

        * select_for_update 串行化同一 token 的并发提交；随后再以
          "status 仍为 PENDING" 的条件更新兜底（后到者命中 0 行）。
        * token 无效/过期/撤销/重复消费/绑定失效 → 统一文案，不泄露存在性；
        * 密码经 EmployeeLifecycleService.activate_invitation_account 走
          validate_password 策略并置 must_change_password=False（§5.4）；
        * 成功写 identity.invitation_accepted 审计（与状态变更同事务）。
        失败统一 InvitationError；调用方（激活页）负责展示。
        """
        raw_token = (token or "").strip()
        if not raw_token:
            raise InvitationError(UNIFIED_INVALID_MESSAGE)
        # 超时归档先行（独立事务），随后主事务按最新状态统一拒绝
        cls._archive_if_stale(raw_token)
        with transaction.atomic():
            try:
                invitation = (
                    Invitation.objects.select_for_update()
                    .select_related("staff_profile", "membership__property")
                    .get(token_hash=token_digest(raw_token))
                )
            except Invitation.DoesNotExist:
                raise InvitationError(UNIFIED_INVALID_MESSAGE) from None
            now = timezone.now()
            if invitation.status != Invitation.Status.PENDING:
                raise InvitationError(UNIFIED_INVALID_MESSAGE)
            if invitation.expires_at <= now:
                raise InvitationError(UNIFIED_INVALID_MESSAGE)
            profile = invitation.staff_profile
            membership = invitation.membership
            # 绑定复核（§5.3）：token 不能跨员工/任职使用
            if membership.staff_profile_id != profile.pk:
                raise InvitationError(UNIFIED_INVALID_MESSAGE)
            # 状态机转换 2 前置：员工在职、任职有效、物业有效（§5.1 授权口径）
            if (
                profile.employment_status != StaffProfile.EmploymentStatus.ACTIVE
                or membership.status != PropertyStaffMembership.Status.ACTIVE
                or membership.property.status != Property.Status.ACTIVE
            ):
                raise InvitationError(UNIFIED_INVALID_MESSAGE)
            if not password or not password.strip():
                raise InvitationError("请设置登录密码")
            try:
                user = EmployeeLifecycleService.activate_invitation_account(
                    profile, password=password
                )
            except EmployeeLifecycleError as exc:
                # 密码策略/账号状态中文消息；不涉及其他员工存在性
                raise InvitationError(str(exc)) from exc
            # 状态条件更新：只允许 PENDING → ACCEPTED 一次（一次性消费兜底）
            updated = Invitation.objects.filter(
                pk=invitation.pk, status=Invitation.Status.PENDING
            ).update(status=Invitation.Status.ACCEPTED, consumed_at=now)
            if not updated:  # pragma: no cover — select_for_update 已串行化
                raise InvitationError(UNIFIED_INVALID_MESSAGE)
            invitation.status = Invitation.Status.ACCEPTED
            invitation.consumed_at = now
            AuditEvent.record(
                action="identity.invitation_accepted",
                entity_type="StaffProfile",
                entity_id=profile.pk,
                actor=user,
                property_obj=membership.property,
                after={
                    "invitation_id": invitation.pk,
                    "resend_count": invitation.resend_count,
                    "account": user.username,
                },
                reason="员工经邀请链接激活并自行设置密码；不记录密码与 token",
                request=request,
            )
            audit.info(
                "邀请激活 员工=%s(profile %s) 账号=%s 邀请=%s",
                profile.full_name,
                profile.pk,
                user.username,
                invitation.pk,
            )
        return invitation

    # ---------- 展示（激活页 GET 只读校验；不写库） ----------
    @classmethod
    def describe_for_activation(cls, token):
        """激活页 GET：token 有效且可激活时返回展示上下文，否则 None。

        与 consume 完全同一套有效性口径（状态/有效期/绑定/员工任职状态），
        但只读、不落任何状态变更，避免 GET 触发状态机转换。
        """
        raw_token = (token or "").strip()
        if not raw_token:
            return None
        try:
            invitation = Invitation.objects.select_related(
                "staff_profile", "membership__property"
            ).get(token_hash=token_digest(raw_token))
        except Invitation.DoesNotExist:
            return None
        profile = invitation.staff_profile
        membership = invitation.membership
        if (
            invitation.status != Invitation.Status.PENDING
            or invitation.expires_at <= timezone.now()
            or membership.staff_profile_id != profile.pk
            or profile.employment_status != StaffProfile.EmploymentStatus.ACTIVE
            or membership.status != PropertyStaffMembership.Status.ACTIVE
            or membership.property.status != Property.Status.ACTIVE
            or profile.user is None
        ):
            return None
        return {
            "invitation": invitation,
            "full_name": profile.full_name,
            "username": profile.user.username,
            "expires_at": invitation.expires_at,
        }

    # ---------- 过期扫描（转换 3，管理命令） ----------
    @staticmethod
    def expire_stale(now=None):
        """把全部 PENDING 且超过 expires_at 的邀请归档为 EXPIRED；返回处理数。

        幂等：已归档（EXPIRED/REVOKED/ACCEPTED）的行不会被再次触碰。
        扫描只写 hotel.audit 日志摘要，不逐条写 AuditEvent（§16.1 未定义
        过期事件名，避免事件噪音；单条过期在消费时已有统一拒绝路径）。
        """
        now = now or timezone.now()
        updated = Invitation.objects.filter(
            status=Invitation.Status.PENDING, expires_at__lte=now
        ).update(status=Invitation.Status.EXPIRED)
        if updated:
            audit.info("邀请过期扫描 过期=%d", updated)
        return updated
