"""归档表只读查询通道（归档门禁 §4.1：敏感字段权限、脱敏、访问审计）。

职责边界：
1. **只读**：只对 ``legacy_*_archive`` 与 ``LegacyArchiveRun`` 发 SELECT，不写归档表；
2. **权限**：通道只服务两类主体（§4.1 D-11，不新造角色）——平台超管与持
   ``hotelapp.finance_access`` 的财务负责人；本模块只做数据侧，权限判定由
   调用方（API 视图 / Admin）先行，再经 :func:`authorize_batch` 复核物业归属；
3. **脱敏**：身份证/电话完整值不出现在任何输出——联系电话一律掩码；
4. **审计**：每次查询登记 who/when/what（``billing.legacy_archive_query``）。

物业隔离口径（不猜测，符合裁定 3）：
归档行本身**不含物业列**（源表快照未自动推定物业归属），故物业隔离由归档批次
的**登记来源**判定——``LegacyArchiveRun.batch_uid`` 对应已登记的
``LegacyMigrationBatch`` 时，按其 ``scope_property`` 隔离；找不到对应迁移批次的
归档批次属"未登记来源"，只有平台超管可查。这样既不自动推定物业，也不给出
跨物业读取口子。
"""

from __future__ import annotations

from typing import Any

from apps.billing import models as billing_models
from apps.billing.legacy_archive import ARCHIVE_SPECS, ArchiveSpec, get_spec_by_table
from apps.core.models import AuditEvent

# 对外表键 → 源表名（URL 友好，避免在 URL 里暴露裸表名）。
TABLE_KEYS: dict[str, str] = {
    "folios": "folio",
    "finance-records": "finance_record",
    "night-audits": "night_audit",
    "accommodations": "accommodation",
}

# 需脱敏的归档列（§4.1 第 1 条：电话/身份证完整值不得出现在任何输出）。
MASKED_FIELDS: dict[str, tuple[str, ...]] = {
    "legacy_accommodation_archive": ("contact_phone",),
}


class LegacyArchiveQueryError(Exception):
    """归档查询被拒绝（未知表键、批次不存在、权限不足）。"""


class LegacyArchiveAccessDenied(LegacyArchiveQueryError):
    """无权查看该归档批次（跨物业或未登记来源的非超管访问）。"""


def mask_phone(value: Any) -> Any:
    """电话脱敏：≥8 位数字取前 3 后 4，不足 8 位一律"已隐藏"（不还原短号）。"""
    if value in (None, ""):
        return value
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        return "已隐藏"
    return f"{digits[:3]}****{digits[-4:]}"


def resolve_spec(table_key: str) -> ArchiveSpec:
    source_table = TABLE_KEYS.get(str(table_key or "").strip())
    if source_table is None:
        raise LegacyArchiveQueryError(
            f"未知归档表 {table_key!r}；可选：{', '.join(sorted(TABLE_KEYS))}"
        )
    return get_spec_by_table(source_table)


def batch_property(batch_uid: str) -> Any:
    """归档批次关联的物业（经已登记的迁移批次）；未登记返回 None。"""
    batch = billing_models.LegacyMigrationBatch.objects.filter(
        batch_uid=str(batch_uid or "").strip()
    ).first()
    return batch.scope_property if batch is not None else None


def authorize_batch(*, batch_uid: str, actor: Any, is_superuser: bool) -> Any:
    """复核批次可见性：超管放行；非超管必须在批次所属物业有有效任职。

    返回批次所属物业（未登记来源时为 None）。拒绝抛
    :class:`LegacyArchiveAccessDenied`（调用方按 404 处理，不泄露存在性）。
    """
    from apps.identity.authorization import AuthorizationService

    uid = str(batch_uid or "").strip()
    if not uid:
        raise LegacyArchiveQueryError("必须指定归档批次")
    if not billing_models.LegacyArchiveRun.objects.filter(batch_uid=uid).exists():
        raise LegacyArchiveQueryError(f"归档批次不存在：{uid}")
    prop = batch_property(uid)
    if is_superuser:
        return prop
    if prop is None:
        raise LegacyArchiveAccessDenied("归档批次未登记迁移来源，仅平台管理员可查")
    if AuthorizationService.get_active_membership(actor, prop) is None:
        raise LegacyArchiveAccessDenied("无该批次所属物业的有效任职")
    if not AuthorizationService.has_permission(actor, prop, "hotelapp.finance_access"):
        raise LegacyArchiveAccessDenied("缺少 hotelapp.finance_access 权限")
    return prop


