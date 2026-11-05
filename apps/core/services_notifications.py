# -*- coding: utf-8 -*-
"""通知与待办中心——运营待办派生服务（基线 2.0 §4.6）。

首期口径（§4.6 规则 + 实施基线）：
  * 站内通知即可；短信、邮件、WhatsApp/第三方消息渠道暂缓；
  * 无 celery：每次调用 refresh_operational_todos() 实时重算"当前应处理集合"，
    与库内未销项待办做 upsert 对账——新增项创建、计数/文案变化更新、
    已解决项标记 is_read=True（从"待处理"视图自动消失）；
  * 去重：dedupe_key = "{todo_code}|{业务键}|g{岗位组id}"，
    (property, dedupe_key) 数据库唯一 → 重复事件/重复刷新不重复堆积（§4.6
    闭环验收"重复事件不重复生成"）；
  * 通知是提示不是业务状态权威：本服务只读源业务模型（Reservation/Room/
    ApprovalRecord/BreakGlassRecord），不写任何业务表；
  * 通知生成不落 AuditEvent（§4.6：通知非审计动作；AuditEvent 只记录
    高危业务写入）。

待办来源 → 文档 §4.6"首期待办来源"映射（首期覆盖口径）：
  * reservation.pending_confirm   待确认预订（PENDING 预订，逐单）；
  * reservation.arrival_today     今日预抵（check_in=营业日今天，逐单）；
  * reservation.departure_today   今日离店（在住且 check_out=今天，逐单）；
  * reservation.overdue           过期未到（check_in<今天仍 PENDING/CONFIRMED，逐单）；
  * housekeeping.cleaning_pending 待清洁房（DIRTY/CLEANING 汇总计数，单条）；
  * housekeeping.release_pending  待放行净房（INSPECTING 汇总计数，单条）；
  * approval.pending              待审批（ApprovalRecord PENDING，逐条——含退款
    待审批/营业日重开待审批等所有高危审批动作）；
  * audit.breakglass_review       break-glass 事后复核（USED 待复核，逐条）。

  文档清单中"夜审阻挡项 / 维修超时待验收 / 预订资料待补充 / 收银班次未关班 /
  邀请待激活"首期不派生（对应源模型/工作台尚未提供可执行闭环），后续按需扩展。
"""
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from apps.billing.models import ApprovalRecord
from apps.core.models import BreakGlassRecord, Notification
from apps.identity.models import Group
from apps.inventory.models import Room
from apps.reservations.models import Reservation

# 汇总类待办的固定业务键（单条/物业）
AGGREGATE_KEY = "all"

# 预订链路跳转：预订中心（列表/详情检索）与前台接待工作台（排房/No-Show 操作）
LINK_RESERVATIONS = "/manage/reservations/"
LINK_FRONT_DESK = "/checkin/"
LINK_HOUSEKEEPING = "/manage/housekeeping/"
LINK_NIGHT_AUDIT = "/manage/night-audit/"
LINK_FINANCE = "/manage/finance/"

# 岗位广播目标（按 ROLE_REGISTRY 既有岗位组名；组不存在时跳过该目标）
AUDIENCE_FRONT = ("前台", "值班经理")
AUDIENCE_HOUSEKEEPING = ("客房服务员", "值班经理")
AUDIENCE_RELEASE = ("值班经理",)          # 净房放行归管理者（room_release_ready 口径）
AUDIENCE_APPROVAL = ("值班经理", "店长")  # 高危审批人口径（refund_approve 等）
AUDIENCE_BREAKGLASS = ("值班经理", "店长")  # core.breakglass_review 持有者


def property_business_date(property_obj):
    """按物业时区取营业日"今天"（与 front_desk._business_date 同口径）。"""
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return timezone.localdate(timezone.now(), tz)


def _item(code, biz_key, category, severity, title, body, link,
          audiences, expires_at=None):
    """构造一条待办派生项（纯数据，不落库）。"""
    return {
        "code": code,
        "biz_key": biz_key,
        "category": category,
        "severity": severity,
        "title": str(title)[:120],
        "body": str(body)[:240],
        "link": link,
        "audiences": audiences,
        "expires_at": expires_at,
    }


