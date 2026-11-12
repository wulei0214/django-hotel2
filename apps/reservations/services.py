import hashlib
import json
import secrets
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.models import Folio
from apps.guests.services import GuestService, phone_hash
from apps.inventory.models import RatePlan, RoomType
from apps.inventory.services import (
    InventoryUnavailable,
    get_room_type_availability,
    hold_inventory_for_reservation,
    models_q_valid_to,
    release_inventory_hold,
)
from .models import BookingAccessToken, PendingBookingGate, Reservation, ReservationRoom


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
    # 幂等重放在此之前已返回，不受上限影响；超限不新增 Guest/Reservation/Folio。
    # WB-103：手机号查重改走 guest.phone_hash（HMAC），不再查询明文列。
    # WB-106：上限的"先读后写"必须有串行化保证——同（物业, phone_hash）的
    # 并发创建事务先在 PendingBookingGate 锁行上 get_or_create + 行锁排队，
    # 拿到行锁后再统计 PENDING 数，消除"并发双读同计数、双双通过上限"窗口；
    # 不同手机号之间互不阻塞。gate_key 与 guest.phone_hash 同源（HMAC），
    # 保证锁行粒度与统计口径完全一致。
    gate_key = phone_hash(normalize_phone(payload["phone"]))
    # WB-106 收尾：同手机号首次并发下单时两个事务可能同时建 gate 行，
    # 后者撞唯一约束。用 savepoint 隔离 IntegrityError 后重取已存在的锁行，
    # 保证两个请求都能进入同一把行锁排队，而不是一个整单失败。
    try:
        with transaction.atomic():
            gate = PendingBookingGate.objects.get_or_create(
                property=property_obj, gate_key=gate_key)[0]
    except IntegrityError:
        gate = PendingBookingGate.objects.get(
            property=property_obj, gate_key=gate_key)
    # 行锁：后续同手机号创建事务在此排队；首次创建无锁行竞争，直接通过。
    PendingBookingGate.objects.select_for_update().get(pk=gate.pk)

    from django.conf import settings as _settings
    pending_cap = int(getattr(_settings, "PUBLIC_PENDING_RESERVATION_CAP", 2))
    pending_count = Reservation.objects.filter(
        property=property_obj,
        guest__phone_hash=gate_key,
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
    # WB-103 收口说明：Guest/GuestPhone 创建统一委托 GuestService.create_guest
    # ——手机号在服务内经 encrypt_phone/phone_hash 密文+哈希落库，不再写
    # Guest.phone/phone_normalized 明文列；共享号码判定（M-03 规则 2，
    # 全局口径）与 shared_review_required 标记逻辑由服务层保持不变。
    # 传入的是本函数 normalize_phone（纯数字）结果，保证 pending 上限
    # 查询的 phone_hash 与建档 hash 完全一致。
    # 事务语义：本函数整体处于 create_reservation 的 transaction.atomic
    # 内（WB-09 同事务建档语义不变）；幂等重放在 _create_reservation
    # 开头按 idempotency_key 返回，不会重复创建 Guest。
    guest = GuestService.create_guest(
        property_obj=property_obj,
        full_name=payload["full_name"].strip(),
        phone=phone,
        source="官网预订")
    booking_no = "R" + secrets.token_hex(10).upper()
    amount = Decimal(availability["total_amount"])
    # RSV-07：与支付意图窗口共用同一时限常量，避免两处硬编码漂移。
    from apps.core.booking_constants import PENDING_RESERVATION_TTL_MINUTES
    expires_at = timezone.now() + timedelta(
        minutes=PENDING_RESERVATION_TTL_MINUTES)

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
        from apps.billing.services import cancel_open_payment_intents_for_reservation
        cancel_open_payment_intents_for_reservation(reservation)
        return reservation, True
    if reservation.status == Reservation.Status.EXPIRED:
        return reservation, False
    raise ReservationConflict("当前预订状态不支持自助取消，请联系酒店前台。")
