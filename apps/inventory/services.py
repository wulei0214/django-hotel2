from datetime import timedelta

from django.db import transaction
from django.db.models import Sum

from .models import DailyRate, RoomTypeInventory


class InventoryUnavailable(Exception):
    """The requested room type has insufficient inventory for at least one night."""


def stay_dates(check_in, check_out):
    if check_out <= check_in:
        raise ValueError("离店日期必须晚于入住日期。")
    current = check_in
    while current < check_out:
        yield current
        current += timedelta(days=1)


def models_q_valid_to(last_night):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=last_night)


def get_room_type_availability(property_obj, room_type, check_in, check_out, rooms=1, rate_plan=None):
    dates = list(stay_dates(check_in, check_out))
    if room_type.property_id != property_obj.pk or not room_type.is_active:
        raise ValueError("房型不存在或未启用。")

    if rate_plan is None:
        # 自动选择房价计划必须落在"该房型在住宿区间实际有价格行"的计划上；
        # 全局取第一个会导致只有碰巧挂在默认计划下的房型可售（其余房型恒 0 间）
        scoped = room_type.property.rate_plans.filter(
            is_active=True,
            valid_from__lte=check_in,
        ).filter(models_q_valid_to(check_out - timedelta(days=1)))
        rate_plan = (
            scoped.filter(
                daily_rates__room_type=room_type,
                daily_rates__stay_date__gte=check_in,
                daily_rates__stay_date__lt=check_out,
            )
            .order_by("valid_from", "id")
            .first()
            or scoped.order_by("valid_from", "id").first()
        )
    if (
        rate_plan is None
        or rate_plan.property_id != property_obj.pk
        or not rate_plan.is_active
        or rate_plan.valid_from > check_in
        or (rate_plan.valid_to and rate_plan.valid_to < dates[-1])
    ):
        raise ValueError("该日期区间没有可用的房价计划。")

    daily_rates = {
        rate.stay_date: rate
        for rate in DailyRate.objects.filter(
            property=property_obj,
            rate_plan=rate_plan,
            room_type=room_type,
            stay_date__gte=check_in,
            stay_date__lt=check_out,
        )
    }
    inventory = {
        row.stay_date: row
        for row in RoomTypeInventory.objects.filter(
            property=property_obj,
            room_type=room_type,
            stay_date__gte=check_in,
            stay_date__lt=check_out,
        )
    }

    currencies = {daily_rates[day].currency for day in dates if day in daily_rates}
    if len(currencies) > 1:
        raise ValueError("同一预订日期区间不能混用不同币种。")

    nightly_details = []
    available_counts = []
    for stay_date in dates:
        rate = daily_rates.get(stay_date)
        stock = inventory.get(stay_date)
        stay_length = len(dates)
        if (
            rate is None
            or rate.closed
            or stock is None
            or stock.stop_sell
            or stay_length < rate.min_stay
            or (rate.max_stay is not None and stay_length > rate.max_stay)
        ):
            return {
                "room_type_id": room_type.pk,
                "room_type_name": room_type.name,
                "rate_plan_id": rate_plan.pk,
                "available_count": 0,
                "total_amount": None,
                "nightly_details": [],
            }
        nightly_details.append(
            {"date": stay_date.isoformat(), "price": str(rate.price), "currency": rate.currency}
        )
        available_counts.append(stock.available_count)

    total_per_room = sum(
        (daily_rates[day].price for day in dates),
        start=daily_rates[dates[0]].price * 0,
    )
    return {
        "room_type_id": room_type.pk,
        "room_type_name": room_type.name,
        "rate_plan_id": rate_plan.pk,
        "available_count": min(available_counts) if available_counts else 0,
        "total_amount": str(total_per_room * rooms),
        "nightly_details": nightly_details,
    }


def _group_reservation_room_quantities(reservation):
    quantities = {}
    for line in reservation.reservation_rooms.values(
        "room_type_id", "check_in", "check_out"
    ):
        key = (line["room_type_id"], line["check_in"], line["check_out"])
        quantities[key] = quantities.get(key, 0) + 1
    return quantities


