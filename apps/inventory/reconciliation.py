"""D4 库存差异自动扫描与人工审批修复（INV-P0）。

依据 docs/PMS2房态库存中心P0P1专项整改需求_2026-11-09.md：
- 系统自动发现差异并生成报告（不自动覆盖库存权威计数）。
- 差异报告保存计算时间/版本/期望值/当前值/差额/关联订单/Hold。
- 修复申请需原因+证据+影响范围；审批人与申请人分离。
- 执行前重新计算并检查版本；数据已变化则拒绝旧方案。
- 修复使用唯一服务+事务+行锁+审计；修复后再次对账。

M4-3（架构治理模块④批次，2026-11-19）补充：
- 扫描调度幂等：同一（物业, 房型, 住宿日）只保留一条 OPEN 报告，重算值
  变化时原位更新，重复调度不堆积重复差异行/待办（待办经 MC-007
  inventory.discrepancy_open 任务源按差异主键去重）。
- review_discrepancy：待办完成入口调用的唯一复核服务——按同一重算口径
  复核，差异已消除/误报才经 dismiss_discrepancy 关闭；差异仍在时拒绝
  闭环并指向唯一修复工作流（申请→审批→执行），不做改状态字段式完成。

M4-4（架构治理模块④批次，2026-11-19）补充（D4-2 B / D2-B 排房冲突日检测）：
- 排房冲突日判定式（``_room_occupancy_counts`` 为唯一实现，扫描端与复核端
  共用口径）：某（物理房, 住宿日）存在 ≥2 条活跃排房明细即冲突日——
    N(房, 日) = |ReservationRoom : assigned_room=房 ∧ check_in ≤ 日 < check_out
      ∧ reservation.status ∈ (PENDING, CONFIRMED, CHECKED_IN)
      ∧ 无 NoShowRecord ∧ 提前离店释放日 ≤ 日 的夜晚不计|
  与事务内排房闸门（front_desk_services._check_room_assignment_conflict，
  WB-085）同款区间重叠判定的日粒度等价形式；No-Show/提前离店释放语义与
  同服务 sold 重算（RSV-03）一致，物理房可售容量=1 间/晚。
- 只报告不自动改派（负责人约束）：冲突记录进 RoomAssignmentConflict，
  待办经 MC-007 任务源 inventory.room_conflict_open 派生；修复只能人工
  执行（前台换房/取消多余明细），review_room_conflict 按同一判定式复核，
  冲突消除才关闭，永不改写排房明细。
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.inventory.models import (
    InventoryDiscrepancy,
    InventoryReconciliationRun,
    Room,
    RoomAssignmentConflict,
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

# D4-2：排房冲突判定的"活跃预订"口径——与事务内排房闸门
# front_desk_services.ACTIVE_RESERVATION_STATUSES（WB-085）同款；测试锁定两者一致。
ROOM_CONFLICT_ACTIVE_STATUSES = (
    Reservation.Status.PENDING,
    Reservation.Status.CONFIRMED,
    Reservation.Status.CHECKED_IN,
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
            property=property_obj,
            room_type=room_type,
            stay_date__gte=lo,
            stay_date__lte=hi,
            status=InventoryHold.HoldStatus.HELD,
        )
        .values("stay_date")
        .annotate(n=Count("pk"))
        .values_list("stay_date", "n")
    )
    lines = list(
        ReservationRoom.objects.filter(
            property=property_obj,
            room_type=room_type,
            check_in__lte=hi,
            check_out__gt=lo,
            reservation__status__in=SOLD_RECOMPUTE_STATUSES,
        )
        .exclude(no_show_record__isnull=False)
        .values_list("check_in", "check_out", "inventory_released_from")
    )
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
        property_obj=property_obj, room_type=room_type, stay_dates=[stay_date]
    )[stay_date]


def _room_occupancy_counts(*, property_obj, start, end):
    """D4-2 排房冲突日判定式的唯一实现（扫描端与复核端共用口径）。

    统计窗口 [start, end)（end 为开区间）内每个（物理房, 住宿日）的活跃
    排房明细计数，返回 ``{(room_id, stay_date): {"count": n, "line_ids": [...]}}``；
    调用方以 ``count >= 2`` 判定冲突日（物理房可售容量=1 间/晚）。

    判定口径（M4-4 设计，见模块 docstring 判定式）：
      * 只统计已排房明细（``assigned_room`` 非空）——未排房明细不占物理房；
      * 合同住宿区间半开覆盖 ``check_in ≤ 日 < check_out``——与 WB-085
        闸门 ``_check_room_assignment_conflict`` 的区间重叠判定同款；
      * 活跃预订状态 PENDING/CONFIRMED/CHECKED_IN——同 WB-085 闸门口径；
      * 排除带 NoShowRecord 的明细（未到释放，与 sold 重算口径一致）；
      * 排除提前离店已释放夜晚（RSV-03 ``inventory_released_from``，与
        sold 重算口径一致）——排房维度与库存维度使用同一释放语义。

    只读：不锁行、不写任何业务表，不阻塞正常预订链路（门禁项②）。
    """
    lines = (
        ReservationRoom.objects.filter(
            property=property_obj,
            assigned_room__isnull=False,
            check_in__lt=end,
            check_out__gt=start,
            reservation__status__in=ROOM_CONFLICT_ACTIVE_STATUSES,
        )
        .exclude(no_show_record__isnull=False)
        .values_list("pk", "assigned_room_id", "check_in", "check_out", "inventory_released_from")
    )
    occupancy: dict = {}
    one_day = timedelta(days=1)
    for line_id, room_id, check_in, check_out, released_from in lines:
        day = max(check_in, start)
        window_end = min(check_out, end)
        while day < window_end:
            if released_from is not None and released_from <= day:
                # RSV-03：提前离店已释放的夜晚不再占用物理房。
                day += one_day
                continue
            bucket = occupancy.setdefault((room_id, day), {"count": 0, "line_ids": []})
            bucket["count"] += 1
            bucket["line_ids"].append(line_id)
            day += one_day
    return occupancy


def _conflict_details_snapshot(line_ids):
    """冲突明细快照（确定性排序，供人工修复定位与原位更新比较）。"""
    rows = {
        row["pk"]: row
        for row in ReservationRoom.objects.filter(pk__in=set(line_ids)).values(
            "pk", "reservation__booking_no", "check_in", "check_out"
        )
    }
    return [
        {
            "line_id": line_id,
            "booking_no": rows[line_id]["reservation__booking_no"] or "",
            "check_in": rows[line_id]["check_in"].isoformat(),
            "check_out": rows[line_id]["check_out"].isoformat(),
        }
        for line_id in sorted(line_ids)
    ]


def _scan_room_assignment_conflicts(*, property_obj, run, start, end):
    """D4-2：排房冲突日检测（只读报告，并入对账运行）。

    幂等化与 M4-3 库存差异同款：同一（物业, 物理房, 住宿日）只保留一条
    OPEN 冲突报告；重复扫描命中已有 OPEN 行时占用计数/明细快照变化则
    原位更新（首报运行归属不变），无变化不写库；不重复建行。

    返回 (检出冲突日数, 新建冲突行数)。
    """
    occupancy = _room_occupancy_counts(property_obj=property_obj, start=start, end=end)
    conflicts = {key: bucket for key, bucket in occupancy.items() if bucket["count"] >= 2}
    if not conflicts:
        return 0, 0

    room_rows = {
        room.pk: room
        for room in Room.objects.filter(
            property=property_obj,
            pk__in={room_id for room_id, _ in conflicts},
        )
    }

    detected = 0
    created = 0
    for room_id, stay_date in sorted(conflicts, key=lambda item: (item[0], item[1])):
        # WB-085 闸门保证排房明细与物理房同物业；直接写库漂移产生的跨物业
        # 排房不在本检测口径内（属另一类主数据漂移，不在此报告）。
        room = room_rows.get(room_id)
        if room is None:
            continue
        detected += 1
        details = _conflict_details_snapshot(conflicts[(room_id, stay_date)]["line_ids"])
        # M4-4 调度幂等：同（物业, 物理房, 住宿日）只一条 OPEN 报告，原位更新。
        conflict = (
            RoomAssignmentConflict.objects.filter(  # type: ignore[has-type]
                property=property_obj,
                room_id=room_id,
                stay_date=stay_date,
                status=RoomAssignmentConflict.Status.OPEN,
            )
            .order_by("pk")
            .first()
        )
        if conflict is None:
            RoomAssignmentConflict.objects.create(  # type: ignore[has-type]
                run=run,
                property=property_obj,
                room=room,
                room_type=room.room_type,
                stay_date=stay_date,
                conflict_count=conflicts[(room_id, stay_date)]["count"],
                details=details,
            )
            created += 1
        elif conflict.conflict_count != conflicts[(room_id, stay_date)]["count"] or (
            conflict.details != details
        ):
            conflict.conflict_count = conflicts[(room_id, stay_date)]["count"]
            conflict.details = details
            conflict.save(update_fields=("conflict_count", "details", "updated_at"))
    return detected, created


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

    M4-4/D4-2：同一运行/窗口并入排房冲突日检测（``_scan_room_assignment_conflicts``，
    结果记 ``run.room_conflict_count``；只报告、不自动改派）。
    """
    today = _property_today(property_obj)
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=30)

    run = InventoryReconciliationRun.objects.create(  # type: ignore[has-type]
        property=property_obj,
        status=InventoryReconciliationRun.Status.COMPLETED,
        checked_by=checked_by,
    )

    types = RoomType.objects.filter(property=property_obj, is_active=True)
    diff_count = 0
    new_count = 0
    scanned_dates = 0
    scanned_types = len(types)

    for rt in types:
        inventory_rows = list(
            RoomTypeInventory.objects.filter(
                property=property_obj, room_type=rt, stay_date__gte=start, stay_date__lt=end
            ).order_by("stay_date")
        )
        date_set = {row.stay_date for row in inventory_rows}
        scanned_dates = max(scanned_dates, len(date_set))
        # PERF-01：每个房型一次批量重算整个窗口，替代逐行 2 条 COUNT。
        recomputed = _recompute_inventory_days(
            property_obj=property_obj, room_type=rt, stay_dates=sorted(date_set)
        )

        for row in inventory_rows:
            # RSV-02：与执行端共用同一重算口径，杜绝两处手写漂移。
            actual_held, actual_sold = recomputed[row.stay_date]

            recomputed_oos = row.out_of_service
            recomputed_total = recomputed_oos + actual_held + actual_sold

            # WB-026：判定口径修正——①held/sold 与真值不一致必须报差异
            # （此前 `and recomputed_total > 0` 因运算符优先级只约束
            # total_rooms 一项）；②total_rooms 是物理容量（≥ 占用合计），
            # 重算占用超过容量即为超卖，报差异（此前"!= 重算和"把容量
            # 误当占用和，所有未满房行都会误报）。
            if (
                row.held != actual_held
                or row.sold != actual_sold
                or row.total_rooms < recomputed_total
            ):
                diff = row.total_rooms - recomputed_total
                # M4-3/D4-4 调度幂等：同一（物业, 房型, 住宿日）只保留一条
                # OPEN 差异报告；重算值变化时原位更新（首报运行归属不变），
                # 重复调度不再堆积重复差异行与重复待办。
                disc = (
                    InventoryDiscrepancy.objects.filter(  # type: ignore[has-type]
                        property=property_obj,
                        room_type=rt,
                        stay_date=row.stay_date,
                        status=InventoryDiscrepancy.Status.OPEN,
                    )
                    .order_by("pk")
                    .first()
                )
                if disc is None:
                    InventoryDiscrepancy.objects.create(  # type: ignore[has-type]
                        run=run,
                        property=property_obj,
                        room_type=rt,
                        stay_date=row.stay_date,
                        current_total=row.total_rooms,
                        recomputed_total=recomputed_total,
                        diff=diff,
                        held_count=actual_held,
                        sold_count=actual_sold,
                        oos_count=recomputed_oos,
                    )
                    new_count += 1
                elif (
                    disc.current_total != row.total_rooms
                    or disc.recomputed_total != recomputed_total
                    or disc.held_count != actual_held
                    or disc.sold_count != actual_sold
                    or disc.oos_count != recomputed_oos
                ):
                    disc.current_total = row.total_rooms
                    disc.recomputed_total = recomputed_total
                    disc.diff = diff
                    disc.held_count = actual_held
                    disc.sold_count = actual_sold
                    disc.oos_count = recomputed_oos
                    disc.save(
                        update_fields=(
                            "current_total",
                            "recomputed_total",
                            "diff",
                            "held_count",
                            "sold_count",
                            "oos_count",
                            "updated_at",
                        )
                    )
                diff_count += 1

    # M4-4/D4-2：排房冲突日检测并入同一对账运行（同一扫描窗口；只报告、
    # 不自动改派；同判定式供 review_room_conflict 复核共用）。
    room_conflicts, new_room_conflicts = _scan_room_assignment_conflicts(
        property_obj=property_obj, run=run, start=start, end=end
    )

    run.diff_count = diff_count
    run.room_conflict_count = room_conflicts
    run.scanned_dates = scanned_dates
    run.scanned_types = scanned_types
    run.save(update_fields=("diff_count", "room_conflict_count", "scanned_dates", "scanned_types"))
    scan_audit_meta = {
        "diff_count": diff_count,
        "scanned_dates": scanned_dates,
        "new_discrepancies": new_count,
        "room_conflicts": room_conflicts,
        "new_room_conflicts": new_room_conflicts,
    }
    AuditEvent.record(
        action="inventory.reconciliation_scanned",
        entity_type="InventoryReconciliationRun",
        entity_id=run.pk,
        actor=checked_by,
        property_obj=property_obj,
        after=scan_audit_meta,
        reason="D4 库存差异扫描",
    )
    return run


