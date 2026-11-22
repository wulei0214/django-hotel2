"""清洁/维修任务级领域命令（MC-006）。

本模块不新增任务表，全部以既有 HousekeepingTask / RoomInspection /
MaintenanceTicket 为载体；房态和库存推进一律复用 apps.inventory.services 的
唯一写入口，保证“任务状态 + 房态 + 库存”在同一事务内成功或回滚。
"""

import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.identity.authorization import AuthorizationService

from .models import HousekeepingTask, MaintenanceTicket, Room, RoomInspection
from .services import (
    HousekeepingOperationError,
    _property_today,
    change_housekeeping_status,
    change_maintenance_status,
)

HK_TERMINAL = (
    HousekeepingTask.TaskStatus.INSPECTED,
    HousekeepingTask.TaskStatus.CANCELLED,
)
MT_TERMINAL = (
    MaintenanceTicket.TicketStatus.VERIFIED,
    MaintenanceTicket.TicketStatus.CANCELLED,
)


def _require(user, property_obj, permission, message):
    if not AuthorizationService.has_permission(user, property_obj, permission):
        raise HousekeepingOperationError(message)


def _task_or_error(property_obj, task_id):
    task = (
        HousekeepingTask.objects.select_for_update()
        .filter(pk=task_id, property=property_obj)
        .first()
    )
    if task is None:
        raise HousekeepingOperationError("清洁任务不存在或不属于当前物业。")
    return task


def _ticket_or_error(property_obj, ticket_id):
    ticket = (
        MaintenanceTicket.objects.select_for_update()
        .filter(pk=ticket_id, property=property_obj)
        .first()
    )
    if ticket is None:
        raise HousekeepingOperationError("维修工单不存在或不属于当前物业。")
    return ticket


