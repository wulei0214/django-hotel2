import hashlib
import json
import secrets
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import Folio
from apps.guests.models import Guest, GuestPhone
from apps.inventory.models import RatePlan, RoomType
from apps.inventory.services import (
    InventoryUnavailable,
    get_room_type_availability,
    hold_inventory_for_reservation,
    models_q_valid_to,
    release_inventory_hold,
)
from .models import BookingAccessToken, Reservation, ReservationRoom


class ReservationConflict(Exception):
    pass


def normalize_phone(phone):
    value = "".join(char for char in str(phone or "") if char.isdigit())
    if not 6 <= len(value) <= 20:
        raise ValueError("请输入有效联系电话。")
    return value


def _fingerprint(payload):
    normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _get_rate_plan(property_obj, check_in, check_out, rate_plan_id=None):
    queryset = RatePlan.objects.filter(
        property=property_obj,
        is_active=True,
        valid_from__lte=check_in,
    ).filter(
        models_q_valid_to(check_out - timedelta(days=1))
    )
    if rate_plan_id is not None:
        queryset = queryset.filter(pk=rate_plan_id)
    plan = queryset.order_by("valid_from", "id").first()
    if plan is None:
        raise ValueError("没有覆盖所选入住日期的有效房价计划。")
    return plan


def _create_reservation(
    *,
    property_obj,
    payload,
    idempotency_key,
    fingerprint,
):
    existing = Reservation.objects.filter(
        property=property_obj,
        idempotency_key=idempotency_key,
    ).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ReservationConflict("相同 Idempotency-Key 不能用于不同的预订内容。")
        return existing, False, None  # 重放不重复暴露令牌（明文只在首次创建返回）

    # §6.4：同手机号同物业未支付 PENDING 上限（默认 2 笔）——
    # 幂等重放在此之前已返回，不受上限影响；超限不新增 Guest/Reservation/Folio
    from django.conf import settings as _settings
    pending_cap = int(getattr(_settings, "PUBLIC_PENDING_RESERVATION_CAP", 2))
    pending_count = Reservation.objects.filter(
        property=property_obj,
        guest__phone_normalized=normalize_phone(payload["phone"]),
        status=Reservation.Status.PENDING,
    ).count()
    if pending_count >= pending_cap:
        raise ReservationConflict(
            "该手机号存在过多未支付预订，请先完成支付或取消后再预订。")

    check_in = payload["check_in"]
    check_out = payload["check_out"]
    rooms = payload["rooms"]
    room_type = RoomType.objects.select_for_update().get(
        pk=payload["room_type_id"],
        property=property_obj,
        is_active=True,
    )
    if payload["adults"] + payload["children"] > room_type.capacity * rooms:
        raise ValueError("入住人数超过所选房型的最大容纳人数。")
    rate_plan = _get_rate_plan(
        property_obj, check_in, check_out, payload.get("rate_plan_id")
    )
    availability = get_room_type_availability(
        property_obj, room_type, check_in, check_out, rooms=rooms, rate_plan=rate_plan
    )
    if availability["available_count"] < rooms:
        raise InventoryUnavailable("所选日期房量不足，请重新查询。")
    if availability["total_amount"] is None:
        raise InventoryUnavailable("所选日期暂不可预订。")

    phone = normalize_phone(payload["phone"])
    # 手机号只用于检索，不把两个使用共享号码的人自动合并为同一自然人。
    shared_phone = GuestPhone.objects.filter(phone_normalized=phone).exists()
    if shared_phone:
        GuestPhone.objects.filter(phone_normalized=phone).update(
            shared_review_required=True)
    guest = Guest.objects.create(
        property=property_obj,
        full_name=payload["full_name"].strip(),
        phone=payload["phone"].strip(),
        phone_normalized=phone,
    )
    GuestPhone.objects.create(
        guest=guest, phone=payload["phone"].strip(),
        phone_normalized=phone, is_primary=True,
        shared_review_required=shared_phone, source="官网预订",
    )
    booking_no = "R" + secrets.token_hex(10).upper()
    amount = Decimal(availability["total_amount"])
    expires_at = timezone.now() + timedelta(minutes=15)

    reservation = Reservation.objects.create(
        property=property_obj,
        booking_no=booking_no,
        guest=guest,
        source="WEBSITE",
        status=Reservation.Status.PENDING,
        check_in=check_in,
        check_out=check_out,
        room_count=rooms,
        adult_count=payload["adults"],
        child_count=payload["children"],
        total_amount=amount,
        currency=availability["nightly_details"][0]["currency"],
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        expires_at=expires_at,
    )

    nightly_rates = availability["nightly_details"]
    per_room_amount = sum(
        (Decimal(item["price"]) for item in nightly_rates),
        start=Decimal("0.00"),
    )
    for _ in range(rooms):
        ReservationRoom.objects.create(
            property=property_obj,
            reservation=reservation,
            room_type=room_type,
            rate_plan=rate_plan,
            check_in=check_in,
            check_out=check_out,
            nightly_rates=nightly_rates,
            amount=per_room_amount,
        )

    Folio.objects.create(
        property=property_obj,
        reservation=reservation,
        guest=guest,
        currency=reservation.currency,
    )
    hold_inventory_for_reservation(reservation)
    # F-01（审计缺口登记册 P0）：访问令牌必须在创建事务内签发——
    # 事务外签发失败会产生"已扣库存但顾客拿不到令牌"的孤儿订单。
    access_token = BookingAccessToken.issue_for(reservation)
    return reservation, True, access_token


