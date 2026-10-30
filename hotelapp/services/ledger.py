"""统一账本服务。

金额一律使用 Decimal，禁止 float 参与入账。
"""
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum
from django.utils import timezone

from ..models import LedgerEntry

CENT = Decimal("0.01")
CHARGE_TYPES = (LedgerEntry.EntryType.ROOM, LedgerEntry.EntryType.CHARGE)
PAYMENT_TYPES = (LedgerEntry.EntryType.PAYMENT, LedgerEntry.EntryType.DEPOSIT)


def money(value):
    if value in (None, ""):
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def post_entry(*, entry_type, amount, description, business_date=None, operator=None,
               accommodation=None, reservation=None, source_type="manual",
               source_id=None, reference=None, reverses=None):
    """幂等写入账本；reference 已存在时返回原记录。"""
    if reference:
        existing = LedgerEntry.objects.filter(reference=reference).first()
        if existing:
            return existing, False
    entry = LedgerEntry.objects.create(
        entry_type=entry_type,
        amount=money(amount),
        description=description,
        business_date=business_date or timezone.localdate(),
        operator=operator,
        accommodation=accommodation,
        reservation=reservation,
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        reference=reference,
        reverses=reverses,
    )
    return entry, True


def post_room_night(accommodation, stay_date, *, operator=None):
    """按「住宿单 + 住宿日」幂等入账一晚房费。

    夜审逐晚过租、退房时补记未过租的夜晚共用同一 reference，
    因此重复夜审或夜审后再退房都不会重复收房费。
    """
    price = money(accommodation.客房编号.价格)
    return post_entry(
        entry_type=LedgerEntry.EntryType.ROOM,
        amount=price,
        description=f"房费（订单{accommodation.id}，住宿日 {stay_date}）",
        business_date=stay_date,
        operator=operator,
        accommodation=accommodation,
        source_type="room_night",
        source_id=f"{accommodation.id}:{stay_date}",
        reference=f"stay:{accommodation.id}:{stay_date}",
    )


def _active(entries):
    """只统计有效凭证：被冲销的原凭证和冲销凭证都不参与余额计算。"""
    return entries.filter(is_void=False, reverses__isnull=True)


def _sum(entries, types):
    value = _active(entries).filter(entry_type__in=types).aggregate(
        total=Sum("amount"))["total"]
    return money(value)


def accommodation_summary(accommodation):
    entries = LedgerEntry.objects.filter(accommodation=accommodation)
    due = _sum(entries, CHARGE_TYPES)
    deposit = _sum(entries, (LedgerEntry.EntryType.DEPOSIT,))
    paid = _sum(entries, (LedgerEntry.EntryType.PAYMENT,))
    refunds = _sum(entries, (LedgerEntry.EntryType.REFUND,))
    balance = deposit + paid + refunds - due
    return {
        "entries": entries.order_by("id"),
        "due": due,
        "deposit": deposit,
        "paid": paid,
        "refunds": refunds,
        "balance": balance,
    }


def reservation_summary(reservation):
    entries = LedgerEntry.objects.filter(reservation=reservation)
    due = _sum(entries, CHARGE_TYPES)
    deposit = _sum(entries, (LedgerEntry.EntryType.DEPOSIT,))
    paid = _sum(entries, (LedgerEntry.EntryType.PAYMENT,))
    refunds = _sum(entries, (LedgerEntry.EntryType.REFUND,))
    balance = deposit + paid + refunds - due
    return {
        "entries": entries.order_by("id"),
        "due": due,
        "deposit": deposit,
        "paid": paid,
        "refunds": refunds,
        "balance": balance,
    }


def reverse_entry(entry, *, reason, operator=None, business_date=None):
    """红字反向凭证；原凭证标记 is_void，但保留在账上。"""
    if entry.is_void:
        return None, False
    reference = f"reversal:{entry.id}"
    reversal, created = post_entry(
        entry_type=LedgerEntry.EntryType.REVERSAL,
        amount=-money(entry.amount),
        description=f"冲账：{reason}（原凭证 #{entry.id}）",
        business_date=business_date or timezone.localdate(),
        operator=operator,
        accommodation=entry.accommodation,
        reservation=entry.reservation,
        source_type="reversal",
        source_id=str(entry.id),
        reference=reference,
        reverses=entry,
    )
    if created:
        entry.is_void = True
        entry.save(update_fields=["is_void"])
    return reversal, created


def void_accommodation(accommodation, *, reason, operator=None):
    """冲账整张房单：逐条红字冲销，不删除任何流水。"""
    reversals = []
    entries = accommodation.ledger_entries.filter(
        is_void=False, reverses__isnull=True).order_by("id")
    for entry in entries:
        reversal, created = reverse_entry(
            entry, reason=reason, operator=operator,
            business_date=timezone.localdate(),
        )
        if created:
            reversals.append(reversal)
    return reversals
