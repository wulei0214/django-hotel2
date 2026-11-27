"""PMS2 核心服务：幂等记录清理（P-08）+ 受控导出凭证签发（MC-008/MC-009）。

纪律：扫描优先、dry-run 默认可审计、绝不误删。
- 仅清理 expires_at 已明确过期且超过保留缓冲（默认 1 天）的记录；
- 仍在 TTL 内（expires_at 未到）的记录不清理；
- expires_at 为空的记录仅报告、不删除（交人工复核，避免误删仍在使用的幂等键）；
- dry_run=True（默认）只统计与审计，不落任何删除；仅显式 dry_run=False 才删除。

导出凭证：签发是唯一写入口 issue_export_credential——token 随机不可枚举、
15 分钟下载有效期、只登记「谁/何时/何用途/何口径」，绝不缓存业务事实。
"""

import secrets
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from .models import AuditEvent, ExportCredential, IdempotencyRecord

# MC-008/MC-009 裁定口径：下载有效期 15 分钟（全仓唯一常量）。
EXPORT_CREDENTIAL_TTL_SECONDS = 15 * 60


def cleanup_idempotency_records(
    *, older_than_days=1, dry_run=True, property_obj=None, batch_size=500
):
    """扫描并（apply 时）清理过期 IdempotencyRecord。

    返回审计摘要 dict：scanned / by_status / null_expiry_count /
    would_delete / deleted / applied。

    P-08：扫描优先、不误删。dry_run=True（默认）时只统计与审计，不产生删除；
    仅当调用方显式 dry_run=False 才执行删除。
    """
    if older_than_days < 0:
        raise ValueError("older_than_days 必须为非负整数。")
    if batch_size < 1:
        raise ValueError("batch_size 必须 >= 1。")

    now = timezone.now()
    cutoff = now - timedelta(days=older_than_days)

    qs = IdempotencyRecord.objects.all()
    if property_obj is not None:
        qs = qs.filter(property=property_obj)

    # 过期且超过保留缓冲的候选（expires_at 明确、已过期；null expires_at 自然被排除）
    candidates = qs.filter(expires_at__lt=cutoff)
    status_counts = (
        candidates.values("status").annotate(cnt=Count("id")).values_list("status", "cnt")
    )
    by_status = dict(status_counts)
    null_expiry_count = qs.filter(expires_at__isnull=True).count()
    would_delete = candidates.count()

    deleted = 0
    if not dry_run:
        # 分批删除，避免大事务长锁；每批按主键精确命中候选集。
        while True:
            batch = list(candidates.values_list("pk", flat=True)[:batch_size])
            if not batch:
                break
            n, _ = IdempotencyRecord.objects.filter(pk__in=batch).delete()
            deleted += n

    AuditEvent.record(
        action="core.idempotency_cleanup",
        entity_type="IdempotencyRecord",
        entity_id="bulk",
        property_obj=property_obj,
        reason="P-08 幂等记录清理" + ("（dry-run，未删除）" if dry_run else "（已删除过期记录）"),
        after={
            "older_than_days": older_than_days,
            "applied": (not dry_run),
            "would_delete": would_delete,
            "deleted": deleted,
            "by_status": by_status,
            "null_expiry_count": null_expiry_count,
        },
    )

    return {
        "scanned": would_delete + null_expiry_count,
        "by_status": by_status,
        "null_expiry_count": null_expiry_count,
        "would_delete": would_delete,
        "deleted": deleted,
        "applied": (not dry_run),
    }


def issue_export_credential(
    *, property_obj, kind, report_code, params, columns, row_count, reason, created_by
):
    """签发受控导出凭证（MC-008/MC-009 唯一写入口）。

    历史实现里审计导出（audit_governance）与报表导出（report_export）各自复制
    了一份「token 生成 + ExportCredential.objects.create」写块，15 分钟 TTL
    常量也各自定义（2026-11-28 结构审查 P2：凭据签发散落两个 API 适配层）。
    两条路径现在都调用本函数；模型 docstring 的职责边界为强制口径——凭证只
    登记「谁在何时、以何用途、按什么口径导出过」，绝不缓存业务事实。
    """
    return ExportCredential.objects.create(
        property=property_obj,
        kind=kind,
        report_code=report_code,
        token=secrets.token_urlsafe(32),
        params=params,
        columns=list(columns),
        row_count=row_count,
        reason=reason,
        created_by=created_by,
        expires_at=timezone.now() + timedelta(seconds=EXPORT_CREDENTIAL_TTL_SECONDS),
    )
