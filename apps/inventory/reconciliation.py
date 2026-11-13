"""D4 库存差异自动扫描与人工审批修复（INV-P0）。

依据 docs/PMS2房态库存中心P0P1专项整改需求_2026-11-09.md：
- 系统自动发现差异并生成报告（不自动覆盖库存权威计数）。
- 差异报告保存计算时间/版本/期望值/当前值/差额/关联订单/Hold。
- 修复申请需原因+证据+影响范围；审批人与申请人分离。
- 执行前重新计算并检查版本；数据已变化则拒绝旧方案。
- 修复使用唯一服务+事务+行锁+审计；修复后再次对账。
"""

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.inventory.models import (
    InventoryDiscrepancy,
    InventoryReconciliationRun,
    RoomType,
    RoomTypeInventory,
)
from apps.inventory.services import _property_today
from apps.reservations.models import (
    InventoryHold,
    Reservation,
    ReservationRoom,
)

SOLD_RECOMPUTE_STATUSES = (
    Reservation.Status.CONFIRMED,
    Reservation.Status.CHECKED_IN,
    Reservation.Status.CHECKED_OUT,
)


def _recompute_inventory_days(*, property_obj, room_type, stay_dates):
    """RSV-01/02 + PERF-01：批量重算多个住宿日，返回 {date: (held, sold)}。

    此前扫描端对每个 (room_type × stay_date) 各发 2 条 COUNT，N+1 随日历
    行数线性放大。这里一次取回该房型窗口内的明细与冻结行，在内存中按日归集；
    执行端通过 ``_recompute_inventory_day`` 委托同一函数，口径天然一致。

    held 真值来自 ``InventoryHold`` 逐日逐单明细（RSV-01 已补齐
    hold/confirm/release 同事务写入）；sold 真值来自房间明细行，
    按明细行计数（多房每间 1）、排除 no-show 与提前离店已释放夜晚。
    """
    dates = list(stay_dates)
    if not dates:
        return {}
    lo, hi = min(dates), max(dates)
    held_counts = dict(
        InventoryHold.objects.filter(
            property=property_obj, room_type=room_type,
            stay_date__gte=lo, stay_date__lte=hi,
            status=InventoryHold.HoldStatus.HELD,
        ).values("stay_date").annotate(n=Count("pk"))
        .values_list("stay_date", "n"))
    lines = list(
        ReservationRoom.objects.filter(
            property=property_obj, room_type=room_type,
            check_in__lte=hi, check_out__gt=lo,
            reservation__status__in=SOLD_RECOMPUTE_STATUSES,
        ).exclude(no_show_record__isnull=False)
        .values_list("check_in", "check_out", "inventory_released_from"))
    sold_counts = {d: 0 for d in dates}
    for check_in, check_out, released_from in lines:
        for d in dates:
            if not (check_in <= d < check_out):
                continue
            # RSV-03：合同 check_out 保留原始值，改由明细级释放区间排除。
            if released_from is not None and released_from <= d:
                continue
            sold_counts[d] += 1
    return {d: (held_counts.get(d, 0), sold_counts[d]) for d in dates}


def _recompute_inventory_day(*, property_obj, room_type, stay_date):
    """扫描端与执行端共用的**唯一**单日重算入口。"""
    return _recompute_inventory_days(
        property_obj=property_obj, room_type=room_type,
        stay_dates=[stay_date])[stay_date]


