"""WorkItem 编排服务（MC-007）。

只负责“任务分配/认领/完成过程”，源业务状态永不复制到这里：
  * sync_work_items 由通知派生服务在同一事务内调用，一个源事实一条任务；
  * 源事实消失时只关闭 WorkItem（记录 SOURCE_RESOLVED，不写 completed_by）；
  * claim/assign 只改分配事实；
  * complete 必须调用 task_registry 的源领域服务，成功后同事务关闭；
    失败在独立事务记录错误，保持待处理状态可重试。
"""

from django.db import transaction
from django.utils import timezone

from apps.core.models import AuditEvent, Notification, WorkItem
from apps.core.task_registry import execute_source_service, get_spec

AGGREGATE_KEY = "all"


class WorkItemError(Exception):
    """任务编排状态冲突/权限不足，由 API 映射 409/403。"""


def work_item_dedupe_key(task_code, source_type, source_id):
    """稳定去重键：一个源事实 + 任务类型一条，不含岗位组。"""
    return f"{task_code}|{source_type}:{source_id}"[:128]


def _audience_group_id(item, group_ids_by_name):
    for name in item.get("audiences", ()):
        group_id = group_ids_by_name.get(name)
        if group_id is not None:
            return group_id
    return None


def sync_work_items(property_obj, items, group_ids_by_name, now=None):
    """把当前活跃源事实对账为 WorkItem；返回 dedupe_key → WorkItem 映射。

    汇总型待办（biz_key='all'）只有 Notification 投影，不生成无法按单一源对象
    完成的任务；未登记 TaskSpec 的来源同样跳过，避免不可执行任务堆积。
    """
    now = now or timezone.now()
    result = {}
    active_keys = set()

    with transaction.atomic():
        for item in items:
            spec = get_spec(item["code"])
            if spec is None or item["biz_key"] == AGGREGATE_KEY:
                continue
            group_id = _audience_group_id(item, group_ids_by_name)
            key = work_item_dedupe_key(item["code"], spec.source_type, item["biz_key"])
            active_keys.add(key)
            defaults = {
                "task_code": spec.task_code,
                "source_type": spec.source_type,
                "source_id": str(item["biz_key"])[:64],
                "title": item["title"],
                "body": item["body"],
                "link": item["link"] or spec.link,
                "severity": item["severity"],
                "assigned_group_id": group_id,
                "expires_at": item.get("expires_at"),
            }
            work_item, created = WorkItem.objects.get_or_create(
                property=property_obj, dedupe_key=key, defaults=defaults
            )
            if not created:
                changed = []
                for field, value in defaults.items():
                    if getattr(work_item, field) != value:
                        setattr(work_item, field, value)
                        changed.append(field)
                if changed:
                    work_item.save(update_fields=changed)
            result[key] = work_item

        stale = WorkItem.objects.filter(
            property=property_obj,
            status__in=(WorkItem.Status.PENDING, WorkItem.Status.CLAIMED),
        ).exclude(dedupe_key__in=active_keys)
        for work_item in stale:
            work_item.status = WorkItem.Status.DONE
            work_item.resolution = WorkItem.Resolution.SOURCE_RESOLVED
            work_item.completed_at = now
            work_item.save(update_fields=("status", "resolution", "completed_at", "updated_at"))

    return result


def _get_locked(property_obj, work_item_id):
    work_item = (
        WorkItem.objects.select_for_update().filter(pk=work_item_id, property=property_obj).first()
    )
    if work_item is None:
        raise WorkItemError("待办不存在或不属于当前物业。")
    return work_item


def claim_work_item(*, property_obj, work_item_id, profile, actor):
    """认领待办：同一人重复认领幂等，他人已认领返回冲突。"""
    with transaction.atomic():
        work_item = _get_locked(property_obj, work_item_id)
        if work_item.status in (WorkItem.Status.DONE, WorkItem.Status.CANCELLED):
            raise WorkItemError("该待办已关闭，不能认领。")
        claimed_by_id = getattr(work_item, "claimed_by_id", None)
        if claimed_by_id and claimed_by_id != profile.pk:
            raise WorkItemError("该待办已由其他员工认领。")
        changed = work_item.status != WorkItem.Status.CLAIMED or (claimed_by_id != profile.pk)
        if changed:
            work_item.status = WorkItem.Status.CLAIMED
            work_item.claimed_by = profile
            work_item.claimed_at = timezone.now()
            work_item.assigned_profile = profile
            work_item.save(
                update_fields=(
                    "status",
                    "claimed_by",
                    "claimed_at",
                    "assigned_profile",
                    "updated_at",
                )
            )
            AuditEvent.record(
                action="workitem.claimed",
                entity_type="WorkItem",
                entity_id=work_item.pk,
                actor=actor,
                property_obj=property_obj,
                after={"task_code": work_item.task_code, "claimed_by": profile.pk},
                reason="员工认领可执行待办",
            )
    return work_item, changed