def _audit(action, entity_type, entity_id, actor, property_obj, before, after, reason):
    AuditEvent.record(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        property_obj=property_obj,
        before=before,
        after=after,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 清洁任务命令
# ---------------------------------------------------------------------------
@transaction.atomic
def claim_housekeeping_task(*, property_obj, task_id, actor):
    """接单：只分配责任人，不推进房态。"""
    _require(actor, property_obj, "inventory.housekeeping_update", "缺少客房任务更新权限。")
    task = _task_or_error(property_obj, task_id)
    if task.status in HK_TERMINAL:
        raise HousekeepingOperationError("该清洁任务已关闭，不能接单。")
    if task.assigned_to_id and task.assigned_to_id != actor.pk:
        raise HousekeepingOperationError("该清洁任务已有其他负责人。")
    changed = task.status != HousekeepingTask.TaskStatus.PENDING or task.assigned_to_id != actor.pk
    if changed:
        before = {"status": task.status, "assigned_to": task.assigned_to_id}
        task.assigned_to = actor
        task.save(update_fields=("assigned_to", "updated_at"))
        _audit(
            "housekeeping.task_claimed",
            "HousekeepingTask",
            task.pk,
            actor,
            property_obj,
            before,
            {"status": task.status, "assigned_to": actor.pk},
            "员工认领任务；房态待开始动作推进",
        )
    return task, changed


@transaction.atomic
def assign_housekeeping_task(*, property_obj, task_id, assignee, actor):
    """派工：不改源业务状态，任务回到待清洁。"""
    _require(actor, property_obj, "inventory.housekeeping_update", "缺少客房任务更新权限。")
    task = _task_or_error(property_obj, task_id)
    if task.status in HK_TERMINAL:
        raise HousekeepingOperationError("该清洁任务已关闭，不能派工。")
    if AuthorizationService.get_active_membership(assignee, property_obj) is None:
        raise HousekeepingOperationError("被指派员工在当前物业没有有效任职。")
    before = {"status": task.status, "assigned_to": task.assigned_to_id}
    task.status = HousekeepingTask.TaskStatus.PENDING
    task.assigned_to = assignee
    task.started_at = None
    task.save(update_fields=("status", "assigned_to", "started_at", "updated_at"))
    _audit(
        "housekeeping.task_assigned",
        "HousekeepingTask",
        task.pk,
        actor,
        property_obj,
        before,
        {"status": task.status, "assigned_to": assignee.pk},
        "管理者派工",
    )
    return task


@transaction.atomic
def start_housekeeping_task(*, property_obj, task_id, actor):
    """开始清洁：任务与房间 DIRTY→CLEANING 同事务。

    MC11-HK-10 伴随缺陷（2026-11-22 E2E 实证）：任务池 REJECTED 行的
    「重新开始」按钮自 c73aaf2 起调用本服务，但本服务此前只接受 PENDING——
    REJECTED 重开必然 409，按钮从未可用。模型 help_text 既有口径
    「REJECTED 回 IN_PROGRESS」（models.py 状态机注释）与页面按钮一致，
    此处补齐服务层语义：REJECTED 任务重新开始，同事务把已检查退回的
    房间 DIRTY→CLEANING（与 PENDING 开始同一推进路径，不新建状态机）。
    """
    _require(actor, property_obj, "inventory.housekeeping_update", "缺少客房任务更新权限。")
    task = _task_or_error(property_obj, task_id)
    if task.status == HousekeepingTask.TaskStatus.IN_PROGRESS:
        return task, False
    if task.status not in (
        HousekeepingTask.TaskStatus.PENDING,
        HousekeepingTask.TaskStatus.REJECTED,
    ):
        raise HousekeepingOperationError("只有待清洁或检查退回的任务可以开始。")
    if task.assigned_to_id and task.assigned_to_id != actor.pk:
        raise HousekeepingOperationError("该任务已指派给其他员工。")
    before = {"status": task.status, "housekeeping_status": task.room.housekeeping_status}
    task.status = HousekeepingTask.TaskStatus.IN_PROGRESS
    task.assigned_to = task.assigned_to or actor
    task.started_at = timezone.now()
    task.save(update_fields=("status", "assigned_to", "started_at", "updated_at"))
    change_housekeeping_status(
        property_obj=property_obj,
        room_id=task.room_id,
        target_status=Room.HousekeepingStatus.CLEANING,
        changed_by=actor,
        notes=f"任务#{task.pk} 开始清洁",
    )
    _audit(
        "housekeeping.task_started",
        "HousekeepingTask",
        task.pk,
        actor,
        property_obj,
        before,
        {"status": task.status, "housekeeping_status": Room.HousekeepingStatus.CLEANING},
        "开始清洁任务",
    )
    return task, True


@transaction.atomic
def complete_housekeeping_task(*, property_obj, task_id, actor):
    """完成清洁：任务→DONE，房间 CLEANING→INSPECTING，同事务。"""
    _require(actor, property_obj, "inventory.housekeeping_update", "缺少客房任务更新权限。")
    task = _task_or_error(property_obj, task_id)
    if task.status == HousekeepingTask.TaskStatus.DONE:
        return task, False
    if task.status != HousekeepingTask.TaskStatus.IN_PROGRESS:
        raise HousekeepingOperationError("只有清洁中的任务可以完成。")
    before = {"status": task.status, "housekeeping_status": task.room.housekeeping_status}
    task.status = HousekeepingTask.TaskStatus.DONE
    task.completed_at = timezone.now()
    task.completed_by = actor
    task.save(update_fields=("status", "completed_at", "completed_by", "updated_at"))
    change_housekeeping_status(
        property_obj=property_obj,
        room_id=task.room_id,
        target_status=Room.HousekeepingStatus.INSPECTING,
        changed_by=actor,
        notes=f"任务#{task.pk} 完成清洁待检查",
    )
    _audit(
        "housekeeping.task_completed",
        "HousekeepingTask",
        task.pk,
        actor,
        property_obj,
        before,
        {"status": task.status, "housekeeping_status": Room.HousekeepingStatus.INSPECTING},
        "清洁完成待检查",
    )
    return task, True


@transaction.atomic
def inspect_housekeeping_task(
    *,
    property_obj,
    task_id,
    actor,
    result,
    checklist_result=None,
    failed_items=None,
    notes="",
):
    """检查/复检：落 RoomInspection，并与净房放行/返工同事务。"""
    if result not in RoomInspection.InspectResult.values:
        raise HousekeepingOperationError("检查结论只能是 PASS 或 FAIL。")
    permission = (
        "hotelapp.room_release_ready"
        if result == RoomInspection.InspectResult.PASS
        else "inventory.housekeeping_update"
    )
    _require(actor, property_obj, permission, "当前岗位无权执行该检查结论。")
    task = _task_or_error(property_obj, task_id)
    if task.status != HousekeepingTask.TaskStatus.DONE:
        raise HousekeepingOperationError("只有待检查的清洁任务可以检查。")
    if task.completed_by_id == actor.pk and result == RoomInspection.InspectResult.PASS:
        raise HousekeepingOperationError("清洁人不得自检放行同一任务。")
    failed_items = failed_items or []
    if result == RoomInspection.InspectResult.FAIL and not failed_items:
        raise HousekeepingOperationError("检查不通过必须填写不合格项。")

    before = {"status": task.status, "housekeeping_status": task.room.housekeeping_status}
    inspection = RoomInspection.objects.create(
        task=task,
        room=task.room,
        inspector=actor,
        result=result,
        checklist_result=checklist_result or {},
        failed_items=failed_items,
        reason=notes[:240],
        notes=notes,
    )
    if result == RoomInspection.InspectResult.PASS:
        task.status = HousekeepingTask.TaskStatus.INSPECTED
        target = Room.HousekeepingStatus.CLEAN
    else:
        task.status = HousekeepingTask.TaskStatus.REJECTED
        target = Room.HousekeepingStatus.DIRTY
    task.inspected_by = actor
    task.inspected_at = timezone.now()
    task.save(update_fields=("status", "inspected_by", "inspected_at", "updated_at"))
    change_housekeeping_status(
        property_obj=property_obj,
        room_id=task.room_id,
        target_status=target,
        changed_by=actor,
        notes=f"任务#{task.pk} 检查{result}",
    )
    if result == RoomInspection.InspectResult.FAIL:
        HousekeepingTask.objects.create(
            property=property_obj,
            room=task.room,
            task_type=task.task_type,
            priority=task.priority,
            due_at=timezone.now() + timedelta(hours=4),
            created_by=actor,
            assigned_to=task.assigned_to,
            notes=f"检查返工（原任务#{task.pk}）：{notes[:200]}",
        )
    _audit(
        "housekeeping.task_inspected",
        "RoomInspection",
        inspection.pk,
        actor,
        property_obj,
        before,
        {"status": task.status, "housekeeping_status": target, "result": result},
        notes or "清洁任务检查",
    )
    return task, inspection


@transaction.atomic
def cancel_housekeeping_task(*, property_obj, task_id, actor, reason):
    """取消尚未开始的清洁任务；不动房态。"""
    _require(actor, property_obj, "inventory.housekeeping_update", "缺少客房任务更新权限。")
    if not str(reason or "").strip():
        raise HousekeepingOperationError("取消任务必须填写原因。")
    task = _task_or_error(property_obj, task_id)
    if task.status != HousekeepingTask.TaskStatus.PENDING:
        raise HousekeepingOperationError("只有待清洁任务可以取消。")
    before = {"status": task.status}
    task.status = HousekeepingTask.TaskStatus.CANCELLED
    task.notes = f"{task.notes}\n取消原因：{reason[:200]}".strip()
    task.save(update_fields=("status", "notes", "updated_at"))
    _audit(
        "housekeeping.task_cancelled",
        "HousekeepingTask",
        task.pk,
        actor,
        property_obj,
        before,
        {"status": task.status},
        reason[:240],
    )
    return task


# ---------------------------------------------------------------------------
# 维修工单命令
# ---------------------------------------------------------------------------
def _ticket_no(property_obj):
    return f"MT-{property_obj.code}-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


@transaction.atomic
def create_maintenance_ticket(
    *,
    property_obj,
    room_id,
    reason,
    actor,
    category="",
    severity="NORMAL",
    from_date=None,
    to_date=None,
    deactivate_room=True,
):
    """报修建单；默认同步登记房间维修停用与未来库存影响。"""
    _require(actor, property_obj, "inventory.maintenance_manage", "缺少维修管理权限。")
    reason = str(reason or "").strip()
    if not 2 <= len(reason) <= 240:
        raise HousekeepingOperationError("报修原因需为 2 至 240 个字符。")
    if severity not in ("LOW", "NORMAL", "HIGH"):
        raise HousekeepingOperationError("紧急程度只能是 LOW/NORMAL/HIGH。")
    room = Room.objects.filter(pk=room_id, property=property_obj, is_active=True).first()
    if room is None:
        raise HousekeepingOperationError("房间不存在或不属于当前物业。")
    if from_date is None:
        from_date = _property_today(property_obj)
    if to_date is not None and to_date < from_date:
        raise HousekeepingOperationError("停用结束日期不得早于开始日期。")
    ticket = MaintenanceTicket.objects.create(
        property=property_obj,
        room=room,
        from_date=from_date,
        to_date=to_date,
        reason=reason,
        category=category[:32],
        severity=severity,
        ticket_no=_ticket_no(property_obj),
        reported_by=actor,
    )
    if deactivate_room:
        change_maintenance_status(
            property_obj=property_obj,
            room_id=room.pk,
            target_status=Room.MaintenanceStatus.OUT_OF_ORDER,
            reason=f"维修工单 {ticket.ticket_no}：{reason}",
            changed_by=actor,
        )
    _audit(
        "maintenance.ticket_created",
        "MaintenanceTicket",
        ticket.pk,
        actor,
        property_obj,
        {},
        {"ticket_no": ticket.ticket_no, "room": room.pk, "severity": severity},
        reason,
    )
    return ticket


@transaction.atomic
def assign_maintenance_ticket(*, property_obj, ticket_id, assignee, actor):
    _require(actor, property_obj, "inventory.maintenance_manage", "缺少维修管理权限。")
    ticket = _ticket_or_error(property_obj, ticket_id)
    if ticket.status != MaintenanceTicket.TicketStatus.OPEN:
        raise HousekeepingOperationError("只有已报修工单可以派工。")
    if AuthorizationService.get_active_membership(assignee, property_obj) is None:
        raise HousekeepingOperationError("被指派员工在当前物业没有有效任职。")
    ticket.assigned_to = assignee
    ticket.save(update_fields=("assigned_to",))
    _audit(
        "maintenance.ticket_assigned",
        "MaintenanceTicket",
        ticket.pk,
        actor,
        property_obj,
        {},
        {"assigned_to": assignee.pk},
        "维修派工",
    )
    return ticket


@transaction.atomic
def start_maintenance_ticket(*, property_obj, ticket_id, actor):
    _require(actor, property_obj, "inventory.maintenance_manage", "缺少维修管理权限。")
    ticket = _ticket_or_error(property_obj, ticket_id)
    if ticket.assigned_to_id and ticket.assigned_to_id != actor.pk:
        raise HousekeepingOperationError("该工单已指派给其他维修人员。")
    if ticket.status == MaintenanceTicket.TicketStatus.IN_PROGRESS:
        return ticket, False
    if ticket.status != MaintenanceTicket.TicketStatus.OPEN:
        raise HousekeepingOperationError("只有已报修工单可以开始维修。")
    ticket.status = MaintenanceTicket.TicketStatus.IN_PROGRESS
    ticket.assigned_to = ticket.assigned_to or actor
    ticket.started_at = timezone.now()
    ticket.save(update_fields=("status", "assigned_to", "started_at"))
    _audit(
        "maintenance.ticket_started",
        "MaintenanceTicket",
        ticket.pk,
        actor,
        property_obj,
        {"status": MaintenanceTicket.TicketStatus.OPEN},
        {"status": ticket.status},
        "开始维修",
    )
    return ticket, True


@transaction.atomic
def resolve_maintenance_ticket(*, property_obj, ticket_id, actor, notes=""):
    """维修完成 → 待验收；不直接恢复房态/库存。"""
    _require(actor, property_obj, "inventory.maintenance_manage", "缺少维修管理权限。")
    ticket = _ticket_or_error(property_obj, ticket_id)
    if ticket.status == MaintenanceTicket.TicketStatus.PENDING_VERIFY:
        return ticket, False
    if ticket.status != MaintenanceTicket.TicketStatus.IN_PROGRESS:
        raise HousekeepingOperationError("只有维修中的工单可以提交验收。")
    ticket.status = MaintenanceTicket.TicketStatus.PENDING_VERIFY
    ticket.notes = f"{ticket.notes}\n维修说明：{notes[:200]}".strip()
    ticket.save(update_fields=("status", "notes"))
    _audit(
        "maintenance.ticket_resolved",
        "MaintenanceTicket",
        ticket.pk,
        actor,
        property_obj,
        {"status": MaintenanceTicket.TicketStatus.IN_PROGRESS},
        {"status": ticket.status},
        notes[:240] or "维修完成待验收",
    )
    return ticket, True


@transaction.atomic
def verify_maintenance_ticket(
    *,
    property_obj,
    ticket_id,
    actor,
    result,
    notes="",
):
    """验收：PASS 才恢复房间与库存；FAIL 退回维修中。"""
    if result not in ("PASS", "FAIL"):
        raise HousekeepingOperationError("验收结论只能是 PASS 或 FAIL。")
    _require(actor, property_obj, "inventory.maintenance_manage", "缺少维修管理权限。")
    ticket = _ticket_or_error(property_obj, ticket_id)
    if ticket.status != MaintenanceTicket.TicketStatus.PENDING_VERIFY:
        raise HousekeepingOperationError("只有待验收工单可以验收。")
    if ticket.reported_by_id == actor.pk and result == "PASS":
        raise HousekeepingOperationError("报修人不得验收自己报修的工单。")
    # MC11-HK-05：维修执行人（派工/开始维修的承担者）不得自行验收，
    # 与清洁链"完成人不得自检放行"同口径（services_housekeeping_tasks.py SoD）。
    if ticket.assigned_to_id == actor.pk and result == "PASS":
        raise HousekeepingOperationError("维修执行人不得自行验收，需他人复核。")
    before = {"status": ticket.status}
    if result == "PASS":
        ticket.status = MaintenanceTicket.TicketStatus.VERIFIED
        ticket.verified_by = actor
        ticket.verified_at = timezone.now()
        ticket.restored_by = actor
        ticket.restored_at = ticket.verified_at
        change_maintenance_status(
            property_obj=property_obj,
            room_id=ticket.room_id,
            target_status=Room.MaintenanceStatus.NORMAL,
            reason=f"维修工单 {ticket.ticket_no} 验收通过，恢复可售",
            changed_by=actor,
            allow_ticket_id=ticket.pk,
        )
    else:
        ticket.status = MaintenanceTicket.TicketStatus.IN_PROGRESS
    ticket.notes = f"{ticket.notes}\n验收：{result} {notes[:200]}".strip()
    ticket.save(
        update_fields=(
            "status",
            "verified_by",
            "verified_at",
            "restored_by",
            "restored_at",
            "notes",
        )
    )
    _audit(
        "maintenance.ticket_verified",
        "MaintenanceTicket",
        ticket.pk,
        actor,
        property_obj,
        before,
        {"status": ticket.status, "result": result},
        notes[:240] or "维修验收",
    )
    return ticket


@transaction.atomic
def cancel_maintenance_ticket(*, property_obj, ticket_id, actor, reason):
    _require(actor, property_obj, "inventory.maintenance_manage", "缺少维修管理权限。")
    if not str(reason or "").strip():
        raise HousekeepingOperationError("取消工单必须填写原因。")
    ticket = _ticket_or_error(property_obj, ticket_id)
    if ticket.status != MaintenanceTicket.TicketStatus.OPEN:
        raise HousekeepingOperationError("只有已报修且尚未派工/维修的工单可以取消。")
    ticket.status = MaintenanceTicket.TicketStatus.CANCELLED
    ticket.notes = f"{ticket.notes}\n取消原因：{reason[:200]}".strip()
    ticket.save(update_fields=("status", "notes"))
    _audit(
        "maintenance.ticket_cancelled",
        "MaintenanceTicket",
        ticket.pk,
        actor,
        property_obj,
        {"status": MaintenanceTicket.TicketStatus.OPEN},
        {"status": ticket.status},
        reason[:240],
    )
    return ticket
