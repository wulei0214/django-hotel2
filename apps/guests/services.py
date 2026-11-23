"""客人证件服务（V1.1 M-02 P0）：密文落库、HMAC 查重、独立权限揭示 + 审计。

加密：Fernet（cryptography 库），密钥来自 DJANGO_PII_KEY；
      未配置时回退用 SECRET_KEY 派生（开发便利），生产必须显式配置独立密钥。
查重：doc_no_hash = HMAC-SHA256(pii_key, doc_no)，不存明文，防彩虹表。
揭示：需要 `guests.document.reveal` 权限，每次写 AuditEvent（不落明文）。
"""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.core.phone import legacy_phone_variants
from apps.core.phone import normalize_phone as _normalize_phone


def _pii_key_bytes():
    """PII 加密/哈希密钥：统一密钥派生入口（WB-123）。

    密钥来源顺序：
      1. settings.DJANGO_PII_KEY（由 hotel/settings.py 从环境变量注入）；
      2. 生产环境（settings.IS_PRODUCTION）缺失时 fail-fast，**禁止回退
         SECRET_KEY**——证件/手机号密文与 HMAC 必须与会话/签名密钥隔离；
      3. 开发/测试允许 SECRET_KEY 派生回退。
    数据迁移 apps/guests/migrations/0007 使用同口径内联实现，避免迁移
    依赖业务模块注册表；两处算法必须保持一致。
    """
    key = getattr(settings, "DJANGO_PII_KEY", "")
    if key:
        return hashlib.sha256(str(key).encode("utf-8")).digest()
    if getattr(settings, "IS_PRODUCTION", False):
        raise ImproperlyConfigured(
            "生产环境必须配置 DJANGO_PII_KEY（独立 PII 密钥），" "禁止回退 SECRET_KEY（WB-123）。"
        )
    # 开发/测试回退：仅限非生产环境
    return hashlib.sha256(str(settings.SECRET_KEY).encode("utf-8")).digest()


def _fernet():
    return Fernet(base64.urlsafe_b64encode(_pii_key_bytes()))


def doc_no_hash(doc_no):
    """不可逆 HMAC 查重哈希（加盐防彩虹表）。"""
    return hmac.new(
        _pii_key_bytes(), (doc_no or "").strip().encode("utf-8"), hashlib.sha256
    ).hexdigest()


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
    return hmac.new(
        _pii_key_bytes(), (normalized or "").strip().encode("utf-8"), hashlib.sha256
    ).hexdigest()


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


def register_document(
    *,
    guest,
    doc_type,
    doc_no,
    verified_by=None,
    issuing_country="CN",
    expiry_date=None,
    request=None,
):
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
        if (
            GuestDocument.objects.filter(guest__property_id=guest.property_id, doc_no_hash=h)
            .exclude(guest=guest)
            .exists()
        ):
            raise ValidationError(
                "该证件已登记在其他客人档案（同物业查重），" "如为同一人请走客人合并/关联流程"
            )
        index_row = (
            GuestDocumentIndex.objects.select_for_update()
            .filter(property_id=guest.property_id, doc_no_hash=h)
            .first()
        )
        if index_row is None:
            try:
                with transaction.atomic():
                    GuestDocumentIndex.objects.create(
                        property=guest.property, guest=guest, doc_no_hash=h
                    )
            except IntegrityError:
                # 并发抢先登记：唯一约束仲裁后按当前占用者判定
                index_row = GuestDocumentIndex.objects.filter(
                    property_id=guest.property_id, doc_no_hash=h
                ).first()
                if index_row is None or index_row.guest_id != guest.pk:
                    raise ValidationError("该证件已登记至其他客人档案") from None
        elif index_row.guest_id != guest.pk:
            raise ValidationError("该证件已登记至其他客人档案")
        doc = GuestDocument.objects.create(
            guest=guest,
            doc_type=doc_type,
            doc_no_ciphertext=encrypt_doc_no(doc_no),
            doc_no_hash=h,
            issuing_country=issuing_country,
            expiry_date=expiry_date,
            verified_by=verified_by,
            verified_at=verified_at,
        )
        # WB-102 收尾：审计必须与证件登记同事务。审计落库失败时整笔登记
        # 回滚，不能出现「证件已占用但无审计轨迹」的半完成状态。
        AuditEvent.record(
            action="guest.document_register",
            entity_type="GuestDocument",
            entity_id=doc.pk,
            actor=verified_by,
            property_obj=guest.property,
            after={"doc_type": doc_type, "masked": _mask(doc_no)},
            request=request,
        )
    return doc


