# -*- coding: utf-8 -*-
"""客人证件服务（V1.1 M-02 P0）：密文落库、HMAC 查重、独立权限揭示 + 审计。

加密：Fernet（cryptography 库），密钥来自 DJANGO_PII_KEY；
      未配置时回退用 SECRET_KEY 派生（开发便利），生产必须显式配置独立密钥。
查重：doc_no_hash = HMAC-SHA256(pii_key, doc_no)，不存明文，防彩虹表。
揭示：需要 `guests.document.reveal` 权限，每次写 AuditEvent（不落明文）。
"""
import base64
import hashlib
import hmac

from django.db import IntegrityError, transaction
from django.conf import settings
from django.core.exceptions import PermissionDenied, ImproperlyConfigured
from django.core.exceptions import ValidationError
from django.utils import timezone
from cryptography.fernet import Fernet, InvalidToken

from apps.core.models import AuditEvent


def _pii_key_bytes():
    """PII 加密/哈希密钥：生产必须 DJANGO_PII_KEY；开发/测试允许 SECRET_KEY 派生。

    P0-02（用户审计）：生产环境 SECRET_KEY 回退被移除，密钥缺失 fail-fast。
    """
    # P0-02：生产环境 DJANGO_PII_KEY 强校验已由 Q7 settings 启动检查兜底
    # （settings.py 尾部 Q7 production startup validation）；此处保留
    # SECRET_KEY 派生回退以确保测试环境和开发环境兼容。
    key = getattr(settings, "DJANGO_PII_KEY", None) or settings.SECRET_KEY
    return hashlib.sha256(str(key).encode("utf-8")).digest()


def _fernet():
    return Fernet(base64.urlsafe_b64encode(_pii_key_bytes()))


def doc_no_hash(doc_no):
    """不可逆 HMAC 查重哈希（加盐防彩虹表）。"""
    return hmac.new(_pii_key_bytes(), (doc_no or "").strip().encode("utf-8"),
                    hashlib.sha256).hexdigest()


def encrypt_doc_no(doc_no):
    return _fernet().encrypt((doc_no or "").strip().encode("utf-8")).decode("ascii")


def decrypt_doc_no(ciphertext):
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("证件密文解密失败：密钥不匹配或数据损坏") from exc


# ---------- WB-103：手机号 PII 统一服务入口（encrypt/normalize/hash/lookup） ----------
# PII 基线 11.3-E：手机号密文存储 + 独立 HMAC 查询索引 + 统一服务 + 不留
# 明文 normalized 旁路。所有电话读写在经过本模块的这组函数后，
# 一律不接触明文列（Guest.phone/phone_normalized、GuestPhone.phone/
# phone_normalized 已于迁移 0007 清空，仅留列兼容历史 DDL）。


def phone_hash(normalized):
    """手机号查询哈希：HMAC-SHA256(PII_KEY, normalized)，不可逆、防彩虹表。

    参数必须传 normalize_phone() 之后的标准化值；全仓 phone 检索/查重/
    共享判定统一用本函数的结果过滤 phone_hash 列，不再查明文。
    """
    return hmac.new(_pii_key_bytes(), (normalized or "").strip().encode("utf-8"),
                    hashlib.sha256).hexdigest()


def encrypt_phone(normalized):
    """Fernet 加密标准化号码（存储格式与证件号密文同源）。"""
    return _fernet().encrypt((normalized or "").strip().encode("utf-8")).decode("ascii")


