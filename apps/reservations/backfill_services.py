"""A8：历史快照回填（Dry-run / 幂等 / 审计 / 可回滚）。

背景：ReservationRoom.early_departure_snapshot 由迁移 0008 新增，历史
legacy 明细（迁移前创建）该列为 NULL。系统对缺失快照按 NONE 兜底（WB-098
legacy 分支），但为提升数据完整性与可审计性，提供治理回填能力：

* backfill_early_departure_snapshots：为 early_departure_snapshot 为 NULL 的
  明细补快照（复用 front_desk_services.snapshot_early_departure_policy 的同一
  查询口径，回填行标记 backfilled=True 便于溯源）。
* clear_backfilled_early_departure_snapshots：回滚——仅清除 backfilled 标记的
  快照、恢复 NULL（NONE 兜底），不影响任何确认时真实写入的快照。

安全纪律（与 P-08 一致）：默认 dry_run=True 不写库；apply 才落库；幂等
（已有快照跳过）；每次回填按预订记 AuditEvent；回滚仅作用于 backfilled 标记行。
"""

from apps.core.models import AuditEvent

from .front_desk_services import snapshot_early_departure_policy
from .models import Reservation, ReservationRoom


def backfill_early_departure_snapshots(
    *,
    dry_run=True,
    property_obj=None,
    rate_plan_id=None,
    batch_size=500,
):
    """为缺 early_departure_snapshot 的 ReservationRoom 补快照。

    参数：
      dry_run：True（默认）仅扫描统计，不写库、不审计；False 实际回填。
      property_obj：仅处理该物业；省略处理全部。
      rate_plan_id：仅处理该房价计划的明细；省略处理全部。
      batch_size：当前仅作分批语义占位，逐预订处理本就不长事务。

    返回：{scanned_reservations, would_backfill, backfilled, by_policy_type, applied}
    """
    if batch_size < 1:
        raise ValueError("batch_size 必须 >= 1。")
    qs = ReservationRoom.objects.filter(early_departure_snapshot__isnull=True)
    if property_obj is not None:
        qs = qs.filter(reservation__property=property_obj)
    if rate_plan_id is not None:
        qs = qs.filter(rate_plan_id=rate_plan_id)
    reservation_ids = list(qs.values_list("reservation_id", flat=True).distinct())

    scanned = len(reservation_ids)
    would_backfill = 0
    backfilled = 0
    by_policy_type: dict[str, int] = {}
    for rid in reservation_ids:
        reservation = Reservation.objects.filter(pk=rid).first()
        if reservation is None:
            continue
        snapped = snapshot_early_departure_policy(
            reservation,
            backfilled=True,
            dry_run=dry_run,
            audit_action="reservation.early_departure_policy_backfilled",
            audit_reason="A8 历史快照回填（Dry-run/回滚安全，backfilled 标记）",
        )
        would_backfill += len(snapped)
        if not dry_run:
            backfilled += len(snapped)
        for line in snapped:
            pt = line.early_departure_snapshot["policy_type"]
            by_policy_type[pt] = by_policy_type.get(pt, 0) + 1
    return {
        "scanned_reservations": scanned,
        "would_backfill": would_backfill,
        "backfilled": backfilled,
        "by_policy_type": by_policy_type,
        "applied": (not dry_run),
    }


def clear_backfilled_early_departure_snapshots(
    *,
    dry_run=True,
    property_obj=None,
    rate_plan_id=None,
    batch_size=500,
):
    """A8 回滚：清除 backfilled 标记的快照，恢复 NULL（NONE 兜底）。

    仅回滚 backfilled 标记的快照，绝不触碰确认时真实写入的快照。返回
    {affected, cleared, applied}。

    注：JSONField __contains 在部分 SQLite 后端（无 JSON1 扩展）不被支持，
    故改为先取非 NULL 快照行、再在 Python 侧按 backfilled 标记筛选，保证
    MySQL / SQLite 双库兼容。
    """
    if batch_size < 1:
        raise ValueError("batch_size 必须 >= 1。")
    qs = ReservationRoom.objects.filter(early_departure_snapshot__isnull=False)
    if property_obj is not None:
        qs = qs.filter(reservation__property=property_obj)
    if rate_plan_id is not None:
        qs = qs.filter(rate_plan_id=rate_plan_id)
    candidates = [
        line
        for line in qs
        if isinstance(line.early_departure_snapshot, dict)
        and line.early_departure_snapshot.get("backfilled")
    ]
    affected = len(candidates)
    cleared = 0
    if not dry_run:
        for line in candidates:
            line.early_departure_snapshot = None
            line.save(update_fields=("early_departure_snapshot",))
            cleared += 1
        AuditEvent.record(
            action="reservation.early_departure_policy_backfill_cleared",
            entity_type="ReservationRoom",
            entity_id="bulk",
            property_obj=property_obj,
            after={"cleared": cleared},
            reason="A8 历史快照回填回滚：清除 backfilled 标记快照，恢复 NONE 兜底",
        )
    return {
        "affected": affected,
        "cleared": cleared,
        "applied": (not dry_run),
    }
