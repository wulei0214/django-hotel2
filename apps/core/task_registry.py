"""可执行待办的动作注册表（MC-007）。

一条 TaskSpec 只声明“这类任务如何完成”，不保存源业务状态副本：
  * source_type：源业务实体类型；
  * permission：完成动作应校验的现有权限码（不新增权限码）；
  * executor：真正调用源领域服务的函数；None 表示该来源当前只能提示、
    必须跳转唯一权威工作流处理，完成端点显式拒绝，绝不改 WorkItem 伪造闭环。
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class TaskSpec:
    task_code: str
    source_type: str
    title: str
    permission: Optional[str]
    executor: Optional[Callable[..., Any]]
    link: str
    note: str = ""


def _approval_executor(*, work_item, actor, payload, request):
    """审批类任务：只调用既有审批领域服务，不直接改 ApprovalRecord。"""
    from apps.billing.models import ApprovalRecord
    from apps.billing.night_audit import NightAuditService
    from apps.billing.services_v2 import ApprovalService

    record = ApprovalRecord.objects.filter(
        pk=int(work_item.source_id), property=work_item.property
    ).first()
    if record is None:
        raise ValueError("审批记录不存在或不属于当前物业。")
    reason = str(payload.get("reason", "") or "").strip()
    if len(reason) < 2 or len(reason) > 240:
        raise ValueError("审批说明需为 2 至 240 个字符。")
    if record.action_code == "night_audit.reopen":
        return NightAuditService.approve_reopen(
            property_obj=work_item.property,
            business_date_id=record.entity_id,
            approval_id=record.pk,
            approved_by=actor,
        )
    if record.action_code == "billing.refund":
        return ApprovalService.approve(
            record.pk, approved_by=actor, property_obj=work_item.property
        )
    return ApprovalService.approve(record.pk, approved_by=actor, property_obj=work_item.property)


def _break_glass_review_executor(*, work_item, actor, payload, request):
    """break-glass 事后复核：复用 NightAuditService 的 SoD 与状态机。"""
    from apps.billing.night_audit import NightAuditService

    return NightAuditService.review_break_glass(
        property_obj=work_item.property,
        record_id=int(work_item.source_id),
        reviewed_by=actor,
        decision=str(payload.get("decision", "CONFIRMED") or "CONFIRMED"),
        reason=str(payload.get("reason", "") or ""),
    )


def _inventory_discrepancy_review_executor(*, work_item, actor, payload, request):
    """库存差异复核（M4-3/D4-4）：调用对账服务按唯一重算口径核对。

    差异已消除/误报 → 经 dismiss_discrepancy 关闭（源领域服务成功后才关
    WorkItem）；差异仍在 → 拒绝闭环并指向唯一修复工作流（申请→审批→执行），
    绝不改 WorkItem 状态字段伪造完成。
    """
    from apps.inventory.reconciliation import review_discrepancy

    return review_discrepancy(
        property_obj=work_item.property,
        discrepancy_id=int(work_item.source_id),
        reviewed_by=actor,
    )


def _room_conflict_review_executor(*, work_item, actor, payload, request):
    """排房冲突复核（M4-4/D4-2）：调用对账服务按同一判定式重算核对。

    冲突已消除/误报 → 经 dismiss_room_conflict 关闭（源领域服务成功后才关
    WorkItem）；冲突仍在 → 拒绝闭环并指向人工修复（前台换房/取消多余明细）。
    复核服务永不改写排房明细——排房冲突只报告、不自动改派（负责人约束）。
    """
    from apps.inventory.reconciliation import review_room_conflict

    return review_room_conflict(
        property_obj=work_item.property,
        conflict_id=int(work_item.source_id),
        reviewed_by=actor,
    )


TASK_SPECS = {
    "approval.pending": TaskSpec(
        task_code="approval.pending",
        source_type="ApprovalRecord",
        title="高危操作待审批",
        permission="billing.approval_view",
        executor=_approval_executor,
        link="/manage/finance/",
        note="完成调用 ApprovalService / NightAuditService；申请人不得审批自己的请求。",
    ),
    "audit.breakglass_review": TaskSpec(
        task_code="audit.breakglass_review",
        source_type="BreakGlassRecord",
        title="紧急重开待复核",
        permission="core.breakglass_review",
        executor=_break_glass_review_executor,
        link="/manage/night-audit/",
        note="完成调用 NightAuditService.review_break_glass；操作人不得复核。",
    ),
    "reservation.pending_confirm": TaskSpec(
        task_code="reservation.pending_confirm",
        source_type="Reservation",
        title="待确认预订",
        permission="hotelapp.change_accommodation",
        executor=None,
        link="/checkin/",
        note="当前唯一确认路径为前台收款或在线支付回调，待负责人裁定独立确认入口。",
    ),
    "reservation.arrival_today": TaskSpec(
        task_code="reservation.arrival_today",
        source_type="Reservation",
        title="今日预抵",
        permission="hotelapp.change_accommodation",
        executor=None,
        link="/checkin/",
        note="请在唯一前台履约工作台完成排房/入住，待办随源状态自动销项。",
    ),
    "reservation.departure_today": TaskSpec(
        task_code="reservation.departure_today",
        source_type="Reservation",
        title="今日离店",
        permission="hotelapp.change_accommodation",
        executor=None,
        link="/checkin/",
        note="请在唯一前台履约工作台完成退房，待办随源状态自动销项。",
    ),
    "reservation.overdue": TaskSpec(
        task_code="reservation.overdue",
        source_type="Reservation",
        title="过期未到",
        permission="hotelapp.no_show_handle",
        executor=None,
        link="/checkin/",
        note="请在唯一前台履约工作台按 No-Show 处理，待办随源状态自动销项。",
    ),
    "housekeeping.cleaning_pending": TaskSpec(
        task_code="housekeeping.cleaning_pending",
        source_type="Room",
        title="待清洁房汇总",
        permission="inventory.housekeeping_update",
        executor=None,
        link="/manage/housekeeping/",
        note="汇总型提示不生成可完成 WorkItem；任务级闭环见 MC-006 任务 API。",
    ),
    "housekeeping.release_pending": TaskSpec(
        task_code="housekeeping.release_pending",
        source_type="Room",
        title="待放行净房汇总",
        permission="hotelapp.room_release_ready",
        executor=None,
        link="/manage/housekeeping/",
        note="汇总型提示不生成可完成 WorkItem；净房放行须按房间逐间执行。",
    ),
    "inventory.discrepancy_open": TaskSpec(
        task_code="inventory.discrepancy_open",
        source_type="InventoryDiscrepancy",
        title="库存对账差异待复核",
        permission="inventory.maintenance_manage",
        executor=_inventory_discrepancy_review_executor,
        link="/manage/rooms/",
        note="完成调用 reconciliation.review_discrepancy 重算核对；差异仍在须走唯一修复工作流（申请→审批→执行），不改状态字段闭环。",
    ),
    "inventory.calendar_coverage": TaskSpec(
        task_code="inventory.calendar_coverage",
        source_type="RoomType",
        title="库存日历覆盖不足",
        permission="inventory.maintenance_manage",
        executor=None,
        link="/manage/rooms/",
        note="日历滚动补齐走唯一同步命令 sync_inventory_calendar（调度每日 02:30）；待办随覆盖恢复自动销项。",
    ),
    "inventory.room_conflict_open": TaskSpec(
        task_code="inventory.room_conflict_open",
        source_type="RoomAssignmentConflict",
        title="排房冲突待复核",
        permission="inventory.maintenance_manage",
        executor=_room_conflict_review_executor,
        link="/manage/rooms/",
        note="完成调用 reconciliation.review_room_conflict 按同一判定式重算；冲突仍在须人工修复（前台换房/取消多余明细），系统只报告不自动改派。",
    ),
}


def get_spec(task_code):
    """返回 TaskSpec；未登记来源返回 None（不生成可执行任务）。"""
    return TASK_SPECS.get(task_code)


def execute_source_service(*, work_item, actor, payload, request):
    """唯一完成入口：调用源领域服务；未登记可执行服务时原样拒绝。"""
    spec = get_spec(work_item.task_code)
    if spec is None or spec.executor is None:
        raise ValueError(spec.note if spec else "该待办来源未登记可执行领域服务。")
    return spec.executor(work_item=work_item, actor=actor, payload=payload or {}, request=request)
