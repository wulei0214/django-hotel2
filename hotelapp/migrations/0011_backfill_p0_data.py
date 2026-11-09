"""P0 数据回填：旧房单日期、房态三态、夜审营业日、旧双账迁移到统一账本。"""
import datetime
from decimal import Decimal, InvalidOperation

from django.db import migrations


def _date(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _decimal(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def backfill(apps, schema_editor):
    Room = apps.get_model("hotelapp", "Room")
    Accommodation = apps.get_model("hotelapp", "Accommodation")
    NightAudit = apps.get_model("hotelapp", "NightAudit")
    Folio = apps.get_model("hotelapp", "Folio")
    FinanceRecord = apps.get_model("hotelapp", "FinanceRecord")
    LedgerEntry = apps.get_model("hotelapp", "LedgerEntry")

    # 1) 房态三态回填
    for room in Room.objects.all():
        status = room.房间状态 or "空闲"
        if status == "维修中":
            room.占用状态, room.清洁状态, room.维修状态 = "空闲", "干净", "维修中"
        elif status == "已预订":
            room.占用状态, room.清洁状态, room.维修状态 = "已预订", "干净", "正常"
        elif status == "已入住":
            room.占用状态, room.清洁状态, room.维修状态 = "已入住", "干净", "正常"
        elif status in ("待清洁", "清洁中", "待检查"):
            room.占用状态, room.清洁状态, room.维修状态 = "空闲", status, "正常"
        else:
            room.占用状态, room.清洁状态, room.维修状态 = "空闲", "干净", "正常"
        room.房间状态 = status
        room.save(update_fields=["占用状态", "清洁状态", "维修状态", "房间状态"])

    # 2) 房单日期区间回填
    for item in Accommodation.objects.all():
        arrival = _date(item.入住日期) or _date(item.入住时间)
        departure = _date(item.离店日期)
        if arrival and not departure:
            departure = arrival + datetime.timedelta(days=1)
        item.入住日期 = arrival
        item.离店日期 = departure
        if arrival:
            item.入住时间 = arrival.strftime("%Y-%m-%d")
        if item.状态 == "已入住" and arrival and not item.实际入住时间:
            item.实际入住时间 = datetime.datetime.combine(arrival, datetime.time(14, 0))
        if item.状态 == "已退房" and departure and not item.实际退房时间:
            item.实际退房时间 = datetime.datetime.combine(departure, datetime.time(12, 0))
            item.退房时间 = departure.strftime("%Y-%m-%d")
        item.save(update_fields=["入住日期", "离店日期", "入住时间", "实际入住时间",
                                 "实际退房时间", "退房时间"])

    # 3) 夜审营业日：同一天只回填第一条，旧重复记录保留但营业日为空
    seen = set()
    for record in NightAudit.objects.order_by("id"):
        day = _date(record.营业日期)
        if day and day not in seen:
            record.营业日 = day
            seen.add(day)
        record.过租金额 = _decimal(record.过租总额)
        record.save(update_fields=["营业日", "过租金额"])

    # 4) 旧 Folio -> LedgerEntry
    folio_types = {"房费", "押金", "收款", "消费", "退款", "冲账"}
    for row in Folio.objects.order_by("id"):
        reference = f"legacy:folio:{row.id}"
        if LedgerEntry.objects.filter(reference=reference).exists():
            continue
        entry_type = row.类型 if row.类型 in folio_types else "消费"
        LedgerEntry.objects.create(
            entry_type=entry_type,
            amount=_decimal(row.金额),
            description=row.事由 or "历史账单流水",
            business_date=_date(row.日期) or datetime.date.today(),
            operator=row.经手人,
            accommodation_id=row.order_id,
            source_type="legacy_folio",
            source_id=str(row.order_id or row.id),
            reference=reference,
        )

    # 5) 旧 FinanceRecord -> LedgerEntry
    for row in FinanceRecord.objects.order_by("id"):
        reference = f"legacy:finance:{row.id}"
        if LedgerEntry.objects.filter(reference=reference).exists():
            continue
        entry_type = "收入" if row.类型 == "收入" else "支出"
        description = row.事由 or "历史财务流水"
        if row.关联订单:
            description = f"{description}（关联订单 {row.关联订单}）"
        LedgerEntry.objects.create(
            entry_type=entry_type,
            amount=_decimal(row.金额),
            description=description,
            business_date=_date(row.日期) or datetime.date.today(),
            operator=row.经手人,
            source_type="legacy_finance",
            source_id=str(row.id),
            reference=reference,
        )


def unbackfill(apps, schema_editor):
    Room = apps.get_model("hotelapp", "Room")
    Accommodation = apps.get_model("hotelapp", "Accommodation")
    NightAudit = apps.get_model("hotelapp", "NightAudit")
    LedgerEntry = apps.get_model("hotelapp", "LedgerEntry")

    LedgerEntry.objects.filter(reference__startswith="legacy:").delete()
    Room.objects.update(占用状态="空闲", 清洁状态="干净", 维修状态="正常")
    Accommodation.objects.update(入住日期=None, 离店日期=None,
                                实际入住时间=None, 实际退房时间=None)
    NightAudit.objects.update(营业日=None, 过租金额=None)


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0010_p0_hardening"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
