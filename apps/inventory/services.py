from datetime import timedelta

from django.db import transaction

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
        rate_plan = (
            room_type.property.rate_plans.filter(
                is_active=True,
                valid_from__lte=check_in,
            )
            .filter(models_q_valid_to(check_out - timedelta(days=1)))
            .order_by("valid_from", "id")
            .first()
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