def _require_reveal_reason(reason):
    """§10.2：PII 揭示理由强制——空白/过短理由一律拒绝（fail-closed）。

    MC11-G360-04：与服务层对齐 API 层（guest_center reveal 端点）的
    8-240 字符策略，消除双轨口径漂移；API 层为唯一生产入口，本服务
    保留供管理命令/内部工具复用（口径一致）。"""
    reason = str(reason or "").strip()
    if len(reason) < 8 or len(reason) > 240:
        raise ValueError("揭示理由必须为 8 至 240 个字符")
    return reason


def reveal_document(document, *, revealed_by, reason="", request=None):
    """揭示证件完整值：需要 `guests.document.reveal` 权限；reason 必填；每次写 AuditEvent。"""
    # P1-01（方案 §9.1）：证件揭示唯一正式权限码 hotelapp.guest_document_reveal；
    # 业务授权统一 AuthorizationService（物业范围 + 岗位权限）
    from apps.identity.authorization import AuthorizationService

    if not AuthorizationService.has_permission(
        revealed_by, document.guest.property, "hotelapp.guest_document_reveal"
    ):
        raise PermissionDenied("揭示证件完整值需要独立权限")
    # Phase 5-B（方案 §10.2）：理由为空/全空白 → 拒绝，且不产生揭示值与审计
    reason = _require_reveal_reason(reason)
    plain = decrypt_doc_no(document.doc_no_ciphertext)
    AuditEvent.record(
        action="guest.document_reveal",
        entity_type="GuestDocument",
        entity_id=document.pk,
        actor=revealed_by,
        property_obj=document.guest.property,
        after={"masked": _mask(plain)},
        reason=reason,
        request=request,
    )
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
        revealed_by, phone.guest.property, "hotelapp.guest_phone_reveal"
    ):
        raise PermissionDenied("揭示电话完整值需要独立权限")
    # Phase 5-B（方案 §10.2）：理由为空/全空白 → 拒绝，且不产生揭示值与审计
    reason = _require_reveal_reason(reason)
    plain = phone_plain(phone)
    AuditEvent.record(
        action="guest.phone_reveal",
        entity_type="GuestPhone",
        entity_id=phone.pk,
        actor=revealed_by,
        property_obj=phone.guest.property,
        after={"masked": _mask(plain)},
        reason=reason,
        request=request,
    )
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
    """标准化手机号：只保留数字（TASK-02 统一口径）。

    历史问题：本函数原先保留 `+`（`re.sub(r"[^\d+]", "", ...)`），而预订侧
    `apps.reservations.services.normalize_phone` 只保留数字。同一号码
    （如 `+86 138 0000 1111`）在两条链路上算出**不同** `phone_hash`：
      * 客人档案侧：HMAC("+8613800001111")
      * 预订/待确认锁行侧：HMAC("8613800001111")
    后果是共享号码判定（M-03 规则 2）、同手机号 PENDING 上限（§6.4）与
    `PendingBookingGate.gate_key` 三处口径漂移，同一自然人可被当成不同人。
    现统一委托 `apps.core.phone.normalize_phone`（纯数字形态），与预订侧
    完全同源。

    历史数据不改写：迁移 0007 上线前以 `+数字` 录入的档案，其 `phone_hash`
    仍是 HMAC("+数字")。读侧比对请用 `phone_hash_candidates()`，它同时
    覆盖规范形态与历史形态，避免存量档案检索/共享判定漏判。
    """
    return _normalize_phone(raw)


def phone_hash_candidates(raw):
    """号码的哈希候选集：`(HMAC(纯数字), HMAC(+数字))`，供读侧兼容比对。

    仅用于**读取**（`phone_hash__in=` 过滤），不得用于写库——写入一律用
    `phone_hash(normalize_phone(x))`，保证新数据只有一种形态。

    返回顺序固定（规范形态在前、历史形态在后）且已去重；空号码返回空元组，
    调用方必须跳过该过滤条件，不得用空集合直接过滤（会得到"零命中"，
    把"没有号码"误判为"号码不存在"）。
    """
    digests = []
    for variant in legacy_phone_variants(normalize_phone(raw)):
        digest = phone_hash(variant)
        if digest not in digests:
            digests.append(digest)
    return tuple(digests)