def _collect_active_items(property_obj, today):
    """实时计算当前物业的"应处理集合"（只读源业务模型，不落库）。"""
    items = []

    # 1. 待确认预订：PENDING 预订逐单派生（预订状态权威仍是 Reservation 本身）
    pending = (
        Reservation.objects.filter(property=property_obj, status=Reservation.Status.PENDING)
        .select_related("guest").order_by("check_in", "id"))
    for reservation in pending:
        items.append(_item(
            "reservation.pending_confirm", reservation.booking_no,
            Notification.Category.RESERVATION, Notification.Severity.WARNING,
            f"待确认预订 {reservation.booking_no}",
            f"客人 {reservation.guest.full_name} 的预订待支付/待确认，"
            f"抵店 {reservation.check_in:%Y-%m-%d}，请跟进确认。",
            LINK_RESERVATIONS, AUDIENCE_FRONT))

    # 2. 今日预抵：check_in=今天 且仍处 PENDING/CONFIRMED（未入住）
    arrivals = (
        Reservation.objects.filter(
            property=property_obj, check_in=today,
            status__in=(Reservation.Status.PENDING, Reservation.Status.CONFIRMED))
        .select_related("guest").order_by("check_in", "id"))
    for reservation in arrivals:
        items.append(_item(
            "reservation.arrival_today", reservation.booking_no,
            Notification.Category.RESERVATION, Notification.Severity.INFO,
            f"今日预抵 {reservation.booking_no}",
            f"客人 {reservation.guest.full_name} 的预订今日（{today:%Y-%m-%d}）抵店，"
            f"请提前准备排房与接待。",
            LINK_FRONT_DESK, AUDIENCE_FRONT))

    # 3. 今日离店：在住（CHECKED_IN）且 check_out=今天
    departures = (
        Reservation.objects.filter(
            property=property_obj, check_out=today,
            status=Reservation.Status.CHECKED_IN)
        .select_related("guest").order_by("check_out", "id"))
    for reservation in departures:
        items.append(_item(
            "reservation.departure_today", reservation.booking_no,
            Notification.Category.RESERVATION, Notification.Severity.INFO,
            f"今日离店 {reservation.booking_no}",
            f"客人 {reservation.guest.full_name} 的预订今日（{today:%Y-%m-%d}）离店，"
            f"请准备退房与查房。",
            LINK_FRONT_DESK, AUDIENCE_FRONT))

    # 4. 过期未到：check_in 已过但仍未入住（PENDING/CONFIRMED）——需确认 No-Show
    overdue = (
        Reservation.objects.filter(
            property=property_obj, check_in__lt=today,
            status__in=(Reservation.Status.PENDING, Reservation.Status.CONFIRMED))
        .select_related("guest").order_by("check_in", "id"))
    for reservation in overdue:
        items.append(_item(
            "reservation.overdue", reservation.booking_no,
            Notification.Category.RESERVATION, Notification.Severity.CRITICAL,
            f"过期未到 {reservation.booking_no}",
            f"预订 {reservation.booking_no} 原定 "
            f"{reservation.check_in:%Y-%m-%d} 抵店至今未入住，"
            f"请与客人确认是否按未到店处理。",
            LINK_FRONT_DESK, AUDIENCE_FRONT))

    # 5. 待清洁房：DIRTY/CLEANING 汇总计数（单条，避免逐间刷屏）
    cleaning_count = Room.objects.filter(
        property=property_obj, is_active=True,
        housekeeping_status__in=(Room.HousekeepingStatus.DIRTY,
                                 Room.HousekeepingStatus.CLEANING)).count()
    if cleaning_count:
        items.append(_item(
            "housekeeping.cleaning_pending", AGGREGATE_KEY,
            Notification.Category.HOUSEKEEPING, Notification.Severity.WARNING,
            f"待清洁房 {cleaning_count} 间",
            f"当前有 {cleaning_count} 间客房待清洁（脏房/清洁中），请安排清洁。",
            LINK_HOUSEKEEPING, AUDIENCE_HOUSEKEEPING))

    # 6. 待放行：INSPECTING 汇总计数（净房放行=room_release_ready 管理者口径）
    inspecting_count = Room.objects.filter(
        property=property_obj, is_active=True,
        housekeeping_status=Room.HousekeepingStatus.INSPECTING).count()
    if inspecting_count:
        items.append(_item(
            "housekeeping.release_pending", AGGREGATE_KEY,
            Notification.Category.HOUSEKEEPING, Notification.Severity.WARNING,
            f"待放行净房 {inspecting_count} 间",
            f"当前有 {inspecting_count} 间客房待检查放行，请安排检查后放行净房。",
            LINK_HOUSEKEEPING, AUDIENCE_RELEASE))

    # 7. 待审批：ApprovalRecord PENDING 逐条（退款/夜审重开等高危双人控制）
    approvals = (ApprovalRecord.objects.filter(
        property=property_obj, status=ApprovalRecord.Status.PENDING)
        .order_by("created_at", "id"))
    for record in approvals:
        if record.action_code.startswith("night_audit"):
            link = LINK_NIGHT_AUDIT
        else:
            link = LINK_FINANCE
        items.append(_item(
            "approval.pending", str(record.pk),
            Notification.Category.APPROVAL, Notification.Severity.WARNING,
            f"待审批：{record.action_code}",
            f"{record.entity_type}#{record.entity_id} 的高危操作待审批：{record.reason}",
            link, AUDIENCE_APPROVAL))

    # 8. break-glass 事后复核：USED（已执行待复核）逐条
    break_glass = (BreakGlassRecord.objects.filter(
        property=property_obj, status=BreakGlassRecord.Status.USED)
        .select_related("business_date").order_by("created_at", "id"))
    for record in break_glass:
        items.append(_item(
            "audit.breakglass_review", str(record.pk),
            Notification.Category.AUDIT, Notification.Severity.CRITICAL,
            f"紧急重开待复核 {record.business_date.business_date:%Y-%m-%d}",
            "营业日紧急重开（break-glass）已执行，须独立事后复核。",
            LINK_NIGHT_AUDIT, AUDIENCE_BREAKGLASS))

    return items


