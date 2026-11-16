"""迁移批次授权闭环（迁移方案 §5.2：登记 PENDING → 批准 ACTIVE → 关闭 CLOSED）。

主体与职责分离（SoD）：
* 登记 = 迁移负责人（可登记拟执行人）；
* 批准 = 项目负责人（批准人写入 ``approved_by``，且必须 ≠ 执行人，DB 约束兜底）；
* 关闭 = 负责人（ACTIVE/PENDING → CLOSED 终态，不可重新激活）。

命令层（``manage_legacy_migration_batch``）只做参数解析与输出，全部校验在此，
便于单测直接覆盖，不依赖 CLI 交互。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import LegacyMigrationBatch
from apps.core.models import AuditEvent, Property

ALLOWED_SOURCE_MODELS = ("Folio", "FinanceRecord")


class LegacyBatchError(Exception):
    """批次登记/批准/关闭被拒绝（结构或状态不合法）。"""


def _record(
    action: str,
    batch: LegacyMigrationBatch,
    *,
    actor: Any = None,
    after: dict[str, Any] | None = None,
    reason: str = "",
) -> None:
    AuditEvent.record(
        action=action,
        entity_type="LegacyMigrationBatch",
        entity_id=str(batch.pk),
        actor=actor,
        property_obj=batch.scope_property,
        after=after or {},
        reason=reason[:240],
    )


def _validate_range(record_range: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(record_range, dict) or not record_range:
        raise LegacyBatchError("授权记录范围不能为空：须提供 pk_min/pk_max 或 pks 清单")
    pks = record_range.get("pks")
    if pks is not None:
        if not isinstance(pks, list) or not pks:
            raise LegacyBatchError("pks 必须是非空数组")
        return {"pks": [str(item) for item in pks]}
    lower = record_range.get("pk_min")
    upper = record_range.get("pk_max")
    try:
        lower_int, upper_int = int(str(lower)), int(str(upper))
    except (TypeError, ValueError) as exc:
        raise LegacyBatchError("pk_min/pk_max 必须是整数") from exc
    if lower_int > upper_int:
        raise LegacyBatchError("pk_min 不能大于 pk_max")
    return {"pk_min": lower_int, "pk_max": upper_int}


def register_batch(
    *,
    batch_uid: str,
    property_obj: Property,
    source_models: Iterable[str],
    record_range: dict[str, Any],
    executed_by: Any,
    approval_basis: str = "",
) -> LegacyMigrationBatch:
    """登记 PENDING 批次：写审批依据、物业、来源模型、记录范围与拟执行人。"""
    uid = str(batch_uid or "").strip()
    if not uid or len(uid) > 64:
        raise LegacyBatchError("批次号必须为 1 至 64 个字符")
    if executed_by is None:
        raise LegacyBatchError("必须登记拟执行人（executed_by）")
    models = [str(item).strip() for item in source_models if str(item).strip()]
    if not models:
        raise LegacyBatchError("授权来源模型不能为空")
    unknown = sorted(set(models) - set(ALLOWED_SOURCE_MODELS))
    if unknown:
        raise LegacyBatchError(
            f"不支持的来源模型：{', '.join(unknown)}；可选：{', '.join(ALLOWED_SOURCE_MODELS)}"
        )
    scope = _validate_range(record_range)
    try:
        with transaction.atomic():
            batch = LegacyMigrationBatch.objects.create(
                batch_uid=uid,
                scope_property=property_obj,
                scope_source_models=models,
                scope_record_range=scope,
                executed_by=executed_by,
                approval_basis=str(approval_basis or "")[:240],
                status=LegacyMigrationBatch.Status.PENDING,
            )
    except IntegrityError as exc:
        raise LegacyBatchError(f"批次号 {uid} 已登记，禁止重复登记") from exc
    _record(
        "billing.legacy_batch_register",
        batch,
        actor=executed_by,
        after={
            "batch_uid": uid,
            "source_models": models,
            "record_range": scope,
            "executed_by_id": executed_by.pk,
        },
        reason=approval_basis,
    )
    return batch


def approve_batch(
    *, batch: LegacyMigrationBatch, approved_by: Any, basis: str = ""
) -> LegacyMigrationBatch:
    """项目负责人批准：PENDING → ACTIVE，并强制 SoD（批准人 ≠ 执行人）。"""
    if batch.status != LegacyMigrationBatch.Status.PENDING:
        raise LegacyBatchError(f"批次状态为 {batch.status}，只有 PENDING 可批准")
    if approved_by is None:
        raise LegacyBatchError("必须记录批准人")
    if batch.executed_by_id == getattr(approved_by, "pk", None):
        raise LegacyBatchError("批准人与执行人不得为同一账号（职责分离）")
    batch.status = LegacyMigrationBatch.Status.ACTIVE
    batch.approved_by = approved_by
    batch.approved_at = timezone.now()
    if basis:
        batch.approval_basis = str(basis)[:240]
    batch.save()
    _record(
        "billing.legacy_batch_approve",
        batch,
        actor=approved_by,
        after={"batch_uid": batch.batch_uid, "approved_by_id": batch.approved_by_id},
        reason=batch.approval_basis,
    )
    return batch


def reject_batch(
    *, batch: LegacyMigrationBatch, approved_by: Any, reason: str
) -> LegacyMigrationBatch:
    """驳回：PENDING → REJECTED（终态），原因必填。"""
    if batch.status != LegacyMigrationBatch.Status.PENDING:
        raise LegacyBatchError(f"批次状态为 {batch.status}，只有 PENDING 可驳回")
    text = str(reason or "").strip()
    if not text:
        raise LegacyBatchError("驳回必须填写原因")
    batch.status = LegacyMigrationBatch.Status.REJECTED
    batch.approved_by = approved_by
    batch.approved_at = timezone.now()
    batch.save()
    _record(
        "billing.legacy_batch_reject",
        batch,
        actor=approved_by,
        after={"batch_uid": batch.batch_uid},
        reason=text,
    )
    return batch


def close_batch(*, batch: LegacyMigrationBatch, actor: Any = None) -> LegacyMigrationBatch:
    """关闭：PENDING/ACTIVE → CLOSED（终态，不可重新激活）。"""
    if batch.status not in (
        LegacyMigrationBatch.Status.PENDING,
        LegacyMigrationBatch.Status.ACTIVE,
    ):
        raise LegacyBatchError(f"批次状态为 {batch.status}，不可关闭")
    batch.status = LegacyMigrationBatch.Status.CLOSED
    batch.closed_at = timezone.now()
    try:
        batch.save()
    except ValidationError as exc:  # pragma: no cover - 状态机守卫兜底
        raise LegacyBatchError(str(exc)) from exc
    _record(
        "billing.legacy_batch_close",
        batch,
        actor=actor,
        after={"batch_uid": batch.batch_uid},
    )
    return batch