@transaction.atomic
def request_inventory_fix(*, property_obj, discrepancy_id, fix_reason, fix_evidence, requested_by):
    """D4：授权人员发起库存修复申请（不自动覆盖权威计数）。"""
    disc = (
        InventoryDiscrepancy.objects.select_for_update()  # type: ignore[has-type]
        .filter(pk=discrepancy_id, property=property_obj)
        .first()
    )
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
    disc.save(
        update_fields=(
            "status",
            "fix_reason",
            "fix_evidence",
            "requested_by",
            "requested_at",
            "updated_at",
        )
    )
    AuditEvent.record(
        action="inventory.fix_requested",
        entity_type="InventoryDiscrepancy",
        entity_id=disc.pk,
        actor=requested_by,
        property_obj=property_obj,
        after={"status": disc.status, "reason": fix_reason[:200]},
        reason="发起库存修复申请",
    )
    return disc


@transaction.atomic
def approve_inventory_fix(*, property_obj, discrepancy_id, approved_by):
    """D4：独立审批；申请人不得自批。"""
    disc = (
        InventoryDiscrepancy.objects.select_for_update()  # type: ignore[has-type]
        .filter(pk=discrepancy_id, property=property_obj)
        .first()
    )
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
        action="inventory.fix_approved",
        entity_type="InventoryDiscrepancy",
        entity_id=disc.pk,
        actor=approved_by,
        property_obj=property_obj,
        after={"status": disc.status},
        reason="审批库存修复申请",
    )
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
    disc = (
        InventoryDiscrepancy.objects.select_for_update()  # type: ignore[has-type]
        .filter(pk=discrepancy_id, property=property_obj)
        .first()
    )
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.FIX_APPROVED:
        raise ValueError("只有已批准的修复可以执行。")

    # RSV-02：执行前重新计算，且必须与扫描端使用完全相同的口径。
    actual_held, actual_sold = _recompute_inventory_day(
        property_obj=property_obj, room_type=disc.room_type, stay_date=disc.stay_date
    )

    # RSV-02：此前此处没有物业过滤，room_type 全局非唯一时会锁到/改写
    # 其他物业的库存行。按物业 + 主键固定顺序锁定。
    row = (
        RoomTypeInventory.objects.select_for_update()
        .filter(property=property_obj, room_type=disc.room_type, stay_date=disc.stay_date)
        .order_by("pk")
        .first()
    )
    if row is None:
        raise ValueError("库存行不存在或不属于当前物业。")

    # RSV-01 防线：明细链路缺失时，actual_held 恒为 0。若据此执行
    # row.held = 0，会把库存服务真实维护的冻结清零，进而放开容量约束
    # 造成超卖，且原客支付回调时 confirm_inventory_for_reservation 会因
    # "Hold 已失效" 拒绝确认（钱收了房没了）。此处拒绝自动修复，保留
    # 权威计数并要求人工核对。
    if actual_held == 0 and row.held > 0:
        has_any_detail = InventoryHold.objects.filter(
            property=property_obj, room_type=disc.room_type, stay_date=disc.stay_date
        ).exists()
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
        property_obj=property_obj, room_type=disc.room_type, stay_date=disc.stay_date
    )
    if post_held != row.held or post_sold != row.sold:
        AuditEvent.record(
            action="inventory.fix_post_check_failed",
            entity_type="InventoryDiscrepancy",
            entity_id=disc.pk,
            actor=executed_by,
            property_obj=property_obj,
            after={
                "held": row.held,
                "sold": row.sold,
                "post_held": post_held,
                "post_sold": post_sold,
            },
            reason="修复后核对仍不一致，需升级处理",
        )

    AuditEvent.record(
        action="inventory.fix_executed",
        entity_type="InventoryDiscrepancy",
        entity_id=disc.pk,
        actor=executed_by,
        property_obj=property_obj,
        after={"status": disc.status, "held": row.held, "sold": row.sold, "version": row.version},
        reason="D4 执行库存修复",
    )
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
            property_obj=property_obj, discrepancy_id=discrepancy_id, executed_by=executed_by
        )
    except _MissingHoldDetails as exc:
        AuditEvent.record(
            action="inventory.fix_refused_missing_hold_details",
            entity_type="InventoryDiscrepancy",
            entity_id=exc.discrepancy_id,
            actor=executed_by,
            property_obj=property_obj,
            after={"row_held": exc.row_held, "recomputed_held": 0},
            reason="无冻结明细，禁止清零真实 held（RSV-01），转人工核对",
        )
        raise ValueError(
            "该住宿日无任何库存冻结明细，无法独立重建 held；为避免"
            "清零真实冻结，已拒绝自动修复，请人工核对（RSV-01）。"
        ) from exc


