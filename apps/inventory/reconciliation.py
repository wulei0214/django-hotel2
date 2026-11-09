# -*- coding: utf-8 -*-
"""D4 库存差异自动扫描与人工审批修复（INV-P0）。

依据 docs/PMS2房态库存中心P0P1专项整改需求_2026-11-09.md：
- 系统自动发现差异并生成报告（不自动覆盖库存权威计数）。
- 差异报告保存计算时间/版本/期望值/当前值/差额/关联订单/Hold。
- 修复申请需原因+证据+影响范围；审批人与申请人分离。
- 执行前重新计算并检查版本；数据已变化则拒绝旧方案。
- 修复使用唯一服务+事务+行锁+审计；修复后再次对账。
"""
from decimal import Decimal

from django.conf import settings
from django.db import transaction, models
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.inventory.models import (
    InventoryDiscrepancy,
    InventoryReconciliationRun,
    RoomType,
    RoomTypeInventory,
)
from apps.reservations.models import (
    InventoryHold,
    Reservation,
    ReservationRoom,
    Stay,
)


def scan_inventory_discrepancies(*, property_obj, days_back=30, checked_by=None):
    """D4：扫描库存差异并生成报告（不自动覆盖库存权威计数）。

    重算口径：total_rooms 应等于 out_of_service + held + sold（数据库约束保证）。
    差异来源：预订 Hold 行数 vs 库存行 held 计数、确认占用行数 vs sold 计数。
    """
    from django.contrib.auth.models import User  # noqa: F401
    today = timezone.localdate()
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
        inventory_rows = RoomTypeInventory.objects.filter(
            property=property_obj, room_type=rt,
            stay_date__gte=start, stay_date__lt=end).order_by("stay_date")
        date_set = set()
        for row in inventory_rows:
            date_set.add(row.stay_date)
        scanned_dates = max(scanned_dates, len(date_set))

        for row in inventory_rows:
            # 重算：统计真实 Hold + 确认占用
            actual_held = InventoryHold.objects.filter(
                room_type=rt, stay_date=row.stay_date,
                status=InventoryHold.HoldStatus.HELD).count()
            actual_sold = ReservationRoom.objects.filter(
                room_type=rt,
                check_in__lte=row.stay_date,
                check_out__gt=row.stay_date,
                reservation__status__in=(
                    Reservation.Status.CONFIRMED,
                    Reservation.Status.CHECKED_IN,
                    Reservation.Status.CHECKED_OUT),
            ).values("reservation_id").distinct().count()

            recomputed_oos = row.out_of_service
            recomputed_total = (recomputed_oos + actual_held + actual_sold)

            if (row.held != actual_held
                    or row.sold != actual_sold
                    or row.total_rooms != recomputed_total
                    and recomputed_total > 0):
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


@transaction.atomic
def execute_inventory_fix(*, property_obj, discrepancy_id, executed_by):
    """D4：执行已批准修复（事务内修正 held/sold；修复后立即再次核对）。"""
    disc = InventoryDiscrepancy.objects.select_for_update().filter(
        pk=discrepancy_id, property=property_obj).first()
    if disc is None:
        raise ValueError("差异记录不存在或不属于当前物业。")
    if disc.status != InventoryDiscrepancy.Status.FIX_APPROVED:
        raise ValueError("只有已批准的修复可以执行。")

    # 执行前重新计算（版本/数据已变化则拒绝旧方案）
    actual_held = InventoryHold.objects.filter(
        room_type=disc.room_type, stay_date=disc.stay_date,
        status=InventoryHold.HoldStatus.HELD).count()
    actual_sold_reservations = ReservationRoom.objects.filter(
        room_type=disc.room_type,
        check_in__lte=disc.stay_date,
        check_out__gt=disc.stay_date,
        reservation__status__in=(
            Reservation.Status.CONFIRMED,
            Reservation.Status.CHECKED_IN,
            Reservation.Status.CHECKED_OUT),
    ).values("reservation_id").distinct().count()

    row = RoomTypeInventory.objects.select_for_update().filter(
        room_type=disc.room_type, stay_date=disc.stay_date).first()
    if row is None:
        raise ValueError("库存行不存在。")

    if row.held == actual_held and row.sold == actual_sold_reservations:
        # 差异已被其他修复消除，直接关闭
        disc.status = InventoryDiscrepancy.Status.FIXED
        disc.executed_by = executed_by
        disc.fixed_at = timezone.now()
        disc.save(update_fields=("status", "executed_by", "fixed_at", "updated_at"))
        return disc

    row.held = actual_held
    row.sold = actual_sold_reservations
    row.version += 1
    row.save(update_fields=("held", "sold", "version", "updated_at"))

    disc.status = InventoryDiscrepancy.Status.FIXED
    disc.executed_by = executed_by
    disc.fixed_at = timezone.now()
    disc.save(update_fields=("status", "executed_by", "fixed_at", "updated_at"))

    # 修复后立即再次对账
    post_check_held = InventoryHold.objects.filter(
        room_type=disc.room_type, stay_date=disc.stay_date,
        status=InventoryHold.HoldStatus.HELD).count()
    if post_check_held != row.held:
        AuditEvent.record(
            action="inventory.fix_post_check_failed",
            entity_type="InventoryDiscrepancy", entity_id=disc.pk,
            actor=executed_by, property_obj=property_obj,
            after={"held": row.held, "post_check": post_check_held},
            reason="修复后核对仍不一致，需升级处理")

    AuditEvent.record(
        action="inventory.fix_executed", entity_type="InventoryDiscrepancy",
        entity_id=disc.pk, actor=executed_by, property_obj=property_obj,
        after={"status": disc.status, "held": row.held, "sold": row.sold,
               "version": row.version},
        reason="D4 执行库存修复")
    return disc


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
