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


def reveal_document(document, *, revealed_by, reason="", request=None):
    """揭示证件完整值：需要 `guests.document.reveal` 权限；每次写 AuditEvent。"""
    if not revealed_by.has_perm("guests.document_reveal"):
        raise PermissionDenied("揭示证件完整值需要独立权限")
    plain = decrypt_doc_no(document.doc_no_ciphertext)
    AuditEvent.record(
        action="guest.document_reveal", entity_type="GuestDocument",
        entity_id=document.pk, actor=revealed_by, property_obj=document.guest.property,
        after={"masked": _mask(plain)}, reason=reason[:240], request=request)
    return plain


def _mask(value):
    v = (value or "").strip()
    if len(v) < 6:
        return v
    return v[:2] + "*" * (len(v) - 4) + v[-2:]