def assign_work_item(*, property_obj, work_item_id, target_profile, actor):
    """指派待办：只改分配事实，不改源业务，不关闭任务。"""
    with transaction.atomic():
        work_item = _get_locked(property_obj, work_item_id)
        if work_item.status in (WorkItem.Status.DONE, WorkItem.Status.CANCELLED):
            raise WorkItemError("该待办已关闭，不能指派。")
        work_item.assigned_profile = target_profile
        work_item.status = WorkItem.Status.PENDING
        work_item.claimed_by = None
        work_item.claimed_at = None
        work_item.save(
            update_fields=("assigned_profile", "status", "claimed_by", "claimed_at", "updated_at")
        )
        AuditEvent.record(
            action="workitem.assigned",
            entity_type="WorkItem",
            entity_id=work_item.pk,
            actor=actor,
            property_obj=property_obj,
            after={"task_code": work_item.task_code, "assigned_to": target_profile.pk},
            reason="管理者指派可执行待办",
        )
    return work_item


def complete_work_item(*, property_obj, work_item_id, profile, actor, payload, request=None):
    """完成待办：源领域服务成功后同事务关闭；失败保留待处理并记错误。"""
    work_item = WorkItem.objects.filter(pk=work_item_id, property=property_obj).first()
    if work_item is None:
        raise WorkItemError("待办不存在或不属于当前物业。")
    if work_item.status in (WorkItem.Status.DONE, WorkItem.Status.CANCELLED):
        return work_item, False
    claimed_by_id = getattr(work_item, "claimed_by_id", None)
    if claimed_by_id and claimed_by_id != profile.pk:
        raise WorkItemError("该待办已由其他员工认领，不能代为完成。")
    spec = get_spec(work_item.task_code)
    if spec is None or spec.executor is None:
        raise WorkItemError(
            spec.note if spec else "该来源未登记可执行领域服务，请通过权威业务工作台处理。"
        )

    try:
        with transaction.atomic():
            locked = WorkItem.objects.select_for_update().get(pk=work_item.pk)
            if locked.status in (WorkItem.Status.DONE, WorkItem.Status.CANCELLED):
                return locked, False
            execute_source_service(work_item=locked, actor=actor, payload=payload, request=request)
            locked.refresh_from_db()
            locked.status = WorkItem.Status.DONE
            locked.resolution = WorkItem.Resolution.COMPLETED
            locked.completed_by = profile
            locked.completed_at = timezone.now()
            locked.last_error_code = ""
            locked.last_error_summary = ""
            locked.save(
                update_fields=(
                    "status",
                    "resolution",
                    "completed_by",
                    "completed_at",
                    "last_error_code",
                    "last_error_summary",
                    "updated_at",
                )
            )
            AuditEvent.record(
                action="workitem.completed",
                entity_type="WorkItem",
                entity_id=locked.pk,
                actor=actor,
                property_obj=property_obj,
                after={"task_code": locked.task_code, "source_id": locked.source_id},
                reason="源领域服务执行成功后关闭待办",
            )
            Notification.objects.filter(
                property=property_obj, work_item=locked, resolved_at__isnull=True
            ).update(resolved_at=locked.completed_at)
            return locked, True
    except Exception as exc:
        # 内层 atomic 回滚会带走错误证据，必须在独立事务落库。
        message = str(exc)[:500]
        with transaction.atomic():
            WorkItem.objects.filter(pk=work_item.pk).update(
                last_error_code="SOURCE_SERVICE_FAILED",
                last_error_summary=message,
                updated_at=timezone.now(),
            )
        raise
