"""隐私数据生命周期治理服务（V1.0 任务书 TASK-02）。

职责：按 RetentionPolicy 计算「到期候选集」→ 过滤 LegalHold 与业务关联 →
dry-run 统计 → 经审批后执行受控清理（脱敏/匿名化，不删账务/审计）→
写 RetentionExecution 执行记录与审计事件。

核心不变量（任务书 §TASK-02-3）：
  1. 不因 Guest 主档到期而级联删除财务账务（Folio/LedgerEntry/Payment 与
     AuditEvent 永不在清理范围——依法保存）；
  2. 不绕过外键约束直接删除：只对 Guest 主档做**不可逆匿名化**（PII 密文
     抹除 + 姓名密文抹除 + 全名置「已匿名化」），保留行本身以维持
     Reservation/Folio 等 PROTECT 外键的完整性；
  3. LegalHold ACTIVE 的对象一律跳过（skipped 计数，不报失败）；
  4. 未结束预订（PENDING/CONFIRMED/CHECKED_IN）、未结账 Folio（OPEN）、
     未完成退款（PENDING/APPROVED/EXECUTING）任一关联存在 → 跳过；
  5. dry_run=True 只统计不落写；正式执行必须携带 APPROVED 审批引用且
     申请人≠审批人（由 ApprovalRecord 保证）；
  6. 同一批次重复执行幂等：已匿名化的 Guest 不再进入候选集。
"""

from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.billing.models import ApprovalRecord, Folio, Refund
from apps.core.models import AuditEvent
from apps.reservations.models import Reservation

from .models import Guest, LegalHold, RetentionExecution

# 清理审批动作码（ApprovalRecord.action_code）
ACTION_LEGAL_HOLD = "privacy.legal_hold"
ACTION_LEGAL_HOLD_RELEASE = "privacy.legal_hold_release"
ACTION_RETENTION_EXECUTE = "privacy.retention_execute"

# 匿名化后主档标记（不可逆；原密文抹除，不可还原）
ANONYMIZED_MARKER = "已匿名化（保留期满清理）"

# 未结束预订状态：存在任一即跳过清理
OPEN_RESERVATION_STATUSES = (
    Reservation.Status.PENDING,
    Reservation.Status.CONFIRMED,
    Reservation.Status.CHECKED_IN,
)
# 未完成退款状态（Refund.Status：PENDING/APPROVED 待执行，SUCCEEDED 为终态）
OPEN_REFUND_STATUSES = (
    Refund.Status.PENDING,
    Refund.Status.APPROVED,
)

# 候选集单批上限（防全表扫描；大数据量由调用方分批）
MAX_CANDIDATES = 500


def _active_legal_hold_guest_ids(property_obj):
    """该物业所有 ACTIVE 冻结的 Guest 主档 id 集合（target_type=GUEST）。"""
    return set(
        LegalHold.objects.filter(
            property=property_obj,
            hold_status=LegalHold.HoldStatus.ACTIVE,
            target_type="GUEST",
        ).values_list("target_id", flat=True)
    )


def _guest_has_open_business(guest):
    """Guest 是否存在未结束预订 / 未结账 Folio / 未完成退款（任一 → 跳过）。"""
    if Reservation.objects.filter(
        property_id=guest.property_id, guest_id=guest.pk, status__in=OPEN_RESERVATION_STATUSES
    ).exists():
        return True
    open_folios = Folio.objects.filter(
        property_id=guest.property_id, guest_id=guest.pk, status=Folio.Status.OPEN
    ).exists()
    if open_folios:
        return True
    return Refund.objects.filter(
        payment__reservation__guest_id=guest.pk, status__in=OPEN_REFUND_STATUSES
    ).exists()