class GuestServiceError(Exception):
    pass


class GuestFieldError(GuestServiceError):
    """入参字段格式/长度错误（RSV-10 口径：应映射 400 VALIDATION_ERROR，
    与业务状态冲突 409 区分）。"""

    pass
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
    def create_guest(
        *,
        property_obj,
        full_name,
        phone="",
        email="",
        notes="",
        created_by=None,
        request=None,
        source="前台登记",
    ):
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
        shared_phone = bool(digest and GuestPhone.objects.filter(phone_hash=digest).exists())
        if shared_phone:
            GuestPhone.objects.filter(phone_hash=digest).update(shared_review_required=True)
        guest = Guest.objects.create(
            property=property_obj,
            full_name=full_name,
            phone_ciphertext=encrypt_phone(norm) if norm else "",
            phone_hash=digest,
            email=email or "",
            notes=notes or "",
        )
        # TASK-03 阶段 1：姓名 PII 双写（密文/盲索引/密钥版本；明文列观察期保留）
        from .name_pii import fill_guest_name_pii

        fill_guest_name_pii(guest, full_name)
        guest.save(update_fields=["full_name_ciphertext", "full_name_hmac", "pii_key_version"])
        if norm:
            GuestPhone.objects.create(
                guest=guest,
                phone_ciphertext=encrypt_phone(norm),
                phone_hash=digest,
                is_primary=True,
                source=source,
                shared_review_required=shared_phone,
            )
        AuditEvent.record(
            action="guest.create",
            entity_type="Guest",
            entity_id=guest.pk,
            actor=created_by,
            property_obj=property_obj,
            after={"full_name": full_name, "phone_masked": _mask(phone)},
            request=request,
        )
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

        guest = (
            Guest.objects.select_for_update()
            .select_related()
            .get(pk=guest_id, property=property_obj)
        )
        changed = {}
        # 自测发现（2026-11-19）：超长字段此前直接落库触发 MySQL DataError
        # → API 500；按模型 max_length 在服务层校验，超长返回 400 语义错误。
        field_limits = {"full_name": 80, "email": 254, "notes": 500}
        for field in ("full_name", "email", "notes"):
            if field in fields and fields[field] is not None:
                new_val = str(fields[field]).strip()
                if len(new_val) > field_limits[field]:
                    raise GuestFieldError(
                        f"{field} 长度不能超过 {field_limits[field]} 字符"
                        f"（当前 {len(new_val)}）。"
                    )
                old_val = getattr(guest, field) or ""
                if new_val != old_val:
                    changed[field] = {"before": old_val, "after": new_val}
                    setattr(guest, field, new_val)
        # TASK-03 阶段 1：姓名变更同步双写（密文/盲索引/密钥版本随明文一致更新）
        if changed.get("full_name"):
            from .name_pii import fill_guest_name_pii

            fill_guest_name_pii(guest, changed["full_name"]["after"])
        phone = fields.get("phone")
        if phone is not None:
            norm = normalize_phone(phone)
            if norm and phone_hash(norm) != guest.phone_hash:
                old_phone = guest_phone_plain(guest)
                digest = phone_hash(norm)
                GuestPhone.objects.filter(guest=guest, is_primary=True).update(
                    is_primary=False, primary_guest_key=None
                )
                shared_phone = (
                    GuestPhone.objects.filter(phone_hash=digest).exclude(guest=guest).exists()
                )
                if shared_phone:
                    GuestPhone.objects.filter(phone_hash=digest).update(shared_review_required=True)
                try:
                    with transaction.atomic():
                        GuestPhone.objects.create(
                            guest=guest,
                            phone_ciphertext=encrypt_phone(norm),
                            phone_hash=digest,
                            is_primary=True,
                            source="前台变更",
                            shared_review_required=shared_phone,
                        )
                except IntegrityError:
                    # WB-104：唯一约束冲突（同 guest 重复号码）→ 整体回滚，
                    # 旧主号码保持未降级状态
                    raise GuestServiceError("该客人已登记过此号码，请勿重复添加。") from None
                guest.phone_ciphertext = encrypt_phone(norm)
                guest.phone_hash = digest
                changed["phone"] = {"before": _mask(old_phone), "after": _mask(norm)}
        if changed:
            guest.save()
            AuditEvent.record(
                action="guest.update",
                entity_type="Guest",
                entity_id=guest.pk,
                actor=updated_by,
                property_obj=guest.property,
                before={k: v["before"] for k, v in changed.items()},
                after={k: v["after"] for k, v in changed.items()},
                request=request,
            )
        return guest, changed

    @staticmethod
    @transaction.atomic
    def add_phone(
        *,
        guest,
        phone,
        phone_type="MOBILE",
        is_primary=False,
        source="前台登记",
        added_by=None,
        request=None,
    ):
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
                is_primary=False, primary_guest_key=None
            )
        try:
            with transaction.atomic():
                phone_obj = GuestPhone.objects.create(
                    guest=guest,
                    phone_ciphertext=encrypt_phone(norm),
                    phone_hash=digest,
                    phone_type=phone_type,
                    is_primary=is_primary,
                    source=source,
                )
        except IntegrityError:
            raise GuestServiceError("该客人已登记过此号码，请勿重复添加。") from None
        if is_primary:
            guest.phone_ciphertext = encrypt_phone(norm)
            guest.phone_hash = digest
            guest.save(update_fields=("phone_ciphertext", "phone_hash", "updated_at"))
        AuditEvent.record(
            action="guest.phone_add",
            entity_type="Guest",
            entity_id=guest.pk,
            actor=added_by,
            property_obj=guest.property,
            after={"masked": _mask(norm), "type": phone_type, "is_primary": is_primary},
            request=request,
        )
        return phone_obj

    @staticmethod
    @transaction.atomic
    def set_primary_phone(*, guest, phone_id, changed_by=None, request=None):
        """把存量号码设为客人主号码（复用 add_phone 的降级/同步口径）。"""
        from .models import GuestPhone

        guest = type(guest).objects.select_for_update().get(pk=guest.pk)
        target = GuestPhone.objects.select_for_update().filter(pk=phone_id, guest=guest).first()
        if target is None:
            raise GuestServiceError("该联系方式不属于当前客人档案。")
        if target.is_primary:
            return target, False
        GuestPhone.objects.filter(guest=guest, is_primary=True).update(
            is_primary=False, primary_guest_key=None
        )
        target.is_primary = True
        target.primary_guest_key = f"primary:{guest.pk}:{target.pk}"[:32]
        target.save(update_fields=("is_primary", "primary_guest_key", "updated_at"))
        guest.phone_ciphertext = target.phone_ciphertext
        guest.phone_hash = target.phone_hash
        guest.save(update_fields=("phone_ciphertext", "phone_hash", "updated_at"))
        AuditEvent.record(
            action="guest.phone_set_primary",
            entity_type="GuestPhone",
            entity_id=target.pk,
            actor=changed_by,
            property_obj=guest.property,
            after={"guest": guest.pk, "is_primary": True},
            request=request,
        )
        return target, True

    @staticmethod
    @transaction.atomic
    def verify_phone(*, guest, phone_id, verified_by=None, request=None):
        """人工核验共享号码；不自动合并客人档案。"""
        from .models import GuestPhone

        target = GuestPhone.objects.select_for_update().filter(pk=phone_id, guest=guest).first()
        if target is None:
            raise GuestServiceError("该联系方式不属于当前客人档案。")
        target.verified_at = timezone.now()
        target.save(update_fields=("verified_at", "updated_at"))
        AuditEvent.record(
            action="guest.phone_verified",
            entity_type="GuestPhone",
            entity_id=target.pk,
            actor=verified_by,
            property_obj=guest.property,
            after={"shared_review_required": target.shared_review_required},
            request=request,
        )
        return target

    @staticmethod
    @transaction.atomic
    def update_document(
        *,
        document,
        doc_type=None,
        expiry_date=None,
        issuing_country=None,
        is_primary=None,
        changed_by=None,
        request=None,
    ):
        """更新证件非号码字段；换号须走新登记 + 索引仲裁，不在本方法改 hash。"""
        from .models import GuestDocument

        document = GuestDocument.objects.select_for_update().get(pk=document.pk)
        changed = {}
        for field, value in (
            ("doc_type", doc_type),
            ("expiry_date", expiry_date),
            ("issuing_country", issuing_country),
            ("is_primary", is_primary),
        ):
            if value is not None and getattr(document, field) != value:
                setattr(document, field, value)
                changed[field] = value
        if changed:
            if changed.get("is_primary"):
                GuestDocument.objects.filter(guest=document.guest).exclude(pk=document.pk).update(
                    is_primary=False
                )
            document.save(update_fields=(*changed.keys(), "updated_at"))
            audit_after = {
                key: value.isoformat() if hasattr(value, "isoformat") else value
                for key, value in changed.items()
            }
            AuditEvent.record(
                action="guest.document_updated",
                entity_type="GuestDocument",
                entity_id=document.pk,
                actor=changed_by,
                property_obj=document.guest.property,
                after=audit_after,
                request=request,
            )
        return document, bool(changed)

    @staticmethod
    @transaction.atomic
    def verify_document(*, document, verified_by, request=None):
        """后补核验：核验人与核验时间由模型 save 强制成对。"""
        from .models import GuestDocument

        document = GuestDocument.objects.select_for_update().get(pk=document.pk)
        document.verified_by = verified_by
        document.verified_at = timezone.now()
        document.save(update_fields=("verified_by", "verified_at", "updated_at"))
        AuditEvent.record(
            action="guest.document_verified",
            entity_type="GuestDocument",
            entity_id=document.pk,
            actor=verified_by,
            property_obj=document.guest.property,
            after={"verified": True},
            request=request,
        )
        return document

    @staticmethod
    @transaction.atomic
    def invalidate_document(*, document, changed_by=None, reason="", request=None):
        """失效证件：保留密文/哈希与索引占用，只写保留截止日并取消主证件。"""
        from .models import GuestDocument

        document = GuestDocument.objects.select_for_update().get(pk=document.pk)
        if document.retention_until:
            return document, False
        document.retention_until = timezone.localdate()
        document.is_primary = False
        document.save(update_fields=("retention_until", "is_primary", "updated_at"))
        AuditEvent.record(
            action="guest.document_invalidated",
            entity_type="GuestDocument",
            entity_id=document.pk,
            actor=changed_by,
            property_obj=document.guest.property,
            after={"retention_until": document.retention_until.isoformat()},
            reason=str(reason or "")[:240],
            request=request,
        )
        return document, True

    # ---------- A3 客人合并（排期 V2.0 第二主线；身份方案 §8.2 冻结解除） ----------
    #
    # 冻结解除依据：PMS 全局最终开发排期 V2.0（2026-11-16）第二主线 A3
    # 「客人合并」获批交付——主档案合并为人工确认动作（本服务只做执行，
    # 匹配/候选仍由 merge_services 只读盘点），满足 §8.2-4 对「主档案、
    # 源档案、操作者、时间、理由和完整关联映射」的留痕要求。
    # 边界不变：不改任何账务事实（Folio/Payment/LedgerEntry 归属原预订
    # 不动，随 Reservation.guest FK 一起迁移）；不物理删除源档案（软归档）。

    MERGE_ACTION = "guest.merged"
    MERGE_REASON_MAX = 240

    @staticmethod
    def _merge_counts(guest):
        """合并前后的引用面摘要（审计 before/after 用，只读）。"""
        from apps.reservations.models import ReservationGuest, StayGuest

        from .models import GuestDocument, GuestMembership, GuestPhone

        return {
            "reservations": guest.reservations.count(),
            "reservation_guests": ReservationGuest.objects.filter(guest=guest).count(),
            "stay_guests": StayGuest.objects.filter(guest=guest).count(),
            "phones": GuestPhone.objects.filter(guest=guest).count(),
            "documents": GuestDocument.objects.filter(guest=guest).count(),
            "memberships": GuestMembership.objects.filter(guest=guest).count(),
        }

    @classmethod
    @transaction.atomic
    def merge_guests(cls, primary, duplicate, *, performed_by=None, reason="", request=None):
        """把 duplicate 档案合并入 primary（软归档；同事务原子执行）。

        校验：
          * 两档案均已落库且不是同一人；
          * 必须同物业（跨物业拒绝）；
          * 任意一方不得已是合并产物（merged_into 非空拒绝，防链式/重复合并）；
          * reason 必填（1-240 字符），完整进入审计。

        迁移（同一事务，任一步失败整体回滚）：
          * GuestDocument 改挂 primary（primary 已有同 (doc_type, doc_no_hash)
            的证件时丢弃 duplicate 的重复行，计数入审计）；
          * GuestDocumentIndex 占用行改指 primary；
          * GuestPhone 按 phone_hash 合并去重——primary 已有同号则丢弃
            duplicate 的行，否则改挂 primary；duplicate 的主号码仅在
            primary 尚无主号码时保留主标记，否则降级保留；
          * GuestMembership 转移到 primary 并保留等级历史；若 primary 已有
            有效会员，转移行的 ACTIVE 置为 TERMINATED（终止留史，审计记录）；
          * Reservation / ReservationGuest / StayGuest 的 guest FK 改指
            primary（Stay 经预订间接归属，不单独改写）；
          * Folio / Payment / LedgerEntry 等账务事实一律不动（归属原预订）。

        duplicate 标记 merged_into=primary + merged_at（软归档），guest 列表
        默认 exclude 已合并档案。全程写 AuditEvent（guest.merged，before/
        after 摘要）。返回迁移计数摘要。
        """
        from apps.reservations.models import Reservation, ReservationGuest, StayGuest

        from .models import (
            Guest,
            GuestDocument,
            GuestDocumentIndex,
            GuestMembership,
            GuestPhone,
        )

        reason = str(reason or "").strip()
        if not reason:
            raise GuestServiceError("客人合并必须填写业务理由。")
        if len(reason) > cls.MERGE_REASON_MAX:
            raise GuestServiceError(f"合并理由不得超过 {cls.MERGE_REASON_MAX} 个字符。")
        if primary is None or duplicate is None or primary.pk is None or duplicate.pk is None:
            raise GuestServiceError("主档案与待合并档案必须已落库。")

        # 按主键序加锁，避免并发合并两对交叉档案时的锁等待死锁
        first_id, second_id = sorted((primary.pk, duplicate.pk))
        locked = {
            g.pk: g for g in Guest.objects.select_for_update().filter(pk__in=(first_id, second_id))
        }
        if primary.pk not in locked or duplicate.pk not in locked:
            raise GuestServiceError("主档案与待合并档案必须已落库。")
        primary = locked[primary.pk]
        duplicate = locked[duplicate.pk]

        if primary.pk == duplicate.pk:
            raise GuestServiceError("主档案与待合并档案不能是同一人。")
        if primary.property_id != duplicate.property_id:
            raise GuestServiceError("只能合并同一物业内的客人档案。")
        if primary.merged_into_id is not None or duplicate.merged_into_id is not None:
            raise GuestServiceError("已合并的档案不能再参与合并（请核对主档案选择）。")

        property_obj = primary.property
        before_summary = {
            "primary": {"guest_id": primary.pk, "references": cls._merge_counts(primary)},
            "duplicate": {
                "guest_id": duplicate.pk,
                "full_name_masked": _mask(duplicate.full_name),
                "references": cls._merge_counts(duplicate),
            },
        }

        # 1) 证件：改挂 primary；primary 已有同 (doc_type, doc_no_hash) 时丢弃重复行
        documents_dropped = 0
        primary_doc_keys = set(
            GuestDocument.objects.filter(guest=primary).values_list("doc_type", "doc_no_hash")
        )
        for doc in GuestDocument.objects.filter(guest=duplicate).order_by("id"):
            if (doc.doc_type, doc.doc_no_hash) in primary_doc_keys:
                doc.delete()
                documents_dropped += 1
                continue
            GuestDocument.objects.filter(pk=doc.pk).update(guest=primary, updated_at=timezone.now())
            primary_doc_keys.add((doc.doc_type, doc.doc_no_hash))
        # 证件占用索引（WB-102）：(property, doc_no_hash) 唯一，改指 primary
        GuestDocumentIndex.objects.filter(guest=duplicate, property=property_obj).update(
            guest=primary, updated_at=timezone.now()
        )

        # 2) 电话：按 phone_hash 去重合并；主号码只在 primary 缺主号码时保留
        phones_dropped = 0
        primary_hashes = set(
            GuestPhone.objects.filter(guest=primary)
            .exclude(phone_hash__isnull=True)
            .exclude(phone_hash="")
            .values_list("phone_hash", flat=True)
        )
        primary_has_main = GuestPhone.objects.filter(guest=primary, is_primary=True).exists()
        duplicate_phones = list(GuestPhone.objects.filter(guest=duplicate).order_by("id"))
        for phone in duplicate_phones:
            if phone.phone_hash and phone.phone_hash in primary_hashes:
                phone.delete()
                phones_dropped += 1
                continue
            keep_primary_flag = bool(phone.is_primary) and not primary_has_main
            if phone.is_primary and not keep_primary_flag:
                phone.is_primary = False
            phone.guest = primary
            phone.save()
            if phone.phone_hash:
                primary_hashes.add(phone.phone_hash)
            if phone.is_primary:
                primary_has_main = True
        # primary 顶层电话字段为空时补齐（不覆盖既有值）：优先取 duplicate
        # 顶层密文/哈希（即 duplicate 的主号码），否则取转移后的主号码行
        if not primary.phone_hash:
            backfill_source = None
            if duplicate.phone_hash:
                primary.phone_ciphertext = duplicate.phone_ciphertext
                primary.phone_hash = duplicate.phone_hash
                primary.save(update_fields=("phone_ciphertext", "phone_hash", "updated_at"))
            else:
                backfill_source = (
                    GuestPhone.objects.filter(guest=primary)
                    .exclude(phone_hash__isnull=True)
                    .exclude(phone_hash="")
                    .order_by("-is_primary", "id")
                    .first()
                )
                if backfill_source is not None:
                    primary.phone_ciphertext = backfill_source.phone_ciphertext
                    primary.phone_hash = backfill_source.phone_hash
                    primary.save(update_fields=("phone_ciphertext", "phone_hash", "updated_at"))

        # 3) 会员：转移到 primary 并保留等级历史；主档案已有有效会员时终止转移行
        memberships_terminated = 0
        memberships_moved = GuestMembership.objects.filter(guest=duplicate).count()
        primary_active_member = GuestMembership.objects.filter(
            property=property_obj, guest=primary, status=GuestMembership.Status.ACTIVE
        ).exists()
        for membership in GuestMembership.objects.filter(guest=duplicate).order_by("id"):
            was_active = membership.status == GuestMembership.Status.ACTIVE
            if was_active and primary_active_member:
                membership.status = GuestMembership.Status.TERMINATED
                memberships_terminated += 1
            membership.guest = primary
            membership.active_guest_key = (
                f"{property_obj.pk}:{primary.pk}"
                if membership.status == GuestMembership.Status.ACTIVE
                else None
            )
            GuestMembership.objects.filter(pk=membership.pk).update(
                guest=primary,
                status=membership.status,
                active_guest_key=membership.active_guest_key,
                updated_at=timezone.now(),
            )
            if membership.status == GuestMembership.Status.ACTIVE:
                primary_active_member = True

        # 4) 预订与住客快照：guest FK 改指 primary（Stay 经预订间接归属；
        #    Folio/Payment 账务事实归属原预订，不动）
        reservations_moved = Reservation.objects.filter(
            property=property_obj, guest=duplicate
        ).update(guest=primary)
        reservation_guests_moved = ReservationGuest.objects.filter(guest=duplicate).update(
            guest=primary
        )
        stay_guests_moved = StayGuest.objects.filter(guest=duplicate).update(guest=primary)

        # 5) duplicate 软归档：merged_into + merged_at 成对写入（保留行不删除）
        duplicate.merged_into = primary
        duplicate.merged_at = timezone.now()
        duplicate.save(update_fields=("merged_into", "merged_at", "updated_at"))

        after_summary = {
            "primary_guest_id": primary.pk,
            "duplicate_guest_id": duplicate.pk,
            "merged_into": primary.pk,
            "merged_at": duplicate.merged_at.isoformat(),
            "mapping": {
                "duplicate_guest_id": duplicate.pk,
                "primary_guest_id": primary.pk,
                "kind": "guest_merge",
                "status": "merged",
            },
            "moved": {
                "reservations": reservations_moved,
                "reservation_guests": reservation_guests_moved,
                "stay_guests": stay_guests_moved,
                "memberships": memberships_moved,
            },
            "dropped": {
                "phones_deduped": phones_dropped,
                "documents_deduped": documents_dropped,
            },
            "memberships_terminated": memberships_terminated,
            "billing_untouched": True,
        }
        AuditEvent.record(
            action=cls.MERGE_ACTION,
            entity_type="Guest",
            entity_id=primary.pk,
            actor=performed_by,
            property_obj=property_obj,
            reason=reason,
            before=before_summary,
            after=after_summary,
            request=request,
        )

        return {
            "primary_guest_id": primary.pk,
            "duplicate_guest_id": duplicate.pk,
            "reservations_moved": reservations_moved,
            "reservation_guests_moved": reservation_guests_moved,
            "stay_guests_moved": stay_guests_moved,
            "phones_deduped": phones_dropped,
            "documents_deduped": documents_dropped,
            "memberships_terminated": memberships_terminated,
            "merged_at": duplicate.merged_at,
        }