def scan_inventory_discrepancies(*, property_obj, days_back=30, checked_by=None):
    """D4：扫描库存差异并生成报告（不自动覆盖库存权威计数）。

    重算口径：total_rooms 应等于 out_of_service + held + sold（数据库约束保证）。
    差异来源：预订 Hold 行数 vs 库存行 held 计数、确认占用行数 vs sold 计数。

    WB-022~026 修正：
      * 扫描窗口按物业时区取"今天"（此前用服务器本地日期，跨时区物业
        窗口漂移一天）；
      * sold 重算按 ReservationRoom 明细行计数（此前按 distinct 预订计数，
        多房预订 2 间同房型只算 1 间，sold 被低估）；
      * sold 重算排除已标记未到的明细行（未到释放库存，不得再占 sold）；
      * 差异判定口径修正：held/sold 不一致必须报（运算符优先级此前只约束
        total_rooms 一项）；total_rooms 按容量语义判定超卖（< 占用合计），
        不再与占用和做相等比较（所有未满房行误报）。
    """
    today = _property_today(property_obj)
    start = today - timezone.timedelta(days=days_back)
    end = today + timezone.timedelta(days=30)

    run = InventoryReconciliationRun.objects.create(
        property=property_obj, status=InventoryReconciliationRun.Status.COMPLETED,
        checked_by=checked_by)

    types = RoomType.objects.filter(property=property_obj, is_active=True)
    diff_count = 0
    scanned_dates = 0
    scanned_types = len(types)

    for rt in types:
        inventory_rows = list(RoomTypeInventory.objects.filter(
            property=property_obj, room_type=rt,
            stay_date__gte=start, stay_date__lt=end).order_by("stay_date"))
        date_set = {row.stay_date for row in inventory_rows}
        scanned_dates = max(scanned_dates, len(date_set))
        # PERF-01：每个房型一次批量重算整个窗口，替代逐行 2 条 COUNT。
        recomputed = _recompute_inventory_days(
            property_obj=property_obj, room_type=rt,
            stay_dates=sorted(date_set))

        for row in inventory_rows:
            # RSV-02：与执行端共用同一重算口径，杜绝两处手写漂移。
            actual_held, actual_sold = recomputed[row.stay_date]

            recomputed_oos = row.out_of_service
            recomputed_total = (recomputed_oos + actual_held + actual_sold)

            # WB-026：判定口径修正——①held/sold 与真值不一致必须报差异
            # （此前 `and recomputed_total > 0` 因运算符优先级只约束
            # total_rooms 一项）；②total_rooms 是物理容量（≥ 占用合计），
            # 重算占用超过容量即为超卖，报差异（此前"!= 重算和"把容量
            # 误当占用和，所有未满房行都会误报）。
            if (row.held != actual_held
                    or row.sold != actual_sold
                    or row.total_rooms < recomputed_total):
                diff = row.total_rooms - recomputed_total
                InventoryDiscrepancy.objects.create(
                    run=run, property=property_obj, room_type=rt,
                    stay_date=row.stay_date,
                    current_total=row.total_rooms,
                    recomputed_total=recomputed_total,
                    diff=diff,
                    held_count=actual_held, sold_count=actual_sold,
                    oos_count=recomputed_oos)
                diff_count += 1

    run.diff_count = diff_count
    run.scanned_dates = scanned_dates
    run.scanned_types = scanned_types
    run.save(update_fields=("diff_count", "scanned_dates", "scanned_types"))
    AuditEvent.record(
        action="inventory.reconciliation_scanned",
        entity_type="InventoryReconciliationRun", entity_id=run.pk,
        actor=checked_by, property_obj=property_obj,
        after={"diff_count": diff_count, "scanned_dates": scanned_dates},
        reason="D4 库存差异扫描")
    return run


@transaction.atomic
def request_inventory_fix(*, property_obj, discrepancy_id, fix_reason,
                          fix_evidence, requested_by):
    """D4：授权人员发起库存修复申请（不自动覆盖权威计数）。"""
    disc = InventoryDiscrepancy.objects.select_for_update().filter(
        pk=discrepancy_id, property=property_obj).first()
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.OPEN:
        raise ValueError("只有待处理的差异可以发起修复申请。")
    fix_reason = str(fix_reason or "").strip()
    if not fix_reason:
        raise ValueError("修复原因必填。")
    disc.status = InventoryDiscrepancy.Status.FIX_REQUESTED
    disc.fix_reason = fix_reason[:500]
    disc.fix_evidence = str(fix_evidence or "").strip()[:500]
    disc.requested_by = requested_by
    disc.requested_at = timezone.now()
    disc.save(update_fields=("status", "fix_reason", "fix_evidence",
                             "requested_by", "requested_at", "updated_at"))
    AuditEvent.record(
        action="inventory.fix_requested", entity_type="InventoryDiscrepancy",
        entity_id=disc.pk, actor=requested_by, property_obj=property_obj,
        after={"status": disc.status, "reason": fix_reason[:200]},
        reason="发起库存修复申请")
    return disc