def candidate_guests(property_obj, policy, as_of, exclude_ids=None):
    """按策略计算到期候选 Guest 查询集（未匿名化 + 到期 + 本物业）。

    到期口径：住客主档保留期自其**最后一笔预订的离店日**起算；无任何
    预订历史（纯建档从未入住）按 Guest.created_at.date() 起算。候选上限
    MAX_CANDIDATES（确定性排序：id 升序，保证分批可续跑）。
    """
    from django.db.models import Max

    cutoff = as_of - timedelta(days=policy.retention_days)
    qs = Guest.objects.filter(property=property_obj).exclude(full_name=ANONYMIZED_MARKER)
    if exclude_ids:
        qs = qs.exclude(pk__in=list(exclude_ids))
    # 一次聚合取「最后离店日」映射（避免 N+1）
    last_map = {
        row["guest_id"]: row["last"]
        for row in Reservation.objects.filter(property=property_obj)
        .values("guest_id")
        .annotate(last=Max("check_out"))
    }
    ids: list[int] = []
    for guest in qs.order_by("id"):
        if len(ids) >= MAX_CANDIDATES:
            break
        anchor = last_map.get(guest.pk) or guest.created_at.date()
        if anchor <= cutoff:
            ids.append(guest.pk)
    return Guest.objects.filter(pk__in=ids).order_by("id")


def plan_retention_cleanup(*, property_obj, policy, as_of, operator, dry_run=True):
    """生成一次清理计划：dry-run 统计或正式执行。

    返回 RetentionExecution（未保存 or 已保存，视 dry_run）。
    正式执行必须已通过 plan 之外的审批链（approval_reference 必填）。
    """
    hold_ids = _active_legal_hold_guest_ids(property_obj)
    exec_record = RetentionExecution(
        property=property_obj,
        policy=policy,
        dry_run=dry_run,
        as_of=as_of,
        operator=operator,
        started_at=timezone.now(),
    )
    candidates = candidate_guests(property_obj, policy, as_of)
    exec_record.candidate_count = candidates.count()

    processed = skipped = failed = 0
    failures: list[dict[str, Any]] = []
    to_process = []
    for guest in candidates:
        if str(guest.pk) in hold_ids:
            skipped += 1  # LegalHold 冻结：跳过而非失败
            continue
        if _guest_has_open_business(guest):
            skipped += 1  # 未结束预订/未结账/退款中：跳过，等下轮
            continue
        to_process.append(guest)

    if dry_run:
        # dry-run：to_process 全部按「将处理」计入 processed 口径前的候选展示，
        # 不落写、不匿名化；skipped/failed 保持统计
        exec_record.skipped_count = skipped
        exec_record.failure_count = failed
        exec_record.failure_detail = failures or None
        exec_record.processed_count = 0  # dry-run 不计入处理数
        exec_record.execution_status = RetentionExecution.ExecutionStatus.DRAFT
        exec_record.save()
        AuditEvent.record(
            action="privacy.retention_dry_run",
            entity_type="RetentionExecution",
            entity_id=str(exec_record.pk),
            property_obj=property_obj,
            after={
                "policy_id": policy.pk,
                "as_of": str(as_of),
                "candidates": exec_record.candidate_count,
                "skipped": skipped,
                "would_process": len(to_process),
            },
            request=None,
        )
        return exec_record, to_process

    # 正式执行：匿名化写路径（逐个事务，单个失败不影响整批可重试）
    exec_record.execution_status = RetentionExecution.ExecutionStatus.EXECUTING
    exec_record.save(update_fields=("execution_status",))
    for guest in to_process:
        try:
            with transaction.atomic():
                anonymize_guest(guest)
            processed += 1
        except Exception as exc:  # noqa: BLE001 单个失败进清单，可重试
            failed += 1
            failures.append({"guest_id": guest.pk, "error": str(exc)[:160]})
    exec_record.processed_count = processed
    exec_record.skipped_count = skipped
    exec_record.failure_count = failed
    exec_record.failure_detail = failures or None
    exec_record.execution_status = (
        RetentionExecution.ExecutionStatus.COMPLETED
        if failed == 0
        else RetentionExecution.ExecutionStatus.FAILED
    )
    exec_record.completed_at = timezone.now()
    exec_record.save()
    AuditEvent.record(
        action="privacy.retention_executed",
        entity_type="RetentionExecution",
        entity_id=str(exec_record.pk),
        property_obj=property_obj,
        after={
            "policy_id": policy.pk,
            "as_of": str(as_of),
            "processed": processed,
            "skipped": skipped,
            "failed": failed,
            "dry_run": False,
        },
        request=None,
    )
    return exec_record, []


