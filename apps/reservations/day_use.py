"""日租（day-use）计费规则（V12-01 / TASK-01）。

业务边界（负责人裁定，写入 `docs/PMS2.0_当前版本` 需求与架构文档）：

* day-use 是**显式计费类型**，不得由"入住日=离店日"反推；过夜订单保持
  `check_out > check_in` 的全部既有语义。
* 价格来源唯一：`billing.DayUsePolicy`（物业×房型×生效区间），创建时把
  价格快照到 `ReservationRoom.day_use_price`，后续政策改动不追溯历史订单。
* 收入确认时点：**入住时一次性确认**房费（`ROOM_CHARGE`，凭证号
  `day-use:{stay.pk}`，全局唯一即幂等键）。夜审只处理过夜在住，不会对
  day-use 过租，因此不存在"0 晚 → 0 收入"的缺口。
* 超时：约定结束时间 + 宽限内不加收；超出后按小时向上取整计费，写入
  独立 `ADJUSTMENT` 凭证（`day-use-overtime:{stay.pk}`），不修改原房费。
* 缺失政策一律 fail-closed：不得静默 0 元，也不得回落到过夜房价。
"""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

MONEY_QUANT = Decimal("0.01")


class DayUseError(ValueError):
    """日租业务规则错误（入参/政策缺失/状态不允许）。

    继承 ``ValueError``：API 层按既有口径映射为 400/409，与
    ``BillingInputError`` 的处理链保持一致。
    """


def _property_tz(property_obj):
    try:
        return ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:  # pragma: no cover - 时区配置异常时兜底
        return ZoneInfo("Asia/Shanghai")


def _quantize(value):
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def resolve_day_use_policy(*, property_obj, room_type, day):
    """解析覆盖指定业务日期的日租政策。

    规则：`is_active` 且 `valid_from <= day <= valid_to` 的政策中取
    `valid_from` 最晚的一条（同日重复由唯一约束 `uq_dayuse_property_type_from`
    拒绝）。无政策 → `DayUseError`，调用方必须整体拒绝，不得 0 元成交。
    """
    from apps.billing.models import DayUsePolicy

    policy = (
        DayUsePolicy.objects.filter(
            property=property_obj,
            room_type=room_type,
            is_active=True,
            valid_from__lte=day,
            valid_to__gte=day,
        )
        .order_by("-valid_from", "-id")
        .first()
    )
    if policy is None:
        raise DayUseError(
            f"{day.isoformat()} 该房型未配置有效的日租价格政策，不能按日租办理；"
            "请先在「房价与库存」中维护日租政策。"
        )
    return policy


def day_use_window(*, policy, day, property_obj, end_override=None):
    """返回约定使用窗口 (start, end)，按物业时区解释。

    `end_override` 用于延长使用时间（前台登记的实际约定结束时间）；
    为空时取政策的 `end_time`。窗口跨越午夜视为配置错误（政策约束已要求
    `end_time > start_time`），此处仍做防御性校验。
    """
    tz = _property_tz(property_obj)
    start = datetime.combine(day, policy.start_time, tzinfo=tz)
    if end_override is not None:
        end = end_override
        if timezone.is_naive(end):
            end = end.replace(tzinfo=tz)
    else:
        end = datetime.combine(day, policy.end_time, tzinfo=tz)
    if end <= start:
        raise DayUseError("日租约定结束时间必须晚于开始时间。")
    return start, end


def overtime_charge(*, policy, planned_end, actual_out):
    """计算超时费：(超时小时数, 金额)。

    宽限期内（`planned_end + grace_minutes`）不计费；超出部分按小时
    向上取整乘以政策费率。返回 `(0, Decimal("0.00"))` 表示无超时费。
    """
    if actual_out is None:
        return 0, Decimal("0.00")
    if timezone.is_naive(actual_out) and timezone.is_aware(planned_end):
        actual_out = timezone.make_aware(actual_out, planned_end.tzinfo)
    deadline = planned_end + timedelta(minutes=int(policy.grace_minutes or 0))
    if actual_out <= deadline:
        return 0, Decimal("0.00")
    seconds = (actual_out - deadline).total_seconds()
    hours = math.ceil(seconds / 3600.0)
    rate = _quantize(policy.overtime_hourly_rate)
    if hours <= 0 or rate <= 0:
        return max(hours, 0), Decimal("0.00")
    return hours, _quantize(rate * hours)