@transaction.atomic
def approve_inventory_fix(*, property_obj, discrepancy_id, approved_by):
    """D4：独立审批；申请人不得自批。"""
    disc = InventoryDiscrepancy.objects.select_for_update().filter(
        pk=discrepancy_id, property=property_obj).first()
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.FIX_REQUESTED:
        raise ValueError("只有修复申请中的差异可以审批。")
    if disc.requested_by_id == approved_by.pk:
        raise ValueError("申请人不得审批自己的库存修复申请（职责分离）。")
    disc.status = InventoryDiscrepancy.Status.FIX_APPROVED
    disc.approved_by = approved_by
    disc.approved_at = timezone.now()
    disc.save(update_fields=("status", "approved_by", "approved_at", "updated_at"))
    AuditEvent.record(
        action="inventory.fix_approved", entity_type="InventoryDiscrepancy",
        entity_id=disc.pk, actor=approved_by, property_obj=property_obj,
        after={"status": disc.status},
        reason="审批库存修复申请")
    return disc


class _MissingHoldDetails(Exception):
    """执行端没有冻结明细，拒绝自动清零；审计须在原子块回滚后独立落库。"""

    def __init__(self, discrepancy_id, row_held):
        super().__init__(f"discrepancy={discrepancy_id} row_held={row_held}")
        self.discrepancy_id = discrepancy_id
        self.row_held = row_held


@transaction.atomic
def _execute_inventory_fix_atomic(*, property_obj, discrepancy_id, executed_by):
    """D4：执行已批准修复（事务内修正 held/sold；修复后立即再次核对）。"""
    disc = InventoryDiscrepancy.objects.select_for_update().filter(
        pk=discrepancy_id, property=property_obj).first()
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.FIX_APPROVED:
        raise ValueError("只有已批准的修复可以执行。")

    # RSV-02：执行前重新计算，且必须与扫描端使用完全相同的口径。
    actual_held, actual_sold = _recompute_inventory_day(
        property_obj=property_obj, room_type=disc.room_type,
        stay_date=disc.stay_date)

    # RSV-02：此前此处没有物业过滤，room_type 全局非唯一时会锁到/改写
    # 其他物业的库存行。按物业 + 主键固定顺序锁定。
    row = RoomTypeInventory.objects.select_for_update().filter(
        property=property_obj, room_type=disc.room_type,
        stay_date=disc.stay_date).order_by("pk").first()
    if row is None:
        raise ValueError("库存行不存在或不属于当前物业。")

    # RSV-01 防线：明细链路缺失时，actual_held 恒为 0。若据此执行
    # row.held = 0，会把库存服务真实维护的冻结清零，进而放开容量约束
    # 造成超卖，且原客支付回调时 confirm_inventory_for_reservation 会因
    # "Hold 已失效" 拒绝确认（钱收了房没了）。此处拒绝自动修复，保留
    # 权威计数并要求人工核对。
    if actual_held == 0 and row.held > 0:
        has_any_detail = InventoryHold.objects.filter(
            property=property_obj, room_type=disc.room_type,
            stay_date=disc.stay_date).exists()
        if not has_any_detail:
            # 不要在此直接写审计再抛错：本函数处于 @transaction.atomic 内，
            # 异常会回滚整块，拒绝证据会一起消失（第 28 轮专项故障注入发现）。
            # 由外层 execute_inventory_fix 在回滚后以独立事务补写审计。
            raise _MissingHoldDetails(disc.pk, row.held)

    if row.held == actual_held and row.sold == actual_sold:
        # 差异已被其他修复消除，直接关闭
        disc.status = InventoryDiscrepancy.Status.FIXED
        disc.executed_by = executed_by
        disc.fixed_at = timezone.now()
        disc.save(update_fields=("status", "executed_by", "fixed_at", "updated_at"))
        return disc

    row.held = actual_held
    row.sold = actual_sold
    row.version += 1
    row.save(update_fields=("held", "sold", "version", "updated_at"))

    disc.status = InventoryDiscrepancy.Status.FIXED
    disc.executed_by = executed_by
    disc.fixed_at = timezone.now()
    disc.save(update_fields=("status", "executed_by", "fixed_at", "updated_at"))

    # 修复后立即再次对账（held 与 sold 都用同一口径复核）
    post_held, post_sold = _recompute_inventory_day(
        property_obj=property_obj, room_type=disc.room_type,
        stay_date=disc.stay_date)
    if post_held != row.held or post_sold != row.sold:
        AuditEvent.record(
            action="inventory.fix_post_check_failed",
            entity_type="InventoryDiscrepancy", entity_id=disc.pk,
            actor=executed_by, property_obj=property_obj,
            after={"held": row.held, "sold": row.sold,
                   "post_held": post_held, "post_sold": post_sold},
            reason="修复后核对仍不一致，需升级处理")

    AuditEvent.record(
        action="inventory.fix_executed", entity_type="InventoryDiscrepancy",
        entity_id=disc.pk, actor=executed_by, property_obj=property_obj,
        after={"status": disc.status, "held": row.held, "sold": row.sold,
               "version": row.version},
        reason="D4 执行库存修复")
    return disc


