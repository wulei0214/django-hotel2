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

from django.db import transaction
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from cryptography.fernet import Fernet, InvalidToken

from apps.core.models import AuditEvent


def _pii_key_bytes():
    """PII 加密/哈希密钥：优先 DJANGO_PII_KEY；否则从 SECRET_KEY 派生（开发回退）。"""
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


def register_document(*, guest, doc_type, doc_no, verified_by=None,
                      issuing_country="CN", expiry_date=None, request=None):
    """登记证件：密文+hash 落库；同一物业内同一证件查重（不自动合并）。"""
    doc_no = (doc_no or "").strip()
    if not doc_no:
        raise ValidationError("证件号不能为空")
    from .models import GuestDocument
    h = doc_no_hash(doc_no)
    if GuestDocument.objects.filter(guest__property=guest.property_id,
                                    doc_no_hash=h).exclude(guest=guest).exists():
        raise ValidationError("该证件已登记在其他客人档案（同物业查重），"
                              "如为同一人请走客人合并/关联流程")
    doc = GuestDocument.objects.create(
        guest=guest, doc_type=doc_type,
        doc_no_ciphertext=encrypt_doc_no(doc_no), doc_no_hash=h,
        issuing_country=issuing_country, expiry_date=expiry_date,
        verified_by=verified_by)
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
    """
    from apps.identity.authorization import AuthorizationService
    if not AuthorizationService.has_permission(
            revealed_by, phone.guest.property, "hotelapp.guest_phone_reveal"):
        raise PermissionDenied("揭示电话完整值需要独立权限")
    # Phase 5-B（方案 §10.2）：理由为空/全空白 → 拒绝，且不产生揭示值与审计
    reason = _require_reveal_reason(reason)
    AuditEvent.record(
        action="guest.phone_reveal", entity_type="GuestPhone",
        entity_id=phone.pk, actor=revealed_by, property_obj=phone.guest.property,
        after={"masked": _mask(phone.phone)}, reason=reason, request=request)
    return phone.phone


def _mask(value):
    v = (value or "").strip()
    if len(v) < 6:
        return v
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


# ---------- V1.1 Phase 2：GuestService（统一客人写入口） ----------

def normalize_phone(raw):
    """标准化手机号：去空格/横线/括号，保留数字和+号。"""
    import re
    return re.sub(r"[^\d+]", "", (raw or "").strip())


class GuestServiceError(Exception):
    """客人服务业务校验失败。"""


class GuestService:
    """Guest/电话/证件唯一写入口（方案 §4.2：前台/Admin/命令统一调此服务）。"""

    @staticmethod
    def create_guest(*, property_obj, full_name, phone="", email="",
                     notes="", created_by=None, request=None):
        """创建客人档案；共享电话只标记人工核验，不把不同客人自动合并。"""
        from .models import Guest, GuestPhone
        full_name = (full_name or "").strip()
        if not full_name:
            raise GuestServiceError("客人姓名不能为空。")
        norm = normalize_phone(phone)
        shared_phone = bool(norm and GuestPhone.objects.filter(
            phone_normalized=norm).exists())
        if shared_phone:
            GuestPhone.objects.filter(phone_normalized=norm).update(
                shared_review_required=True)
        guest = Guest.objects.create(
            property=property_obj, full_name=full_name,
            phone=phone, phone_normalized=norm or None,
            email=email or "", notes=notes or "")
        if norm:
            GuestPhone.objects.create(
                guest=guest, phone=phone, phone_normalized=norm,
                is_primary=True, source="前台登记",
                shared_review_required=shared_phone)
        AuditEvent.record(
            action="guest.create", entity_type="Guest", entity_id=guest.pk,
            actor=created_by, property_obj=property_obj,
            after={"full_name": full_name, "phone_masked": _mask(phone)},
            request=request)
        return guest

    @staticmethod
    def update_guest(*, guest_id, property_obj, updated_by=None, request=None, **fields):
        """物业范围内更新客人档案；手机号变更保留历史并写审计。"""
        from .models import Guest, GuestPhone
        guest = Guest.objects.select_related().get(pk=guest_id, property=property_obj)
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
            if norm and norm != guest.phone_normalized:
                old_phone = guest.phone or ""
                GuestPhone.objects.filter(
                    guest=guest, is_primary=True).update(
                        is_primary=False, primary_guest_key=None)
                shared_phone = GuestPhone.objects.filter(
                    phone_normalized=norm).exclude(guest=guest).exists()
                if shared_phone:
                    GuestPhone.objects.filter(phone_normalized=norm).update(
                        shared_review_required=True)
                GuestPhone.objects.create(
                    guest=guest, phone=phone, phone_normalized=norm,
                    is_primary=True, source="前台变更",
                    shared_review_required=shared_phone)
                guest.phone = phone
                guest.phone_normalized = norm
                changed["phone"] = {"before": _mask(old_phone),
                                    "after": _mask(phone)}
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
    def add_phone(*, guest, phone, phone_type="MOBILE", is_primary=False,
                  source="前台登记", added_by=None, request=None):
        """添加电话号码；主号码切换时旧号码自动降级。"""
        from .models import GuestPhone
        norm = normalize_phone(phone)
        if not norm:
            raise GuestServiceError("电话号码不能为空。")
        if is_primary:
            GuestPhone.objects.filter(guest=guest, is_primary=True).update(
                is_primary=False, primary_guest_key=None)
        phone_obj = GuestPhone.objects.create(
            guest=guest, phone=phone, phone_normalized=norm,
            phone_type=phone_type, is_primary=is_primary, source=source)
        if is_primary:
            guest.phone = phone
            guest.phone_normalized = norm
            guest.save(update_fields=("phone", "phone_normalized", "updated_at"))
        AuditEvent.record(
            action="guest.phone_add", entity_type="Guest", entity_id=guest.pk,
            actor=added_by, property_obj=guest.property,
            after={"masked": _mask(phone), "type": phone_type,
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