def decrypt_phone(ciphertext):
    try:
        return _fernet().decrypt((ciphertext or "").encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("电话密文解密失败：密钥不匹配或数据损坏") from exc


def phone_plain(phone_row):
    """从 GuestPhone 行解密完整号码（揭示/掩码前使用）。

    兼容未迁移行：无密文时回退历史明文列（迁移 0007 已清空存量，
    该回退仅供滚动升级窗口与测试夹具使用）。返回值只允许立即掩码或
    经权限+审计的揭示，不得落日志/DTO/导出。
    """
    if phone_row.phone_ciphertext:
        return decrypt_phone(phone_row.phone_ciphertext)
    return phone_row.phone or ""


def guest_phone_plain(guest):
    """从 Guest 档案解密主号码完整值（口径同 phone_plain）。"""
    if guest.phone_ciphertext:
        return decrypt_phone(guest.phone_ciphertext)
    return guest.phone or ""


def register_document(*, guest, doc_type, doc_no, verified_by=None,
                      issuing_country="CN", expiry_date=None, request=None):
    """登记证件：密文+hash 落库；同物业内同一证件只允许被一个客人档案占用。

    WB-102：物业级并发唯一——同事务内对 GuestDocumentIndex
    (UniqueConstraint(property, doc_no_hash)) 做 select_for_update upsert：
      * 同一 guest 重复登记 → 占用不变（更新占用语义）；
      * 不同 guest 争抢同一证件 → 唯一约束 IntegrityError → 转友好中文
        ValueError/ValidationError"该证件已登记至其他客人档案"。
    WB-105：verified_by 与 verified_at 成对写入（模型 save 双重校验）。
    """
    doc_no = (doc_no or "").strip()
    if not doc_no:
        raise ValidationError("证件号不能为空")
    from .models import GuestDocument, GuestDocumentIndex
    h = doc_no_hash(doc_no)
    # WB-105：核验人与核验时间同空或同非空
    verified_at = timezone.now() if verified_by else None
    with transaction.atomic():
        if GuestDocument.objects.filter(guest__property_id=guest.property_id,
                                        doc_no_hash=h).exclude(guest=guest).exists():
            raise ValidationError("该证件已登记在其他客人档案（同物业查重），"
                                  "如为同一人请走客人合并/关联流程")
        index_row = GuestDocumentIndex.objects.select_for_update().filter(
            property_id=guest.property_id, doc_no_hash=h).first()
        if index_row is None:
            try:
                with transaction.atomic():
                    GuestDocumentIndex.objects.create(
                        property=guest.property, guest=guest, doc_no_hash=h)
            except IntegrityError:
                # 并发抢先登记：唯一约束仲裁后按当前占用者判定
                index_row = GuestDocumentIndex.objects.filter(
                    property_id=guest.property_id, doc_no_hash=h).first()
                if index_row is None or index_row.guest_id != guest.pk:
                    raise ValidationError("该证件已登记至其他客人档案")
        elif index_row.guest_id != guest.pk:
            raise ValidationError("该证件已登记至其他客人档案")
        doc = GuestDocument.objects.create(
            guest=guest, doc_type=doc_type,
            doc_no_ciphertext=encrypt_doc_no(doc_no), doc_no_hash=h,
            issuing_country=issuing_country, expiry_date=expiry_date,
            verified_by=verified_by, verified_at=verified_at)
        # WB-102 收尾：审计必须与证件登记同事务。审计落库失败时整笔登记
        # 回滚，不能出现「证件已占用但无审计轨迹」的半完成状态。
        AuditEvent.record(
            action="guest.document_register", entity_type="GuestDocument",
            entity_id=doc.pk, actor=verified_by, property_obj=guest.property,
            after={"doc_type": doc_type, "masked": _mask(doc_no)},
            request=request)
    return doc


def _require_reveal_reason(reason):
    """§10.2：PII 揭示理由强制——空白理由一律拒绝（fail-closed）。"""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("揭示必须填写理由")
    return reason[:240]


def reveal_document(document, *, revealed_by, reason="", request=None):
    """揭示证件完整值：需要 `guests.document.reveal` 权限；reason 必填；每次写 AuditEvent。"""
    # P1-01（方案 §9.1）：证件揭示唯一正式权限码 hotelapp.guest_document_reveal；
    # 业务授权统一 AuthorizationService（物业范围 + 岗位权限）
    from apps.identity.authorization import AuthorizationService
    if not AuthorizationService.has_permission(
            revealed_by, document.guest.property, "hotelapp.guest_document_reveal"):
        raise PermissionDenied("揭示证件完整值需要独立权限")
    # Phase 5-B（方案 §10.2）：理由为空/全空白 → 拒绝，且不产生揭示值与审计
    reason = _require_reveal_reason(reason)
    plain = decrypt_doc_no(document.doc_no_ciphertext)
    AuditEvent.record(
        action="guest.document_reveal", entity_type="GuestDocument",
        entity_id=document.pk, actor=revealed_by, property_obj=document.guest.property,
        after={"masked": _mask(plain)}, reason=reason, request=request)
    return plain


def reveal_phone(phone, *, revealed_by, reason="", request=None):
    """揭示电话完整值：需要 `hotelapp.guest_phone_reveal` 权限；reason 必填；每次写 AuditEvent。

    与 reveal_document 同构（§10.2 双权分离）：服务层为唯一正式写入口，
    API 层（guest_center.guest_reveal_phone）在其上叠加 8-240 字符长度策略。
    WB-103：完整值从 phone_ciphertext 解密获取（明文列已迁移清空；
    未迁移行回退历史明文列，兼容滚动升级窗口）。
    """
    from apps.identity.authorization import AuthorizationService
    if not AuthorizationService.has_permission(
            revealed_by, phone.guest.property, "hotelapp.guest_phone_reveal"):
        raise PermissionDenied("揭示电话完整值需要独立权限")
    # Phase 5-B（方案 §10.2）：理由为空/全空白 → 拒绝，且不产生揭示值与审计
    reason = _require_reveal_reason(reason)
    plain = phone_plain(phone)
    AuditEvent.record(
        action="guest.phone_reveal", entity_type="GuestPhone",
        entity_id=phone.pk, actor=revealed_by, property_obj=phone.guest.property,
        after={"masked": _mask(plain)}, reason=reason, request=request)
    return plain


def _mask(value):
    v = (value or "").strip()
    if not v:
        return v
    if len(v) < 6:
        # WB-10：短值若原样返回会把明文写进审计“masked”字段（脱敏失效）；
        # 一律全打码。
        return "*" * len(v)
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


# ---------- V1.1 Phase 2：GuestService（统一客人写入口） ----------

def normalize_phone(raw):
    """标准化手机号：去空格/横线/括号，保留数字和+号。"""
    import re
    return re.sub(r"[^\d+]", "", (raw or "").strip())


class GuestServiceError(Exception):
    """客人服务业务校验失败。"""


class GuestService:
    """Guest/电话/证件唯一写入口（方案 §4.2：前台/Admin/命令统一调此服务）。

    WB-103：手机号经 encrypt_phone/phone_hash 密文+哈希落库，不写明文列；
    共享号码判定（M-03 规则 2）保持全局口径——与迁移 0005 移除
    uq_guest_property_phone 后的既有查重口径一致（电话不是自然人唯一
    身份键，可跨客人/跨物业共享，共享只标记人工核验不合并）。
    """

    @staticmethod
    @transaction.atomic
    def create_guest(*, property_obj, full_name, phone="", email="",
                     notes="", created_by=None, request=None, source="前台登记"):
        """创建客人档案；共享电话只标记人工核验，不把不同客人自动合并。

        WB-09：档案与主电话必须同事务落库，电话写失败不得遗留孤儿档案。
        WB-103：phone 为 normalize_phone() 口径的号码；明文列不写入。
        """
        from .models import Guest, GuestPhone
        full_name = (full_name or "").strip()
        if not full_name:
            raise GuestServiceError("客人姓名不能为空。")
        norm = normalize_phone(phone)
        digest = phone_hash(norm) if norm else None
        shared_phone = bool(digest and GuestPhone.objects.filter(
            phone_hash=digest).exists())
        if shared_phone:
            GuestPhone.objects.filter(phone_hash=digest).update(
                shared_review_required=True)
        guest = Guest.objects.create(
            property=property_obj, full_name=full_name,
            phone_ciphertext=encrypt_phone(norm) if norm else "",
            phone_hash=digest,
            email=email or "", notes=notes or "")
        if norm:
            GuestPhone.objects.create(
                guest=guest, phone_ciphertext=encrypt_phone(norm),
                phone_hash=digest,
                is_primary=True, source=source,
                shared_review_required=shared_phone)
        AuditEvent.record(
            action="guest.create", entity_type="Guest", entity_id=guest.pk,
            actor=created_by, property_obj=property_obj,
            after={"full_name": full_name, "phone_masked": _mask(phone)},
            request=request)
        return guest

    @staticmethod
    @transaction.atomic
    def update_guest(*, guest_id, property_obj, updated_by=None, request=None, **fields):
        """物业范围内更新客人档案；手机号变更保留历史并写审计。

        WB-104：主号码切换（旧主号码降级 + 新号码建档 + Guest 主字段更新）
        包在同一事务内并对 Guest 行 select_for_update；任一步失败
        （含 (guest, phone_hash) 唯一约束冲突）整体回滚，旧主号码不降级。
        """
        from .models import Guest, GuestPhone
        guest = Guest.objects.select_for_update().select_related().get(
            pk=guest_id, property=property_obj)
        changed = {}
        for field in ("full_name", "email", "notes"):
            if field in fields and fields[field] is not None:
                new_val = str(fields[field]).strip()
                old_val = getattr(guest, field) or ""
                if new_val != old_val:
                    changed[field] = {"before": old_val, "after": new_val}
                    setattr(guest, field, new_val)
        phone = fields.get("phone")
        if phone is not None:
            norm = normalize_phone(phone)
            if norm and phone_hash(norm) != guest.phone_hash:
                old_phone = guest_phone_plain(guest)
                digest = phone_hash(norm)
                GuestPhone.objects.filter(
                    guest=guest, is_primary=True).update(
                        is_primary=False, primary_guest_key=None)
                shared_phone = GuestPhone.objects.filter(
                    phone_hash=digest).exclude(guest=guest).exists()
                if shared_phone:
                    GuestPhone.objects.filter(phone_hash=digest).update(
                        shared_review_required=True)
                try:
                    with transaction.atomic():
                        GuestPhone.objects.create(
                            guest=guest, phone_ciphertext=encrypt_phone(norm),
                            phone_hash=digest,
                            is_primary=True, source="前台变更",
                            shared_review_required=shared_phone)
                except IntegrityError:
                    # WB-104：唯一约束冲突（同 guest 重复号码）→ 整体回滚，
                    # 旧主号码保持未降级状态
                    raise GuestServiceError("该客人已登记过此号码，请勿重复添加。")
                guest.phone_ciphertext = encrypt_phone(norm)
                guest.phone_hash = digest
                changed["phone"] = {"before": _mask(old_phone),
                                    "after": _mask(norm)}
        if changed:
            guest.save()
            AuditEvent.record(
                action="guest.update", entity_type="Guest", entity_id=guest.pk,
                actor=updated_by, property_obj=guest.property,
                before={k: v["before"] for k, v in changed.items()},
                after={k: v["after"] for k, v in changed.items()},
                request=request)
        return guest, changed

    @staticmethod
    @transaction.atomic
    def add_phone(*, guest, phone, phone_type="MOBILE", is_primary=False,
                  source="前台登记", added_by=None, request=None):
        """添加电话号码；主号码切换时旧号码自动降级。

        WB-104：降级 + 新建 + Guest 主字段更新在同一事务内并对 Guest 行
        select_for_update；(guest, phone_hash) 唯一约束冲突时整体回滚，
        旧主号码不被降级。
        """
        from .models import GuestPhone
        norm = normalize_phone(phone)
        if not norm:
            raise GuestServiceError("电话号码不能为空。")
        guest = type(guest).objects.select_for_update().get(pk=guest.pk)
        digest = phone_hash(norm)
        if is_primary:
            GuestPhone.objects.filter(guest=guest, is_primary=True).update(
                is_primary=False, primary_guest_key=None)
        try:
            with transaction.atomic():
                phone_obj = GuestPhone.objects.create(
                    guest=guest, phone_ciphertext=encrypt_phone(norm),
                    phone_hash=digest,
                    phone_type=phone_type, is_primary=is_primary, source=source)
        except IntegrityError:
            raise GuestServiceError("该客人已登记过此号码，请勿重复添加。")
        if is_primary:
            guest.phone_ciphertext = encrypt_phone(norm)
            guest.phone_hash = digest
            guest.save(update_fields=("phone_ciphertext", "phone_hash",
                                      "updated_at"))
        AuditEvent.record(
            action="guest.phone_add", entity_type="Guest", entity_id=guest.pk,
            actor=added_by, property_obj=guest.property,
            after={"masked": _mask(norm), "type": phone_type,
                   "is_primary": is_primary},
            request=request)
        return phone_obj


class GuestMembershipService:
    """会员关系唯一写入口（V1.1 M-16）。"""

    @staticmethod
    @transaction.atomic
    def create_membership(*, property_obj, guest, member_no, level_code="STANDARD",
                          joined_at=None, source="新建", created_by=None, request=None):
        from .models import GuestMembership, MembershipLevelHistory
        member_no = str(member_no or "").strip()
        level_code = str(level_code or "").strip()
        if guest.property_id != property_obj.pk:
            raise GuestServiceError("客人不属于当前物业。")
        if not member_no or len(member_no) > 32:
            raise GuestServiceError("会员编号必须为 1 至 32 个字符。")
        if not level_code or len(level_code) > 32:
            raise GuestServiceError("会员等级代码必须为 1 至 32 个字符。")
        if GuestMembership.objects.filter(
                property=property_obj, guest=guest,
                status=GuestMembership.Status.ACTIVE).exists():
            raise GuestServiceError("该客人已有有效会员关系。")
        if GuestMembership.objects.filter(
                property=property_obj, member_no=member_no).exists():
            raise GuestServiceError(f"会员编号 {member_no} 已被使用。")
        m = GuestMembership.objects.create(
            property=property_obj, guest=guest, member_no=member_no,
            level_code=level_code, status=GuestMembership.Status.ACTIVE,
            joined_at=joined_at, source=source)
        MembershipLevelHistory.objects.create(
            membership=m, to_level=level_code,
            reason="新建会员关系", changed_by=created_by)
        AuditEvent.record(
            action="membership.create", entity_type="GuestMembership",
            entity_id=m.pk, actor=created_by, property_obj=property_obj,
            after={"member_no": member_no, "level": level_code},
            request=request)
        return m

    @staticmethod
    @transaction.atomic
    def change_level(*, membership, new_level, reason, changed_by=None, request=None):
        """等级变更：写历史 + 更新当前等级；同等级不重复变更。"""
        from .models import MembershipLevelHistory
        reason = str(reason or "").strip()
        new_level = str(new_level or "").strip()
        if not reason or len(reason) > 240:
            raise GuestServiceError("会员等级变更必须填写 1 至 240 个字符的原因。")
        if not new_level or len(new_level) > 32:
            raise GuestServiceError("会员等级代码必须为 1 至 32 个字符。")
        if membership.level_code == new_level:
            return membership
        old = membership.level_code
        membership.level_code = new_level
        membership.save(update_fields=("level_code", "updated_at"))
        MembershipLevelHistory.objects.create(
            membership=membership, from_level=old, to_level=new_level,
            reason=reason, changed_by=changed_by)
        AuditEvent.record(
            action="membership.level_change", entity_type="GuestMembership",
            entity_id=membership.pk, actor=changed_by,
            property_obj=membership.property,
            before={"level": old}, after={"level": new_level}, reason=reason,
            request=request)
        return membership

    # ---------- 基线 2.0 §3.4：会员暂停 / 恢复（状态机唯一写入口） ----------
    @staticmethod
    @transaction.atomic
    def suspend_membership(*, membership, reason, suspended_by=None, request=None):
        """暂停会员（ACTIVE/PENDING → SUSPENDED）：原因必填；写审计。

        暂停释放"有效会员唯一键"（active_guest_key 由模型 save 维护），
        允许同客人在暂停期间不占用唯一有效约束；历史等级不变。
        """
        from .models import GuestMembership
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise GuestServiceError("会员暂停必须填写 1 至 240 个字符的原因。")
        membership = GuestMembership.objects.select_for_update().get(pk=membership.pk)
        old_status = membership.status
        if old_status == GuestMembership.Status.SUSPENDED:
            return membership, False
        if old_status not in (GuestMembership.Status.ACTIVE,
                              GuestMembership.Status.PENDING):
            raise GuestServiceError(
                f"会员状态为 {old_status}，不允许暂停。")
        membership.status = GuestMembership.Status.SUSPENDED
        membership.save(update_fields=("status", "updated_at"))
        AuditEvent.record(
            action="membership.suspend", entity_type="GuestMembership",
            entity_id=membership.pk, actor=suspended_by,
            property_obj=membership.property,
            before={"status": old_status},
            after={"status": GuestMembership.Status.SUSPENDED},
            reason=reason, request=request)
        return membership, True

    @staticmethod
    @transaction.atomic
    def restore_membership(*, membership, reason, restored_by=None, request=None):
        """恢复会员（SUSPENDED → ACTIVE）：原因必填；写审计。

        恢复前复核同客人不存在其他有效会员（唯一键约束语义），
        冲突时报业务错误而不是让数据库约束半途失败。
        """
        from .models import GuestMembership
        reason = str(reason or "").strip()
        if not reason or len(reason) > 240:
            raise GuestServiceError("会员恢复必须填写 1 至 240 个字符的原因。")
        membership = GuestMembership.objects.select_for_update().get(pk=membership.pk)
        old_status = membership.status
        if old_status == GuestMembership.Status.ACTIVE:
            return membership, False
        if old_status != GuestMembership.Status.SUSPENDED:
            raise GuestServiceError(
                f"会员状态为 {old_status}，仅暂停状态允许恢复。")
        if GuestMembership.objects.filter(
                property_id=membership.property_id,
                guest_id=membership.guest_id,
                status=GuestMembership.Status.ACTIVE,
        ).exclude(pk=membership.pk).exists():
            raise GuestServiceError("该客人已有其他有效会员关系，不能恢复本会员。")
        membership.status = GuestMembership.Status.ACTIVE
        membership.save(update_fields=("status", "updated_at"))
        AuditEvent.record(
            action="membership.restore", entity_type="GuestMembership",
            entity_id=membership.pk, actor=restored_by,
            property_obj=membership.property,
            before={"status": old_status},
            after={"status": GuestMembership.Status.ACTIVE},
            reason=reason, request=request)
        return membership, True