def execute_inventory_fix(*, property_obj, discrepancy_id, executed_by):
    """D4 执行已批准库存修复（公开入口）。

    修复本体在单个原子块内；若因「无冻结明细」拒绝，内层原子块整体回滚
    （差异保持 FIX_APPROVED、权威 held 不变），此时必须在**原子块之外**
    独立写拒绝审计再抛出业务错误，否则审计会随回滚一起消失，人工核对
    将失去可追溯证据（第 28 轮专项故障注入发现的真实缺陷）。
    """
    try:
        return _execute_inventory_fix_atomic(
            property_obj=property_obj, discrepancy_id=discrepancy_id,
            executed_by=executed_by)
    except _MissingHoldDetails as exc:
        AuditEvent.record(
            action="inventory.fix_refused_missing_hold_details",
            entity_type="InventoryDiscrepancy", entity_id=exc.discrepancy_id,
            actor=executed_by, property_obj=property_obj,
            after={"row_held": exc.row_held, "recomputed_held": 0},
            reason="无冻结明细，禁止清零真实 held（RSV-01），转人工核对")
        raise ValueError(
            "该住宿日无任何库存冻结明细，无法独立重建 held；为避免"
            "清零真实冻结，已拒绝自动修复，请人工核对（RSV-01）。") from exc


@transaction.atomic
def dismiss_discrepancy(*, property_obj, discrepancy_id, dismissed_by,
                        reason=""):
    """关闭差异（无差异/误报）。"""
    disc = InventoryDiscrepancy.objects.select_for_update().filter(
        pk=discrepancy_id, property=property_obj).first()
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.OPEN:
        raise ValueError("只有待处理的差异可以关闭。")
    disc.status = InventoryDiscrepancy.Status.DISMISSED
    disc.save(update_fields=("status", "updated_at"))
    AuditEvent.record(
        action="inventory.disc_dismissed", entity_type="InventoryDiscrepancy",
        entity_id=disc.pk, actor=dismissed_by, property_obj=property_obj,
        after={"status": disc.status},
        reason=str(reason or "")[:240])
    return disc