def post_day_use_room_charge(
    *, property_obj, reservation, stay, folio, actor=None, business_date=None
):
    """入住时确认日租房费（强幂等）。

    金额取 `ReservationRoom.day_use_price` 快照（不读当前政策，避免政策
    调整追溯历史订单）；快照缺失说明订单未按日租链路创建，直接拒绝，
    不得回落到 `line.amount` 以外的不确定来源。
    """
    from apps.billing.models import LedgerEntry
    from apps.billing.services_v2 import BillingService

    line = stay.reservation_room
    price = line.day_use_price
    if price is None:
        raise DayUseError("日租订单缺少房费快照，不能确认收入；请核查订单创建链路。")
    price = _quantize(price)
    if price <= 0:
        raise DayUseError("日租房费快照必须大于零。")

    entry, created = BillingService.post_entry(
        property_obj=property_obj,
        folio=folio,
        stay=stay,
        entry_type=LedgerEntry.EntryType.ROOM_CHARGE,
        amount=price,
        currency=folio.currency,
        business_date=business_date or line.check_in,
        reference=f"day-use:{stay.pk}",
        description=(f"日租房费 {line.check_in.isoformat()}" f"（政策价快照，单次使用）"),
        posted_by=actor,
    )
    return entry, created


def occupancy_dates(*, check_in, check_out, billing_type):
    """计费类型对应的"占用营业日"集合。

    * NIGHT_STAY：`[check_in, check_out)`；
    * DAY_USE：仅 `check_in` 一天——当日使用当日退，但仍占用当日房量，
      否则同日可被重复售卖（超卖）且排房冲突检测失效。

    该函数是库存冻结/确认/释放与冲突扫描的唯一口径，禁止各调用点自行
    用 `check_out - check_in` 推导，避免 day-use 被算成 0 天而"凭空多出"房量。
    """
    if billing_type == "DAY_USE":
        if check_out != check_in:
            raise DayUseError("日租订单的离店日期必须等于入住日期。")
        return [check_in]
    if check_out <= check_in:
        raise DayUseError("过夜订单的离店日期必须晚于入住日期。")
    days = []
    current = check_in
    while current < check_out:
        days.append(current)
        current += timedelta(days=1)
    return days


def resolve_window_for_line(*, line, property_obj):
    """解析订单的日租政策与约定窗口：(policy, start, end)。

    `line.day_use_end_override` 优先（延长使用时间）；否则回落到创建当日的
    政策 `end_time`。政策已失效/删除时按同日历史政策兜底；都没有则返回
    `(None, None, None)`——调用方不得据此凭空计费。
    """
    day = line.check_in
    override = line.day_use_end_override
    from apps.billing.models import DayUsePolicy

    policy = (
        DayUsePolicy.objects.filter(
            property=property_obj,
            room_type_id=line.room_type_id,
            valid_from__lte=day,
            valid_to__gte=day,
        )
        .order_by("-valid_from", "-id")
        .first()
    )
    if policy is None:
        if override is None:
            return None, None, None
        tz = _property_tz(property_obj)
        start = datetime.combine(day, time(0, 0), tzinfo=tz)
        end = override if timezone.is_aware(override) else override.replace(tzinfo=tz)
        return None, start, end
    start, end = day_use_window(
        policy=policy, day=day, property_obj=property_obj, end_override=override
    )
    return policy, start, end