def _mask_columns(spec: ArchiveSpec) -> tuple[str, ...]:
    return MASKED_FIELDS.get(spec.archive_table, ())


def list_batches(*, property_obj: Any = None, is_superuser: bool = False) -> list[dict[str, Any]]:
    """列出归档批次（非超管必须提供物业上下文，且仅见该物业的已登记批次）。"""
    if not is_superuser and property_obj is None:
        raise LegacyArchiveAccessDenied("缺少物业上下文，拒绝列出归档批次")
    runs = billing_models.LegacyArchiveRun.objects.all().order_by("batch_uid", "source_table")
    items: list[dict[str, Any]] = []
    for run in runs:
        prop = batch_property(run.batch_uid)
        if not is_superuser and (
            prop is None or (property_obj is not None and prop.pk != property_obj.pk)
        ):
            continue
        items.append(
            {
                "batch_uid": run.batch_uid,
                "source_table": run.source_table,
                "archive_table": run.archive_table,
                "row_count": run.row_count,
                "min_pk": run.min_pk,
                "max_pk": run.max_pk,
                "table_digest": run.table_digest,
                "algorithm_version": run.algorithm_version,
                "computed_at": run.computed_at.isoformat() if run.computed_at else None,
                "operator": run.operator,
                "property_code": prop.code if prop is not None else None,
            }
        )
    return items


def list_rows(
    *,
    batch_uid: str,
    table_key: str,
    actor: Any,
    property_obj: Any = None,
    is_superuser: bool = False,
    page: int = 1,
    page_size: int = 100,
    pk_min: int | None = None,
    pk_max: int | None = None,
    request: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """只读查询某批次某表的归档行（脱敏 + 审计），返回 ``(rows, pagination)``。

    分页口径与 ``apps.core.api_contract.paginate`` 一致（page/page_size/total/
    has_next/has_previous），页大小上限 500；行内容按归档列序输出，电话列已掩码。
    """
    spec = resolve_spec(table_key)
    batch_prop = authorize_batch(batch_uid=batch_uid, actor=actor, is_superuser=is_superuser)
    if not is_superuser:
        if property_obj is None or batch_prop is None or batch_prop.pk != property_obj.pk:
            raise LegacyArchiveAccessDenied("批次物业与当前物业上下文不一致")
    run = billing_models.LegacyArchiveRun.objects.filter(
        batch_uid=batch_uid, source_table=spec.source_table
    ).first()
    if run is None:
        raise LegacyArchiveQueryError(
            f"批次 {batch_uid} 未归档源表 {spec.source_table}（先执行 archive_legacy_finance）"
        )
    columns = spec.columns
    queryset = spec.archive_model.objects.filter(archive_batch=batch_uid)
    if not is_superuser:
        # FIN-02：逐记录物业快照才是 D6 物业级过滤依据；NULL/未确认归属仅超管可查。
        queryset = queryset.filter(property_id=property_obj.pk)
    queryset = queryset.order_by("id")
    if pk_min is not None:
        queryset = queryset.filter(id__gte=pk_min)
    if pk_max is not None:
        queryset = queryset.filter(id__lte=pk_max)
    total = queryset.count()
    size = max(1, min(int(page_size or 100), 500))
    current = max(1, int(page or 1))
    start = (current - 1) * size
    raw_rows = list(queryset.values(*columns)[start : start + size])

    masked = _mask_columns(spec)
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        item = dict(row)
        for field in masked:
            if field in item:
                item[field] = mask_phone(item[field])
        rows.append(item)

    AuditEvent.record(
        action="billing.legacy_archive_query",
        entity_type="LegacyArchiveRun",
        entity_id=f"{batch_uid}/{spec.source_table}",
        actor=actor,
        property_obj=property_obj,
        after={
            "batch_uid": batch_uid,
            "source_table": spec.source_table,
            "filters": {"pk_min": pk_min, "pk_max": pk_max},
            "page": current,
            "page_size": size,
            "returned_rows": len(rows),
            "total": total,
        },
        request=request,
    )
    pagination = {
        "page": current,
        "page_size": size,
        "total": total,
        "has_next": start + size < total,
        "has_previous": current > 1,
    }
    return rows, pagination


def table_keys() -> list[str]:
    """可用表键（供页面/契约自检）。"""
    return sorted(TABLE_KEYS)


def specs_for_admin() -> list[ArchiveSpec]:
    """Admin 注册顺序用：四张归档表规格。"""
    return list(ARCHIVE_SPECS)