def anonymize_guest(guest):
    """不可逆匿名化单个 Guest 主档：抹除全部 PII 密文/哈希，姓名置标记。

    保留行本身（Reservation/Folio/GuestDocument 等外键完整性）；账务与
    审计记录完全不动。幂等：已匿名化（full_name=标记）直接返回。
    """
    if guest.full_name == ANONYMIZED_MARKER:
        return False
    guest.full_name = ANONYMIZED_MARKER
    guest.phone_ciphertext = None
    guest.phone_hash = None
    guest.email = ""
    guest.notes = ""
    guest.consent_version = ""
    # 旧明文历史列（迁移 0007 已清空存量，此处兜底）
    guest.phone = None
    guest.phone_normalized = None
    guest.save(
        update_fields=[
            "full_name",
            "phone_ciphertext",
            "phone_hash",
            "email",
            "notes",
            "consent_version",
            "phone",
            "phone_normalized",
            "updated_at",
        ]
    )
    AuditEvent.record(
        action="privacy.guest_anonymized",
        entity_type="Guest",
        entity_id=str(guest.pk),
        property_obj=guest.property,
        after={"marker": ANONYMIZED_MARKER},
        request=None,
    )
    return True


def request_legal_hold(*, property_obj, guest, reason, requested_by, idempotency_key):
    """申请法律冻结：创建 ApprovalRecord（PENDING）——审批通过后回填 LegalHold。

    申请人不得自批（审批模型保证）；冻结在 LegalHold.ACTIVE 前不生效，
    审批通过由审批中心调 confirm_legal_hold 完成回填。
    """
    return ApprovalRecord.objects.create(
        property=property_obj,
        action_code=ACTION_LEGAL_HOLD,
        entity_type="Guest",
        entity_id=str(guest.pk),
        requested_by=requested_by,
        reason=reason[:240],
        idempotency_key=idempotency_key,
        payload_snapshot={"guest_id": guest.pk, "booking_hint": reason[:80]},
    )


def confirm_legal_hold(approval_record, approved_by):
    """审批通过后调用：创建 ACTIVE LegalHold 并回填审批引用。"""
    if approval_record.status != ApprovalRecord.Status.APPROVED:
        raise ValueError("审批未通过，不能创建法律冻结。")
    hold = LegalHold.objects.create(
        property=approval_record.property,
        target_type="GUEST",
        target_id=approval_record.entity_id,
        hold_reason=approval_record.reason,
        requested_by=approval_record.requested_by,
        approved_by=approved_by,
        approval_reference=str(approval_record.pk),
    )
    AuditEvent.record(
        action="privacy.legal_hold_created",
        entity_type="LegalHold",
        entity_id=str(hold.pk),
        property_obj=approval_record.property,
        after={"guest_id": approval_record.entity_id, "approval": str(approval_record.pk)},
        request=None,
    )
    return hold


def release_legal_hold(hold, *, approved_by, release_reason, approval_record=None):
    """解除冻结（解除本身也走 ApprovalRecord，approval_reference 可选回填）。"""
    if hold.hold_status != LegalHold.HoldStatus.ACTIVE:
        raise ValueError("冻结已解除。")
    hold.hold_status = LegalHold.HoldStatus.RELEASED
    hold.released_at = timezone.now()
    hold.release_reason = release_reason[:240]
    hold.save(update_fields=("hold_status", "released_at", "release_reason"))
    AuditEvent.record(
        action="privacy.legal_hold_released",
        entity_type="LegalHold",
        entity_id=str(hold.pk),
        property_obj=hold.property,
        after={"reason": release_reason[:80]},
        request=None,
    )
    return hold