@transaction.atomic
def dismiss_discrepancy(*, property_obj, discrepancy_id, dismissed_by, reason=""):
    """关闭差异（无差异/误报）。"""
    disc = (
        InventoryDiscrepancy.objects.select_for_update()  # type: ignore[has-type]
        .filter(pk=discrepancy_id, property=property_obj)
        .first()
    )
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.OPEN:
        raise ValueError("只有待处理的差异可以关闭。")
    disc.status = InventoryDiscrepancy.Status.DISMISSED
    disc.save(update_fields=("status", "updated_at"))
    AuditEvent.record(
        action="inventory.disc_dismissed",
        entity_type="InventoryDiscrepancy",
        entity_id=disc.pk,
        actor=dismissed_by,
        property_obj=property_obj,
        after={"status": disc.status},
        reason=str(reason or "")[:240],
    )
    return disc


@transaction.atomic
def review_discrepancy(*, property_obj, discrepancy_id, reviewed_by):
    """M4-3/D4-4：inventory.discrepancy_open 待办的唯一完成复核服务。

    与扫描/执行端共用同一重算口径（``_recompute_inventory_day``）复核差异：
      * 差异已消除/误报 → 经 dismiss_discrepancy 关闭（源领域服务成功，
        WorkItem 随之关闭；不直接改 WorkItem 状态字段伪造闭环）；
      * 差异仍在 → 拒绝完成并指向唯一修复工作流（申请→审批→执行，
        职责分离），WorkItem 保持待处理可重试；
      * 只报告不修复：本服务从不改写库存权威计数（held/sold），也不触碰
        排房明细——排房冲突维度后续只报告、不自动改派（负责人约束）。
    """
    disc = (
        InventoryDiscrepancy.objects.select_for_update()  # type: ignore[has-type]
        .filter(pk=discrepancy_id, property=property_obj)
        .first()
    )
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.OPEN:
        raise ValueError("只有待处理的差异可以复核。")

    row = (
        RoomTypeInventory.objects.filter(
            property=property_obj, room_type=disc.room_type, stay_date=disc.stay_date
        )
        .order_by("pk")
        .first()
    )
    if row is None:
        raise ValueError("库存行不存在，无法复核；请先核对房型库存主数据。")

    actual_held, actual_sold = _recompute_inventory_day(
        property_obj=property_obj, room_type=disc.room_type, stay_date=disc.stay_date
    )
    recomputed_total = row.out_of_service + actual_held + actual_sold
    healed = (
        row.held == actual_held and row.sold == actual_sold and row.total_rooms >= recomputed_total
    )
    if not healed:
        AuditEvent.record(
            action="inventory.disc_review_rejected",
            entity_type="InventoryDiscrepancy",
            entity_id=disc.pk,
            actor=reviewed_by,
            property_obj=property_obj,
            after={
                "held": row.held,
                "sold": row.sold,
                "actual_held": actual_held,
                "actual_sold": actual_sold,
            },
            reason="复核确认差异仍存在，转唯一修复工作流",
        )
        raise ValueError(
            "复核确认差异仍存在（账面 held/sold 与重算真值不一致），"
            "请走唯一库存修复工作流：申请修复→独立审批→执行（职责分离）。"
        )

    dismissed = dismiss_discrepancy(
        property_obj=property_obj,
        discrepancy_id=disc.pk,
        dismissed_by=reviewed_by,
        reason="对账复核：按唯一重算口径核对，差异已消除/误报",
    )
    AuditEvent.record(
        action="inventory.disc_reviewed",
        entity_type="InventoryDiscrepancy",
        entity_id=disc.pk,
        actor=reviewed_by,
        property_obj=property_obj,
        after={
            "held": row.held,
            "sold": row.sold,
            "actual_held": actual_held,
            "actual_sold": actual_sold,
            "status": dismissed.status,
        },
        reason="D4-4 待办复核：重算核对后关闭差异",
    )
    return dismissed