class GuestMembershipService:
    """会员关系唯一写入口（V1.1 M-16）。"""

    @staticmethod
    @transaction.atomic
    def create_membership(
        *,
        property_obj,
        guest,
        member_no,
        level_code="STANDARD",
        joined_at=None,
        expires_at=None,
        source="新建",
        created_by=None,
        request=None,
    ):
        from .models import GuestMembership, MembershipLevelHistory

        member_no = str(member_no or "").strip()
        level_code = str(level_code or "").strip()
        if expires_at is not None:
            from django.utils import timezone as _tz

            effective_joined = joined_at or _tz.localdate()
            if expires_at < effective_joined:
                raise GuestServiceError("会员有效期不得早于加入日期。")
        if guest.property_id != property_obj.pk:
            raise GuestServiceError("客人不属于当前物业。")
        if not member_no or len(member_no) > 32:
            raise GuestServiceError("会员编号必须为 1 至 32 个字符。")
        if not level_code or len(level_code) > 32:
            raise GuestServiceError("会员等级代码必须为 1 至 32 个字符。")
        if GuestMembership.objects.filter(
            property=property_obj, guest=guest, status=GuestMembership.Status.ACTIVE
        ).exists():
            raise GuestServiceError("该客人已有有效会员关系。")
        if GuestMembership.objects.filter(property=property_obj, member_no=member_no).exists():
            raise GuestServiceError(f"会员编号 {member_no} 已被使用。")
        m = GuestMembership.objects.create(
            property=property_obj,
            guest=guest,
            member_no=member_no,
            level_code=level_code,
            status=GuestMembership.Status.ACTIVE,
            joined_at=joined_at,
            expires_at=expires_at,
            source=source,
        )
        MembershipLevelHistory.objects.create(
            membership=m, to_level=level_code, reason="新建会员关系", changed_by=created_by
        )
        AuditEvent.record(
            action="membership.create",
            entity_type="GuestMembership",
            entity_id=m.pk,
            actor=created_by,
            property_obj=property_obj,
            after={
                "member_no": member_no,
                "level": level_code,
                "expires_at": str(expires_at) if expires_at else None,
            },
            request=request,
        )
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
            membership=membership,
            from_level=old,
            to_level=new_level,
            reason=reason,
            changed_by=changed_by,
        )
        AuditEvent.record(
            action="membership.level_change",
            entity_type="GuestMembership",
            entity_id=membership.pk,
            actor=changed_by,
            property_obj=membership.property,
            before={"level": old},
            after={"level": new_level},
            reason=reason,
            request=request,
        )
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
        if old_status not in (GuestMembership.Status.ACTIVE, GuestMembership.Status.PENDING):
            raise GuestServiceError(f"会员状态为 {old_status}，不允许暂停。")
        membership.status = GuestMembership.Status.SUSPENDED
        membership.save(update_fields=("status", "updated_at"))
        AuditEvent.record(
            action="membership.suspend",
            entity_type="GuestMembership",
            entity_id=membership.pk,
            actor=suspended_by,
            property_obj=membership.property,
            before={"status": old_status},
            after={"status": GuestMembership.Status.SUSPENDED},
            reason=reason,
            request=request,
        )
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
            raise GuestServiceError(f"会员状态为 {old_status}，仅暂停状态允许恢复。")
        if (
            GuestMembership.objects.filter(
                property_id=membership.property_id,
                guest_id=membership.guest_id,
                status=GuestMembership.Status.ACTIVE,
            )
            .exclude(pk=membership.pk)
            .exists()
        ):
            raise GuestServiceError("该客人已有其他有效会员关系，不能恢复本会员。")
        membership.status = GuestMembership.Status.ACTIVE
        membership.save(update_fields=("status", "updated_at"))
        AuditEvent.record(
            action="membership.restore",
            entity_type="GuestMembership",
            entity_id=membership.pk,
            actor=restored_by,
            property_obj=membership.property,
            before={"status": old_status},
            after={"status": GuestMembership.Status.ACTIVE},
            reason=reason,
            request=request,
        )
        return membership, True
