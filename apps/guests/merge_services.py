"""Guest 重复档案治理（A3）：候选报告 / 只读影响面盘点 / 人工关联标记。

规范依据（docs/PMS2.0_当前版本/01_产品需求与业务设计/
PMS2身份认证与员工住客账户体系改造开发方案2.0_2026-11-10.md）：

  §8.2-1  不自动按手机号、姓名、证件号合并 Guest。
  §8.2-2  新预订复用 Guest 必须先有明确匹配策略与人工确认流程；策略批准前
          不得擅自复用或合并。
  §8.2-3  支持人工建立重复档案关联/疑似重复标记，但不物理删除源档案。
  §8.2-4  合并一旦未来获批，必须保留主档案、源档案、操作者、时间、理由和
          完整关联映射。
  §8.2-5  合并必须审查 Reservation、ReservationGuest、Folio、LedgerEntry、
          Payment、Refund、AuditEvent、LegacyRecordLink 等引用关系；禁止
          直接改写或删除账务历史。
  §8.2-6  旧 Client→Guest 的映射必须使用 LegacyRecordLink 或经批准的迁移
          映射表，保留原 ID 和来源系统。
  §8.2-7  合并策略、匹配置信度、冲突处理、回滚与人工审批流程属于业务待确认
          项；**在批准前只能做只读盘点和候选重复报告**。
  §18.1   重复档案的正式匹配、人工合并审批与合并后主档案规则必须由业务负责
          人批准；未决事项不得被开发人员以默认实现方式「悄悄决定」。

本模块实现范围（严格限定，不越界）：

  ✅ find_duplicate_candidates()    只读候选报告：只列「证据信号」，不给置信度
                                    评分、不设自动阈值、不做任何自动合并。
  ✅ preview_merge_impact()         只读影响面盘点：按 §8.2-5 逐类统计引用并
                                    列出冲突事实（只陈述事实，不给处置结论）。
  ✅ link_duplicate_guests()        人工建立重复档案关联/疑似重复标记
                                    （§8.2-3 本期固定执行）：只写审计映射，
                                    **不移动任何业务引用、不物理删除源档案**。
  ✅ dismiss_duplicate_candidate()  人工判定「非同一人」的否定结论留痕（同为只审计）。
  ✅ list_duplicate_links()          只读查询已建立的关联映射。
  ⛔ execute_merge()                 物理合并 / FK 迁移 / 源档案归档 / 撤销补偿
                                    —— 全部冻结，恒定拒绝执行。

关联映射载体说明：本期**不新增 Model**。理由：
  1. §8.2-7 规定批准前只能做只读盘点与候选报告，物理合并所需的正式载体
     （如 PMS3.0 规划的 GuestMergeRequest）尚未获批，提前建表会与未来批准
     的设计冲突，并造成「第二套映射机制」（违反治理 V1.1 §3.3）；
  2. 关联映射是**只增不改的审计事实**，AuditEvent 已是全项目唯一审计载体
     （append-only + DB 触发器防篡改），且 §8.2-4 要求的「主档案、源档案、
     操作者、时间、理由和完整关联映射」可完整落入其字段。
  待业务负责人批准合并策略后，再评估是否升格为一等 Model（迁移走 L3 审批）。
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.core.models import AuditEvent, LegacyRecordLink

from .models import Guest, GuestDocument, GuestMembership, GuestPhone
from .services import GuestServiceError, _mask

# ---------- 常量 ----------

#: 证据信号（**只陈述事实，不含任何评分/阈值**——匹配置信度属业务待确认项）
SIGNAL_SHARED_PHONE = "SHARED_PHONE_HASH"
SIGNAL_SAME_NAME = "SAME_NAME"
SIGNAL_DOCUMENT_HASH = "DOCUMENT_HASH_CONFLICT"

#: 审计动作码
ACTION_LINKED = "guest.duplicate_linked"
ACTION_DISMISSED = "guest.duplicate_dismissed"

#: 建立/解除重复档案关联所需的权限码（复用既有客人写权限，不新增权限码）
PERMISSION_LINK = "hotelapp.change_client"

FROZEN_REASON = (
    "Guest 物理合并在业务负责人批准合并策略前处于冻结状态"
    "（PMS2 身份方案 2.0 §8.2-7 / §18.1：匹配置信度、冲突处理、回滚与人工"
    "审批流程属业务待确认项，批准前只能做只读盘点和候选重复报告）。"
)


class GuestMergePolicyFrozen(GuestServiceError):
    """Guest 物理合并未获业务批准时的显式拒绝（fail-closed）。"""


# ---------- 内部工具 ----------


def _validate_pair(property_obj, source_guest, target_guest):
    """校验一对档案：同物业、不同档案、均已落库。"""
    if source_guest is None or target_guest is None:
        raise GuestServiceError("源档案与目标档案必须同时提供。")
    if source_guest.pk is None or target_guest.pk is None:
        raise GuestServiceError("源档案与目标档案必须已落库。")
    if source_guest.pk == target_guest.pk:
        raise GuestServiceError("源档案与目标档案不能是同一档案。")
    if source_guest.property_id != property_obj.pk or target_guest.property_id != property_obj.pk:
        raise GuestServiceError("重复档案关联只能在同一物业内建立。")


def _normalized_name(name):
    return (name or "").strip().casefold()


def _pair_key(a, b):
    return (a, b) if a < b else (b, a)


def _guest_ids_for_phone_hash(property_obj, digest):
    return list(
        GuestPhone.objects.filter(guest__property_id=property_obj.pk, phone_hash=digest)
        .values_list("guest_id", flat=True)
        .distinct()
    )


def _guest_ids_for_doc_hash(property_obj, digest):
    return list(
        GuestDocument.objects.filter(guest__property_id=property_obj.pk, doc_no_hash=digest)
        .values_list("guest_id", flat=True)
        .distinct()
    )


def _observed_signals(source_guest, target_guest):
    """两人档案之间**实际观察到**的证据信号（只读，不含评分）。"""
    signals = []
    src_phones = set(
        GuestPhone.objects.filter(guest=source_guest)
        .exclude(phone_hash__isnull=True)
        .exclude(phone_hash="")
        .values_list("phone_hash", flat=True)
    )
    tgt_phones = set(
        GuestPhone.objects.filter(guest=target_guest)
        .exclude(phone_hash__isnull=True)
        .exclude(phone_hash="")
        .values_list("phone_hash", flat=True)
    )
    if src_phones & tgt_phones:
        signals.append(SIGNAL_SHARED_PHONE)
    if _normalized_name(source_guest.full_name) == _normalized_name(
        target_guest.full_name
    ) and _normalized_name(source_guest.full_name):
        signals.append(SIGNAL_SAME_NAME)
    src_docs = set(
        GuestDocument.objects.filter(guest=source_guest).values_list("doc_no_hash", flat=True)
    )
    tgt_docs = set(
        GuestDocument.objects.filter(guest=target_guest).values_list("doc_no_hash", flat=True)
    )
    if src_docs & tgt_docs:
        signals.append(SIGNAL_DOCUMENT_HASH)
    return signals


def _reference_inventory(guest):
    """§8.2-5 要求的引用面盘点：逐类统计该档案被引用的业务事实数量（只读）。"""
    from apps.billing.models import (
        Folio,
        LedgerEntry,
        Payment,
        Refund,
        RefundLiability,
    )
    from apps.reservations.models import Reservation, ReservationGuest, StayGuest

    folio_ids = list(Folio.objects.filter(guest=guest).values_list("pk", flat=True))
    return {
        "reservations": Reservation.objects.filter(guest=guest).count(),
        "reservation_guests": ReservationGuest.objects.filter(guest=guest).count(),
        "stay_guests": StayGuest.objects.filter(guest=guest).count(),
        "folios": len(folio_ids),
        "open_folios": Folio.objects.filter(guest=guest, status=Folio.Status.OPEN).count(),
        "ledger_entries": LedgerEntry.objects.filter(folio_id__in=folio_ids).count(),
        "payments": Payment.objects.filter(reservation__guest=guest).count(),
        "refunds": Refund.objects.filter(payment__reservation__guest=guest).count(),
        "refund_liabilities": RefundLiability.objects.filter(reservation__guest=guest).count(),
        "audit_events": AuditEvent.objects.filter(
            entity_type="Guest", entity_id=str(guest.pk)
        ).count(),
        "legacy_record_links": LegacyRecordLink.objects.filter(
            target_model="Guest", target_pk=str(guest.pk)
        ).count(),
        "memberships": GuestMembership.objects.filter(guest=guest).count(),
        "active_memberships": GuestMembership.objects.filter(
            guest=guest, status=GuestMembership.Status.ACTIVE
        ).count(),
        "phones": GuestPhone.objects.filter(guest=guest).count(),
        "documents": GuestDocument.objects.filter(guest=guest).count(),
    }


def _detect_conflicts(source_guest, target_guest):
    """冲突事实清单：只陈述客观冲突，不给处置结论（处置口径待业务批准）。"""
    conflicts = []

    src_active = GuestMembership.objects.filter(
        guest=source_guest, status=GuestMembership.Status.ACTIVE
    ).count()
    tgt_active = GuestMembership.objects.filter(
        guest=target_guest, status=GuestMembership.Status.ACTIVE
    ).count()
    if src_active and tgt_active:
        conflicts.append(
            {
                "kind": "BOTH_ACTIVE_MEMBERSHIP",
                "detail": "两个档案各持有一条有效会员关系；主档案归属与会员编号"
                "处置口径待业务批准。",
            }
        )

    src_primary = GuestPhone.objects.filter(guest=source_guest, is_primary=True).count()
    tgt_primary = GuestPhone.objects.filter(guest=target_guest, is_primary=True).count()
    if src_primary and tgt_primary:
        conflicts.append(
            {
                "kind": "BOTH_PRIMARY_PHONE",
                "detail": "两个档案各有主号码；合并后主号码唯一性处置口径待业务批准。",
            }
        )

    for signal in _observed_signals(source_guest, target_guest):
        if signal == SIGNAL_SHARED_PHONE:
            conflicts.append(
                {
                    "kind": "SHARED_PHONE_HASH",
                    "detail": "两个档案登记了同一标准化号码（共享号码，不等于同一自然人）。",
                }
            )
        elif signal == SIGNAL_DOCUMENT_HASH:
            conflicts.append(
                {
                    "kind": "SHARED_DOCUMENT_HASH",
                    "detail": "两个档案登记了同一证件号哈希（证件占用仲裁为历史遗留）。",
                }
            )

    if (source_guest.email or "").strip() != (target_guest.email or "").strip():
        conflicts.append(
            {
                "kind": "EMAIL_MISMATCH",
                "detail": "两个档案的电子邮箱不一致；保留哪一份待业务批准。",
            }
        )
    if _normalized_name(source_guest.full_name) != _normalized_name(target_guest.full_name):
        conflicts.append(
            {
                "kind": "NAME_MISMATCH",
                "detail": "两个档案的姓名不一致；保留哪一份待业务批准。",
            }
        )

    src_inv = _reference_inventory(source_guest)
    if src_inv["ledger_entries"]:
        conflicts.append(
            {
                "kind": "SOURCE_HAS_LEDGER_ENTRIES",
                "detail": f"源档案已有 {src_inv['ledger_entries']} 条账本凭证；"
                "按 §8.2-5 禁止改写或删除账务历史。",
            }
        )
    if src_inv["open_folios"]:
        conflicts.append(
            {
                "kind": "SOURCE_HAS_OPEN_FOLIO",
                "detail": f"源档案仍有 {src_inv['open_folios']} 张未结账单；"
                "账务结清前不得迁移或归档。",
            }
        )
    return conflicts


def _existing_link(property_obj, source_guest, target_guest):
    """是否已存在同一对档案的关联/否定记录（审计载体，Python 侧比对避免 JSON 查询）。"""
    events = AuditEvent.objects.filter(
        property=property_obj,
        entity_type="Guest",
        action__in=(ACTION_LINKED, ACTION_DISMISSED),
        entity_id__in=(str(source_guest.pk), str(target_guest.pk)),
    )
    for event in events:
        payload = event.after_json or {}
        if not isinstance(payload, dict):
            continue
        src = payload.get("source_guest_id")
        tgt = payload.get("target_guest_id")
        if {src, tgt} == {source_guest.pk, target_guest.pk}:
            return event
    return None


# ---------- 公开服务 ----------


def find_duplicate_candidates(property_obj, *, limit=200):
    """只读候选重复报告（§8.2-7 允许的唯一产出形态之一）。

    只列**证据信号**（共享号码哈希 / 同名 / 证件哈希冲突），**不给置信度
    评分、不设自动阈值、不做任何自动合并**——匹配置信度与阈值属业务待确认
    项（§8.2-7 / §18.1），由人工逐条判断。

    返回结构含 ``policy`` 段显式声明：``auto_merge=False``、
    ``confidence_scored=False``、``approval_required=True``。
    """
    if limit < 1:
        raise GuestServiceError("limit 必须大于 0。")

    pairs: dict[tuple[int, int], dict] = {}

    def _add(a, b, signal, detail=None):
        key = _pair_key(a, b)
        item = pairs.setdefault(key, {"signals": [], "details": {}})
        if signal not in item["signals"]:
            item["signals"].append(signal)
        if detail is not None:
            item["details"].setdefault(signal, []).append(detail)

    # 信号 1：同一标准化号码关联多个档案（共享号码；M-03 语义：可共享，
    # 不等于同一自然人——与 §8.2-1「不得用手机号直接认定同一自然人」一致）
    shared_phone_hashes = (
        GuestPhone.objects.filter(guest__property_id=property_obj.pk)
        .exclude(phone_hash__isnull=True)
        .exclude(phone_hash="")
        .values("phone_hash")
        .annotate(guest_count=Count("guest", distinct=True))
        .filter(guest_count__gt=1)
    )
    for row in shared_phone_hashes:
        ids = _guest_ids_for_phone_hash(property_obj, row["phone_hash"])
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                _add(
                    ids[i],
                    ids[j],
                    SIGNAL_SHARED_PHONE,
                    {"phone_hash_prefix": row["phone_hash"][:8]},
                )

    # 信号 2：同一证件号哈希出现在多个档案（正常写入会被 WB-102 占用索引
    # 拦截，故本信号主要指向历史遗留或绕过占用的数据）
    shared_doc_hashes = (
        GuestDocument.objects.filter(guest__property_id=property_obj.pk)
        .values("doc_no_hash")
        .annotate(guest_count=Count("guest", distinct=True))
        .filter(guest_count__gt=1)
    )
    for row in shared_doc_hashes:
        ids = _guest_ids_for_doc_hash(property_obj, row["doc_no_hash"])
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                _add(
                    ids[i],
                    ids[j],
                    SIGNAL_DOCUMENT_HASH,
                    {"doc_no_hash_prefix": row["doc_no_hash"][:8]},
                )

    # 信号 3：同物业同名（弱信号，仅供人工参考）
    name_groups: dict[str, list[int]] = {}
    for guest_id, full_name in Guest.objects.filter(property_id=property_obj.pk).values_list(
        "pk", "full_name"
    ):
        key = _normalized_name(full_name)
        if not key:
            continue
        name_groups.setdefault(key, []).append(guest_id)
    for key, ids in name_groups.items():
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                _add(ids[i], ids[j], SIGNAL_SAME_NAME, {"full_name_masked": _mask(key)})

    candidates = []
    for (a, b), item in sorted(pairs.items())[:limit]:
        candidates.append(
            {
                "source_guest_id": a,
                "target_guest_id": b,
                "signals": item["signals"],
                "signal_details": item["details"],
                "shared_phone_review_required": SIGNAL_SHARED_PHONE in item["signals"],
                "auto_merge": False,
            }
        )

    by_signal: dict[str, int] = {}
    for candidate in candidates:
        for signal in candidate["signals"]:
            by_signal[signal] = by_signal.get(signal, 0) + 1

    return {
        "property_id": property_obj.pk,
        "generated_at": timezone.now().isoformat(),
        "candidates": candidates,
        "counts": {
            "candidate_pairs": len(candidates),
            "by_signal": by_signal,
            "truncated": len(pairs) > len(candidates),
        },
        "policy": {
            "auto_merge": False,
            "confidence_scored": False,
            "approval_required": True,
            "note": "匹配置信度、阈值与冲突处置属业务待确认项（§8.2-7 / §18.1）；"
            "本报告只列证据信号，不给评分、不给自动结论。",
        },
        "read_only": True,
    }


def preview_merge_impact(property_obj, *, source_guest, target_guest):
    """只读合并影响面盘点（§8.2-5 引用面 + 冲突事实）。

    本函数**不执行任何写操作**；返回体中的 ``executable`` 恒为 ``False``，
    并附冻结原因，避免调用方误以为可以直接合并。
    """
    _validate_pair(property_obj, source_guest, target_guest)
    return {
        "property_id": property_obj.pk,
        "generated_at": timezone.now().isoformat(),
        "source": {
            "guest_id": source_guest.pk,
            "full_name_masked": _mask(source_guest.full_name),
            "references": _reference_inventory(source_guest),
        },
        "target": {
            "guest_id": target_guest.pk,
            "full_name_masked": _mask(target_guest.full_name),
            "references": _reference_inventory(target_guest),
        },
        "conflicts": _detect_conflicts(source_guest, target_guest),
        "executable": False,
        "frozen_reason": FROZEN_REASON,
        "read_only": True,
    }


@transaction.atomic
def link_duplicate_guests(
    *, property_obj, source_guest, target_guest, reason, operator=None, signals=None, request=None
):
    """人工建立重复档案关联/疑似重复标记（§8.2-3 本期固定执行）。

    只写审计映射：**不移动任何业务引用、不物理删除源档案、不做物理合并**
    （§8.2-3 / §8.2-7）。关联映射完整保留「源档案、目标档案、操作者、时间、
    理由与信号」，满足 §8.2-4 对未来获批合并的留痕要求。

    权限：需 ``hotelapp.change_client``（复用既有客人写权限，不新增权限码）。
    """
    reason = str(reason or "").strip()
    if not reason:
        raise GuestServiceError("建立重复档案关联必须填写理由。")
    if len(reason) > 240:
        raise GuestServiceError("关联理由不得超过 240 个字符。")
    _validate_pair(property_obj, source_guest, target_guest)

    if operator is None:
        raise GuestServiceError("建立重复档案关联必须记录操作者。")
    from apps.identity.authorization import AuthorizationService

    if not AuthorizationService.has_permission(operator, property_obj, PERMISSION_LINK):
        raise PermissionDenied("建立重复档案关联需要客人档案修改权限。")

    if _existing_link(property_obj, source_guest, target_guest):
        raise GuestServiceError("该对档案已有重复关联或否定记录，请勿重复建立。")

    observed = (
        list(signals) if signals is not None else _observed_signals(source_guest, target_guest)
    )

    event = AuditEvent.record(
        action=ACTION_LINKED,
        entity_type="Guest",
        entity_id=source_guest.pk,
        actor=operator,
        property_obj=property_obj,
        reason=reason,
        after={
            "source_guest_id": source_guest.pk,
            "target_guest_id": target_guest.pk,
            "mapping": {
                "source_guest_id": source_guest.pk,
                "target_guest_id": target_guest.pk,
                "kind": "duplicate_association",
                "status": "not_merged",
            },
            "signals": observed,
            "data_moved": False,
            "source_deleted": False,
            "policy_status": "merge_policy_pending_approval",
        },
        request=request,
    )
    return {
        "source_guest_id": source_guest.pk,
        "target_guest_id": target_guest.pk,
        "signals": observed,
        "audit_event_id": event.pk,
        "merged": False,
        "data_moved": False,
    }


@transaction.atomic
def dismiss_duplicate_candidate(
    *, property_obj, source_guest, target_guest, reason, operator=None, request=None
):
    """人工判定「非同一人」并留痕（同样只写审计，不改任何档案数据）。"""
    reason = str(reason or "").strip()
    if not reason:
        raise GuestServiceError("否定重复关联必须填写理由。")
    if len(reason) > 240:
        raise GuestServiceError("否定理由不得超过 240 个字符。")
    _validate_pair(property_obj, source_guest, target_guest)
    if operator is None:
        raise GuestServiceError("否定重复关联必须记录操作者。")
    from apps.identity.authorization import AuthorizationService

    if not AuthorizationService.has_permission(operator, property_obj, PERMISSION_LINK):
        raise PermissionDenied("否定重复关联需要客人档案修改权限。")
    if _existing_link(property_obj, source_guest, target_guest):
        raise GuestServiceError("该对档案已有重复关联或否定记录，请勿重复建立。")

    event = AuditEvent.record(
        action=ACTION_DISMISSED,
        entity_type="Guest",
        entity_id=source_guest.pk,
        actor=operator,
        property_obj=property_obj,
        reason=reason,
        after={
            "source_guest_id": source_guest.pk,
            "target_guest_id": target_guest.pk,
            "decision": "not_same_person",
            "data_moved": False,
            "source_deleted": False,
        },
        request=request,
    )
    return {
        "source_guest_id": source_guest.pk,
        "target_guest_id": target_guest.pk,
        "decision": "not_same_person",
        "audit_event_id": event.pk,
    }


def list_duplicate_links(property_obj, *, guest=None):
    """只读查询已建立的重复档案关联/否定记录（审计载体，可回溯）。"""
    events = AuditEvent.objects.filter(
        property=property_obj, entity_type="Guest", action__in=(ACTION_LINKED, ACTION_DISMISSED)
    ).order_by("created_at")
    links = []
    for event in events:
        payload = event.after_json or {}
        if not isinstance(payload, dict):
            continue
        src = payload.get("source_guest_id")
        tgt = payload.get("target_guest_id")
        if guest is not None and guest.pk not in (src, tgt):
            continue
        links.append(
            {
                "source_guest_id": src,
                "target_guest_id": tgt,
                "kind": event.action,
                "signals": payload.get("signals", []),
                "merged": bool((payload.get("mapping") or {}).get("status") == "merged"),
                "reason": event.reason,
                "operator_id": event.actor_id,
                "created_at": event.created_at.isoformat(),
            }
        )
    return links


def execute_merge(
    *, property_obj, source_guest, target_guest, reason="", operator=None, request=None
):
    """Guest 物理合并——**冻结入口，恒定拒绝执行**。

    存在此函数是为了把冻结边界显式化：任何试图物理合并（迁移 FK、归档或
    删除源档案）的调用都会得到明确拒绝，而不是被「悄悄实现」为默认行为。
    解冻前提：业务负责人批准合并策略、匹配置信度、冲突处理、回滚与人工
    审批流程（§8.2-7 / §18.1），并按治理 V1.1 §2.3 走 L3/L4 变更审批。
    """
    raise GuestMergePolicyFrozen(FROZEN_REASON)