def post_overtime_charge(
    *, property_obj, stay, folio, actor=None, business_date=None, actual_out=None
):
    """退房时结算超时使用费（独立 ADJUSTMENT 凭证，可幂等重放）。

    原房费凭证是政策价，超时是另一项收费事实，不得改写原凭证金额。
    无政策/窗口/未超时 → 返回 `None`，不产生任何凭证。
    """
    from apps.billing.models import LedgerEntry
    from apps.billing.services_v2 import BillingService

    line = stay.reservation_room
    policy, _start, planned_end = resolve_window_for_line(line=line, property_obj=property_obj)
    if policy is None or planned_end is None:
        return None
    hours, amount = overtime_charge(
        policy=policy,
        planned_end=planned_end,
        actual_out=actual_out or timezone.now(),
    )
    if amount <= 0:
        return None
    entry, _created = BillingService.post_entry(
        property_obj=property_obj,
        folio=folio,
        stay=stay,
        entry_type=LedgerEntry.EntryType.ADJUSTMENT,
        amount=amount,
        currency=folio.currency,
        business_date=business_date or line.check_in,
        reference=f"day-use-overtime:{stay.pk}",
        description=f"日租超时使用费（{hours} 小时 × 政策费率）",
        posted_by=actor,
    )
    return entry