@transaction.atomic
def dismiss_room_conflict(*, property_obj, conflict_id, dismissed_by, reason=""):
    """M4-4/D4-2：关闭排房冲突记录（冲突已消除/误报）。

    排房冲突无修复申请/审批工作流（修复=前台换房/取消多余明细的人工动作，
    不是库存计数改写）；关闭即终态，重复扫描按判定式重判。
    """
    conflict = (
        RoomAssignmentConflict.objects.select_for_update()  # type: ignore[has-type]
        .filter(pk=conflict_id, property=property_obj)
        .first()
    )
    if conflict is None:
        raise ValueError("排房冲突记录不存在或不属于当前物业。")
    if conflict.status != RoomAssignmentConflict.Status.OPEN:
        raise ValueError("只有待处理的排房冲突可以关闭。")
    conflict.status = RoomAssignmentConflict.Status.DISMISSED
    conflict.dismiss_reason = str(reason or "")[:240]
    conflict.dismissed_by = dismissed_by
    conflict.dismissed_at = timezone.now()
    conflict.save(
        update_fields=("status", "dismiss_reason", "dismissed_by", "dismissed_at", "updated_at")
    )
    AuditEvent.record(
        action="inventory.room_conflict_dismissed",
        entity_type="RoomAssignmentConflict",
        entity_id=conflict.pk,
        actor=dismissed_by,
        property_obj=property_obj,
        after={"status": conflict.status},
        reason=str(reason or "")[:240],
    )
    return conflict