@transaction.atomic
def hold_inventory_for_reservation(reservation):
    """在同一事务中锁定每晚库存并增加 held。"""
    quantities = _group_reservation_room_quantities(reservation)

    for (room_type_id, check_in, check_out), quantity in sorted(
        quantities.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        dates = list(stay_dates(check_in, check_out))
        rows = list(
            RoomTypeInventory.objects.select_for_update()
            .filter(
                property=reservation.property,
                room_type_id=room_type_id,
                stay_date__in=dates,
            )
            .order_by("stay_date")
        )
        if len(rows) != len(dates):
            raise InventoryUnavailable("库存日历不完整，无法完成预订。")
        if any(row.stop_sell or row.available_count < quantity for row in rows):
            raise InventoryUnavailable("所选日期房量不足，请重新查询。")

        for row in rows:
            row.held += quantity
            row.version += 1
            row.save(update_fields=("held", "version", "updated_at"))


@transaction.atomic
def confirm_inventory_for_reservation(reservation):
    """支付成功时将 held 转为 sold；调用方需锁定预订记录。"""
    quantities = _group_reservation_room_quantities(reservation)

    for (room_type_id, check_in, check_out), quantity in sorted(
        quantities.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        dates = list(stay_dates(check_in, check_out))
        rows = list(
            RoomTypeInventory.objects.select_for_update()
            .filter(
                property=reservation.property,
                room_type_id=room_type_id,
                stay_date__in=dates,
            )
            .order_by("stay_date")
        )
        if len(rows) != len(dates) or any(row.held < quantity for row in rows):
            raise InventoryUnavailable("预订库存 Hold 已失效，不能确认订单。")
        for row in rows:
            row.held -= quantity
            row.sold += quantity
            row.version += 1
            row.save(update_fields=("held", "sold", "version", "updated_at"))


@transaction.atomic
def release_inventory_hold(reservation):
    """取消/过期时释放 Hold；重复调用由 Reservation 状态机保证幂等。"""
    quantities = _group_reservation_room_quantities(reservation)

    for (room_type_id, check_in, check_out), quantity in sorted(
        quantities.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        dates = list(stay_dates(check_in, check_out))
        rows = list(
            RoomTypeInventory.objects.select_for_update()
            .filter(
                property=reservation.property,
                room_type_id=room_type_id,
                stay_date__in=dates,
            )
            .order_by("stay_date")
        )
        if len(rows) != len(dates):
            raise InventoryUnavailable("库存日历不完整，无法释放预订 Hold。")
        if any(row.held < quantity for row in rows):
            raise InventoryUnavailable("库存 Hold 数量与预订状态不一致。")
        for row in rows:
            row.held -= quantity
            row.version += 1
            row.save(update_fields=("held", "version", "updated_at"))



@transaction.atomic
def release_sold_inventory_for_room_line(line, *, from_date):
    """No-Show 按房间明细释放尚未消耗的未来已售库存。"""
    dates = list(stay_dates(max(line.check_in, from_date), line.check_out))
    if not dates:
        return 0
    rows = list(
        RoomTypeInventory.objects.select_for_update()
        .filter(
            property=line.property,
            room_type_id=line.room_type_id,
            stay_date__in=dates,
        )
        .order_by("stay_date")
    )
    if len(rows) != len(dates):
        raise InventoryUnavailable("No-Show 对应库存日历不完整，不能安全释放库存。")
    if any(row.sold < 1 for row in rows):
        raise InventoryUnavailable("No-Show 对应已售库存不足，不能重复释放。")
    for row in rows:
        row.sold -= 1
        row.version += 1
        row.save(update_fields=("sold", "version", "updated_at"))
    return len(rows)


def adjust_daily_rate(*, property_obj, rate_id, price, reason,
                      updated_by=None, request=None):
    """调价唯一写入口：物业校验、价格精度、历史保护和审计。"""
    from decimal import Decimal, InvalidOperation
    from zoneinfo import ZoneInfo
    from django.db import transaction
    from django.utils import timezone
    from apps.core.models import AuditEvent
    from .models import DailyRate

    reason = str(reason or "").strip()
    if not reason or len(reason) > 240:
        raise ValueError("调价必须填写 1 至 240 个字符的原因。")
    try:
        amount = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("价格格式无效。") from exc
    if (not amount.is_finite() or amount < 0
            or amount != amount.quantize(Decimal("0.01"))):
        raise ValueError("价格必须是非负且精确到分的金额。")
    try:
        tz = ZoneInfo(property_obj.timezone or "Asia/Shanghai")
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    today = timezone.localdate(timezone.now(), tz)

    with transaction.atomic():
        rate = DailyRate.objects.select_for_update().filter(
            pk=rate_id, property=property_obj,
            rate_plan__property=property_obj,
            room_type__property=property_obj,
        ).first()
        if rate is None:
            raise ValueError("每日房价不存在或不属于当前物业。")
        if rate.closed:
            raise ValueError("该住宿日已关房，不能调整房价。")
        if rate.stay_date < today:
            raise ValueError("历史住宿日的房价不可修改。")
        before = rate.price
        if before == amount:
            return rate, False
        rate.price = amount
        rate.save(update_fields=("price", "updated_at"))
        AuditEvent.record(
            action="inventory.daily_rate_adjust",
            entity_type="DailyRate", entity_id=rate.pk,
            actor=updated_by, property_obj=property_obj,
            before={"price": str(before), "stay_date": rate.stay_date.isoformat()},
            after={"price": str(amount), "stay_date": rate.stay_date.isoformat()},
            reason=reason, request=request,
        )
        return rate, True