def convert_day_use_to_overnight(
    *, property_obj, reservation_room_id, new_check_out, actor=None, reason=""
):
    """DU-11（负责人裁定 2.5）：day-use 转过夜住宿的正式业务操作。

    重新校验库存/房价/排房/账单后再调整计费类型与住宿日期，**不得只改
    Folio 金额**：

    * 校验：仅 day-use 订单（显式类型，非同日推断）；状态限 CONFIRMED /
      CHECKED_IN（已退房不可转）；新离店日必须晚于今天（过夜至少住当晚）；
      理由必填（1-240 字符，审计追溯）。
    * 房价（WB-075 同口径 fail-closed）：为 [check_in, new_check_out) 每个
      夜晚取 DailyRate 快照生成 ``nightly_rates``——缺任一晚价格直接拒绝，
      不静默 0 元、不套用日租政策价。转换当晚的房费由**下一次夜审过租**
      按快照正常生成（day-use 政策价只覆盖日间时段，两者是独立计费事实）。
    * 库存：首日房量已由 day-use 占用（sold 已计），新增住宿日
      ``[tomorrow, new_check_out)`` 逐日行锁 ``sold += 1``（缺行/停售/满房
      拒绝），并重同步 CONFIRMED 冻结明细（RSV-01）。
    * 账单：Folio 保持 OPEN 不动——已入账的日租房费是独立事实；新增夜晚
      的房费凭证由夜审产生，余额守卫与退款负债逻辑沿用既有链路。
    * 排房：沿用已分配房间（在住延续），不做换房。
    * 审计：前后状态与理由写入 AuditEvent。
    """
    from django.db import transaction

    from apps.core.models import AuditEvent
    from apps.inventory.models import DailyRate, RoomTypeInventory
    from apps.inventory.services import (
        InventoryUnavailable,
        _property_today,
        sync_inventory_hold_details,
    )
    from apps.reservations.models import (
        InventoryHold,
        Reservation,
        ReservationRoom,
    )

    if not str(reason or "").strip() or len(reason.strip()) > 240:
        raise DayUseError("转换原因必填（1-240 字符）。")

    with transaction.atomic():
        line = (
            ReservationRoom.objects.select_for_update()
            .filter(pk=reservation_room_id, property=property_obj)
            .first()
        )
        if line is None:
            raise DayUseError("未找到该物业下的预订房间明细。")
        reservation = Reservation.objects.select_for_update().get(
            pk=line.reservation_id  # type: ignore[attr-defined]
        )
        if reservation.billing_type != Reservation.BillingType.DAY_USE:
            raise DayUseError(
                "仅 day-use 订单可转换为过夜住宿；过夜订单的提前离店走"
                "提前离店政策链路（裁定 2.4，不得反向转换）。"
            )
        if reservation.status not in (
            Reservation.Status.CONFIRMED,
            Reservation.Status.CHECKED_IN,
        ):
            raise DayUseError("当前订单状态不允许转换（已退房/已取消不可转）。")

        today = _property_today(property_obj)
        if not hasattr(new_check_out, "year"):
            raise DayUseError("new_check_out 必须是日期。")
        if new_check_out <= today:
            raise DayUseError("转换后的离店日期必须晚于今天（过夜至少住当晚）。")

        nights = []
        current = line.check_in
        line_room_type_id = line.room_type_id  # type: ignore[attr-defined]
        while current < new_check_out:
            rate_row = (
                DailyRate.objects.filter(
                    property=property_obj,
                    room_type_id=line_room_type_id,
                    stay_date=current,
                )
                .order_by("-id")
                .first()
            )
            if rate_row is None or rate_row.price is None:
                raise DayUseError(
                    f"缺少 {current.isoformat()} 的过夜房价快照，不能转换（WB-075 同口径 fail-closed）。"
                )
            nights.append({"date": current.isoformat(), "price": str(rate_row.price)})
            current += timedelta(days=1)
        nightly_sum = sum((Decimal(n["price"]) for n in nights), Decimal("0.00"))

        before = {
            "billing_type": reservation.billing_type,
            "check_out": line.check_out.isoformat(),
            "amount": str(line.amount),
            "total_amount": str(reservation.total_amount),
        }

        # 库存：新增住宿日 [check_in+1, new_check_out) 逐日 sold += 1。
        # 首日房量已由 day-use 占用（occupancy_dates 单日口径），不得重复计。
        added_dates = []
        probe = line.check_in + timedelta(days=1)
        while probe < new_check_out:
            added_dates.append(probe)
            probe += timedelta(days=1)
        if added_dates:
            inv_rows = list(
                RoomTypeInventory.objects.select_for_update()
                .filter(
                    property=property_obj,
                    room_type_id=line_room_type_id,
                    stay_date__in=added_dates,
                )
                .order_by("stay_date")
            )
            if len(inv_rows) != len(added_dates):
                raise InventoryUnavailable("库存日历不完整，不能转换为过夜住宿。")
            for inv_row in inv_rows:
                if inv_row.stop_sell or inv_row.sold >= inv_row.total_rooms:
                    raise InventoryUnavailable(
                        f"{inv_row.stay_date} 房量不足或已停售，不能转换为过夜住宿。"
                    )
            for inv_row in inv_rows:
                inv_row.sold += 1
                inv_row.version += 1
                inv_row.save(update_fields=("sold", "version", "updated_at"))

        line.check_out = new_check_out
        line.nightly_rates = nights
        line.amount = nightly_sum
        line.day_use_end_override = None
        line.save(
            update_fields=(
                "check_out",
                "nightly_rates",
                "amount",
                "day_use_end_override",
            )
        )
        reservation.billing_type = Reservation.BillingType.NIGHT_STAY
        reservation.check_out = new_check_out
        reservation.total_amount = reservation.total_amount + nightly_sum
        reservation.save(update_fields=("billing_type", "check_out", "total_amount", "updated_at"))

        # RSV-01：按新占用区间重同步 CONFIRMED 冻结明细（新日期建 CONFIRMED
        # 明细行，使库存对账的真值与计数一致）。
        sync_inventory_hold_details(
            property_obj=property_obj,
            lines=[line],
            target_status=InventoryHold.HoldStatus.CONFIRMED,
        )

        after = {
            "billing_type": reservation.billing_type,
            "check_out": line.check_out.isoformat(),
            "amount": str(line.amount),
            "total_amount": str(reservation.total_amount),
            "added_inventory_dates": [d.isoformat() for d in added_dates],
        }
        AuditEvent.record(
            action="reservations.day_use_converted_to_overnight",
            entity_type="ReservationRoom",
            entity_id=str(line.pk),
            actor=actor,
            property_obj=property_obj,
            before=before,
            after=after,
            reason=reason.strip(),
        )
        return line, reservation


__all__ = [
    "DayUseError",
    "convert_day_use_to_overnight",
    "day_use_window",
    "occupancy_dates",
    "overtime_charge",
    "post_day_use_room_charge",
    "post_overtime_charge",
    "resolve_day_use_policy",
    "resolve_window_for_line",
]