def create_reservation(
    *,
    property_obj,
    payload,
    idempotency_key,
):
    if property_obj is None:
        raise ValueError("当前酒店物业不可用。")
    if not idempotency_key or len(idempotency_key) > 128:
        raise ValueError("必须提供有效的 Idempotency-Key。")

    check_in = payload["check_in"]
    check_out = payload["check_out"]
    if check_out <= check_in:
        raise ValueError("离店日期必须晚于入住日期。")
    property_today = timezone.localdate(timezone.now(), ZoneInfo(property_obj.timezone))
    if check_in < property_today:
        raise ValueError("入住日期不能早于酒店当地今天。")
    if payload["rooms"] < 1 or payload["rooms"] > 10:
        raise ValueError("一次预订房间数必须在 1 至 10 间之间。")
    if payload["adults"] < 1 or payload["children"] < 0:
        raise ValueError("入住人数不正确。")
    if not payload["full_name"].strip():
        raise ValueError("入住人姓名不能为空。")
    normalize_phone(payload["phone"])

    fingerprint = _fingerprint(
        {
            "full_name": payload["full_name"].strip(),
            "phone": normalize_phone(payload["phone"]),
            "room_type_id": payload["room_type_id"],
            "rate_plan_id": payload.get("rate_plan_id"),
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "rooms": payload["rooms"],
            "adults": payload["adults"],
            "children": payload["children"],
        }
    )

    try:
        with transaction.atomic():
            return _create_reservation(
                property_obj=property_obj,
                payload=payload,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
    except IntegrityError:
        # 并发重复请求可能同时通过首次查询；唯一约束负责最终仲裁。
        existing = Reservation.objects.filter(
            property=property_obj,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            if existing.request_fingerprint != fingerprint:
                raise ReservationConflict("相同 Idempotency-Key 已用于其他预订内容。")
            return existing, False, None
        raise


@transaction.atomic
def cancel_pending_reservation(*, property_obj, booking_no, reservation_pk):
    """P0-08：取消凭 Booking Access Token（view 层已校验），按主键定位不再用 phone。"""
    try:
        reservation = (
            Reservation.objects.select_for_update()
            .select_related("guest")
            .get(
                property=property_obj,
                booking_no=booking_no,
                pk=reservation_pk,
            )
        )
    except Reservation.DoesNotExist as exc:
        raise ReservationConflict("未找到预订记录。") from exc

    if reservation.status == Reservation.Status.CANCELLED:
        return reservation, False
    if reservation.status == Reservation.Status.PENDING:
        try:
            release_inventory_hold(reservation)
        except InventoryUnavailable as exc:
            raise ReservationConflict(
                f"库存记录与预订状态不一致，暂不能取消：{exc}"
            ) from exc
        if reservation.expires_at and reservation.expires_at <= timezone.now():
            reservation.status = Reservation.Status.EXPIRED
        else:
            reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=("status", "updated_at"))
        return reservation, True
    if reservation.status == Reservation.Status.EXPIRED:
        return reservation, False
    raise ReservationConflict("当前预订状态不支持自助取消，请联系酒店前台。")