@transaction.atomic
def review_room_conflict(*, property_obj, conflict_id, reviewed_by):
    """M4-4/D4-2：inventory.room_conflict_open 待办的唯一完成复核服务。

    与扫描端共用同一判定式（``_room_occupancy_counts``）复核冲突：
      * 冲突已消除/误报 → 经 dismiss_room_conflict 关闭（源领域服务成功，
        WorkItem 随之关闭；不直接改 WorkItem 状态字段伪造闭环）；
      * 冲突仍在 → 拒绝闭环并指向人工修复路径（前台换房/取消多余明细），
        WorkItem 保持待处理可重试；
      * 只报告不自动改派（负责人约束）：本服务从不改写 ReservationRoom/
        排房明细，也不改预订状态——改派只能经唯一前台履约工作流执行。
    """
    conflict = (
        RoomAssignmentConflict.objects.select_for_update()  # type: ignore[has-type]
        .filter(pk=conflict_id, property=property_obj)
        .first()
    )
    if conflict is None:
        raise ValueError("排房冲突记录不存在或不属于当前物业。")
    if conflict.status != RoomAssignmentConflict.Status.OPEN:
        raise ValueError("只有待处理的排房冲突可以复核。")

    occupancy = _room_occupancy_counts(
        property_obj=property_obj,
        start=conflict.stay_date,
        end=conflict.stay_date + timedelta(days=1),
    )
    bucket = occupancy.get((conflict.room_id, conflict.stay_date))
    current_count = bucket["count"] if bucket else 0
    if current_count >= 2:
        AuditEvent.record(
            action="inventory.room_conflict_review_rejected",
            entity_type="RoomAssignmentConflict",
            entity_id=conflict.pk,
            actor=reviewed_by,
            property_obj=property_obj,
            after={
                "room_id": conflict.room_id,
                "stay_date": conflict.stay_date.isoformat(),
                "conflict_count": conflict.conflict_count,
                "current_count": current_count,
            },
            reason="复核确认排房冲突仍存在，转人工修复（前台换房/取消多余明细）",
        )
        raise ValueError(
            "复核确认排房冲突仍存在（同房同日存在 "
            f"{current_count} 条活跃排房明细），请走人工修复：前台换房或"
            "取消多余明细；本系统只报告、不自动改派。"
        )

    dismissed = dismiss_room_conflict(
        property_obj=property_obj,
        conflict_id=conflict.pk,
        dismissed_by=reviewed_by,
        reason="对账复核：按同一判定式重算，冲突已消除/误报",
    )
    AuditEvent.record(
        action="inventory.room_conflict_reviewed",
        entity_type="RoomAssignmentConflict",
        entity_id=conflict.pk,
        actor=reviewed_by,
        property_obj=property_obj,
        after={
            "room_id": conflict.room_id,
            "stay_date": conflict.stay_date.isoformat(),
            "count_at_report": conflict.conflict_count,
            "current_count": current_count,
            "status": dismissed.status,
        },
        reason="D4-2 待办复核：重算核对后关闭排房冲突",
    )
    return dismissed