def refresh_operational_todos(property_obj, now=None):
    """按源业务状态实时派生运营待办并 upsert（幂等）。

    返回对账摘要：{"active", "created", "updated", "resolved", "by_code"}。
    口径：
      * 活跃项逐条按 (property, dedupe_key) get_or_create；已存在则仅当
        标题/内容/级别变化时更新（不重置人工已读状态，避免已读项每次刷新
        反复变未读形成提醒风暴）；
      * 库内 dedupe_key 非空且不在活跃集合的未读通知 → 标记已读（自动消失）；
        dedupe_key 为空（人工/公告类）永不自动销项；
      * 全程事务；不写业务表、不产生 AuditEvent。
    """
    now = now or timezone.now()
    today = property_business_date(property_obj)
    items = _collect_active_items(property_obj, today)

    # 岗位广播目标：只投递给实际存在的岗位组（缺组不阻断其它来源）
    audience_names = {name for item in items for name in item["audiences"]}
    group_ids_by_name = dict(
        Group.objects.filter(name__in=audience_names)
        .values_list("name", "id"))

    summary = {"active": 0, "created": 0, "updated": 0, "resolved": 0,
               "by_code": {}}
    active_keys = set()

    with transaction.atomic():
        for item in items:
            for name in item["audiences"]:
                group_id = group_ids_by_name.get(name)
                if group_id is None:
                    continue
                dedupe_key = f"{item['code']}|{item['biz_key']}|g{group_id}"[:128]
                active_keys.add(dedupe_key)
                summary["by_code"][item["code"]] = (
                    summary["by_code"].get(item["code"], 0) + 1)
                notification, created = (
                    Notification.objects.get_or_create(
                        property=property_obj, dedupe_key=dedupe_key,
                        defaults={
                            "role_group_id": group_id,
                            "category": item["category"],
                            "severity": item["severity"],
                            "title": item["title"],
                            "body": item["body"],
                            "link": item["link"],
                            "expires_at": item["expires_at"],
                        }))
                if created:
                    summary["created"] += 1
                    continue
                changed_fields = []
                for field in ("title", "body", "severity", "expires_at"):
                    if getattr(notification, field) != item[field]:
                        setattr(notification, field, item[field])
                        changed_fields.append(field)
                if changed_fields:
                    notification.save(update_fields=changed_fields)
                    summary["updated"] += 1

        # 已解决项自动销项：仅限运营待办（dedupe_key 非空）且仍未读
        resolved = (Notification.objects
                    .filter(property=property_obj, is_read=False)
                    .exclude(dedupe_key__isnull=True)
                    .exclude(dedupe_key="")
                    .exclude(dedupe_key__in=active_keys))
        summary["resolved"] = resolved.update(is_read=True, read_at=now)
        summary["active"] = len(active_keys)

    return summary
