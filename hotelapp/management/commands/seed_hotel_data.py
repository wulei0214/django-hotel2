# -*- coding: utf-8 -*-
import random
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.models import Property
from apps.guests.models import Guest, GuestPhone
from apps.identity.models import (
    PropertyStaffMembership, StaffProfile, StaffRoleAssignment)
from apps.inventory.models import (
    DailyRate, RatePlan, Room, RoomType, RoomTypeInventory)
from apps.reservations.models import Reservation, ReservationRoom

FIRST = ["张伟","王芳","李娜","刘洋","陈静","杨帆","赵敏","黄磊","周婷","吴昊",
         "徐霞","孙浩","马丽","朱强","胡雪","郭涛","林芳","何军","高艳","罗成",
         "郑爽","梁红","谢娜","宋佳","唐磊","韩雪","冯刚","曹颖","彭飞","董洁"]
TYPES = [
    ("STD","标准间",2,"双床",[288,328,368]),
    ("SUP","高级大床房",2,"大床",[388,428,488]),
    ("DLX","豪华套房",3,"大床",[588,688,788]),
    ("FAM","家庭房",4,"一大两小",[488,538,588]),
    ("BUS","商务套房",2,"特大床",[688,788,888]),
]
STAFF = [
    ("李经理","值班经理","GM-DUTY"),("张前台","前台","FD-01"),
    ("王前台","前台","FD-02"),("赵财务","财务","FIN-01"),
    ("刘客房","客房服务员","HK-01"),("陈客房","客房服务员","HK-02"),
]
SOURCES = ["WEBSITE","WALK_IN","PHONE","OTA"]
STATUSES = ["PENDING","CONFIRMED","CHECKED_IN","CHECKED_OUT","CANCELLED","NO_SHOW"]


class Command(BaseCommand):
    help = "创建真实模拟酒店测试数据；幂等可重复执行"

    def add_arguments(self, parser):
        parser.add_argument("--rooms-per-type", type=int, default=4)
        parser.add_argument("--guests", type=int, default=30)
        parser.add_argument("--reservations", type=int, default=15)

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        random.seed(42)
        prop = Property.objects.filter(code="DEFAULT").first()
        if not prop:
            prop = Property.objects.create(code="DEFAULT", name="示例酒店")
        n = options["rooms_per_type"]

        types = {}
        for code, name, cap, bed, prices in TYPES:
            rt, _ = RoomType.objects.get_or_create(
                property=prop, code=code,
                defaults={"name": name, "capacity": cap, "bed_type": bed})
            types[code] = rt
            rp, _ = RatePlan.objects.get_or_create(
                property=prop, code=f"BAR_{code}",
                defaults={"name": name + " 门市价", "valid_from": date.today()})
            for i in range(n):
                num = str(random.randint(1001, 9999))
                Room.objects.get_or_create(
                    property=prop, number=num,
                    defaults={"room_type": rt, "floor": num[:1]})
                # 未来 30 天逐日房价 + 逐日可售库存（公开预订 /reserve/ 的数据源；
                # 此前库存表为空导致页面所有日期"剩余可订 0 间"）
                base_price = Decimal(str(prices[i % len(prices)]))
                for offset in range(30):
                    day = date.today() + timedelta(days=offset)
                    DailyRate.objects.get_or_create(
                        property=prop, rate_plan=rp, room_type=rt,
                        stay_date=day,
                        defaults={"price": base_price})
                room_count = options["rooms_per_type"]
                for offset in range(30):
                    day = date.today() + timedelta(days=offset)
                    RoomTypeInventory.objects.get_or_create(
                        property=prop, room_type=rt, stay_date=day,
                        defaults={"total_rooms": room_count})
        self.stdout.write("  OK 房型+房间+价格+30天库存")

        for name, role, emp_no in STAFF:
            username = "staff_" + emp_no.lower().replace("-", "_")
            user, created = User.objects.get_or_create(
                username=username, defaults={"first_name": name})
            if created:
                user.set_password("Staff@123456")
                user.is_active = True
                user.save()
            sp, _ = StaffProfile.objects.get_or_create(
                user=user, defaults={"full_name": name, "employment_status": "ACTIVE"})
            m, _ = PropertyStaffMembership.objects.get_or_create(
                property=prop, staff_profile=sp,
                defaults={"employee_no": emp_no, "position_code": role, "status": "ACTIVE"})
            from django.contrib.auth.models import Group
            grp = Group.objects.filter(name=role).first()
            if grp:
                StaffRoleAssignment.objects.get_or_create(
                    membership=m, group=grp, defaults={"starts_at": timezone.now()})
        self.stdout.write("  OK 员工")

        for i in range(options["guests"]):
            name = FIRST[i % len(FIRST)]
            phone = "138" + str(random.randint(10000000, 99999999))
            guest, _created = Guest.objects.get_or_create(
                property=prop, phone_normalized=phone,
                defaults={"full_name": name + "(" + str(i+1) + ")",
                          "phone": phone, "phone_normalized": phone})
            # V1.1 M-03：电话走 GuestPhone（列表脱敏列的数据来源）；对存量客人幂等补建
            from apps.guests.models import GuestPhone
            GuestPhone.objects.get_or_create(
                guest=guest, phone=phone,
                defaults={"phone_normalized": phone, "is_primary": True})
            # V1.1 M-16：约三分之一客人带有效会员关系（会员中心演示数据）；按 pk 幂等
            from apps.guests.models import GuestMembership
            if guest.pk % 3 == 0:
                GuestMembership.objects.get_or_create(
                    property=prop, guest=guest,
                    defaults={"member_no": "MEM" + prop.code + str(guest.pk).zfill(5),
                              "level_code": ["STANDARD", "ADVANCED", "ELITE"][(guest.pk // 3) % 3],
                              "status": "ACTIVE"})
        self.stdout.write("  OK 客人 " + str(Guest.objects.filter(property=prop).count()))

        gl = list(Guest.objects.filter(property=prop))
        rl = list(Room.objects.filter(property=prop))
        today = date.today()
        rp = RatePlan.objects.filter(property=prop).first()
        created = 0
        for i in range(options["reservations"]):
            bno = "BK" + today.strftime("%Y%m%d") + str(i+1).zfill(4)
            if Reservation.objects.filter(booking_no=bno).exists():
                continue
            status = STATUSES[i % len(STATUSES)]
            g = random.choice(gl)
            r = random.choice(rl)
            ci = today + timedelta(days=random.randint(-3, 14))
            co = ci + timedelta(days=random.randint(1, 3))
            amt = Decimal(str(random.choice([288, 388, 488, 588]))) * max(1, (co - ci).days)
            res = Reservation.objects.create(
                property=prop, booking_no=bno, guest=g, status=status,
                check_in=ci, check_out=co, room_count=1, total_amount=amt,
                source=random.choice(SOURCES))
            ReservationRoom.objects.create(
                property=prop, reservation=res, room_type=r.room_type,
                rate_plan=rp, check_in=ci, check_out=co, amount=amt,
                assigned_room=r if status in ("CHECKED_IN", "CHECKED_OUT") else None)
            created += 1
        total = Reservation.objects.filter(property=prop).count()
        self.stdout.write("  OK 预订新建 " + str(created) + " (总 " + str(total) + ")")
        # ---------- 今日运营快照（让主界面/前台有活数据；幂等） ----------
        from apps.reservations.models import ReservationRoom, Stay
        from apps.inventory.models import Room as SeedRoom
        today = date.today()
        now = timezone.now()
        # 1) 两单已确认且未入住 → 今日在住（分配房间 + Stay + 占用房态）
        for res in Reservation.objects.filter(
                property=prop, status="CONFIRMED",
                reservation_rooms__assigned_room__isnull=True).distinct()[:2]:
            rr = res.reservation_rooms.first()
            if rr is None:
                continue
            room = SeedRoom.objects.filter(
                property=prop, occupancy_status="VACANT",
                housekeeping_status="CLEAN").first()
            if room is None:
                break
            rr.assigned_room = room
            rr.save(update_fields=["assigned_room"])
            res.status = "CHECKED_IN"
            res.save(update_fields=["status"])
            Stay.objects.get_or_create(
                reservation_room=rr,
                defaults={"property": prop, "room": room,
                          "status": "IN_HOUSE", "actual_check_in": now})
            room.occupancy_status = "OCCUPIED"
            room.save(update_fields=["occupancy_status"])
        # 2) 两单待确认/已确认 → 今日预抵
        for res in Reservation.objects.filter(
                property=prop, status__in=["PENDING", "CONFIRMED"],
                check_in__gt=today).order_by("id")[:2]:
            res.check_in = today
            res.save(update_fields=["check_in"])
            res.reservation_rooms.update(check_in=today)
        # 3) 一单待确认 → 昨日未到（过期预警演示）
        overdue = Reservation.objects.filter(
            property=prop, status="PENDING", check_in__gt=today
        ).order_by("-id").first()
        if overdue is None:
            overdue = Reservation.objects.filter(
                property=prop, status="PENDING", check_in=today
            ).order_by("-id").first()
        if overdue is not None:
            overdue.check_in = today - timedelta(days=1)
            overdue.save(update_fields=["check_in"])
            overdue.reservation_rooms.update(
                check_in=today - timedelta(days=1))
        self.stdout.write("  OK 今日运营快照（在住/预抵/预警）")
        self.stdout.write(self.style.SUCCESS("真实模拟酒店测试数据创建完成"))
        # P1-10: LegacyRecordLink 填充
        from apps.core.models import LegacyRecordLink
        from hotelapp.models import Staff as LegacyStaff, Client as LegacyClient
        link_count = 0
        for ls in LegacyStaff.objects.all():
            from apps.identity.models import StaffProfile as SP
            sp = SP.objects.filter(legacy_staff=ls).first()
            if sp is None:
                sp = SP.objects.filter(full_name=ls.姓名).first()
                status = "MATCH_CANDIDATE" if sp else "UNREVIEWED"
            else:
                status = "LINKED"
            if sp is None:
                continue
            _, created = LegacyRecordLink.objects.get_or_create(
                property=prop, source_system="HOTELAPP_V1",
                source_model="Staff", source_pk=str(ls.员工id),
                defaults={"target_model": "identity.StaffProfile",
                          "target_pk": str(sp.pk), "mapping_status": status})
            if created:
                link_count += 1
        self.stdout.write(f'  ✓ LegacyRecordLink 填充 {link_count} 条')


class LegacyLinkFillMixin:
    """P1-10：填充 LegacyRecordLink（Staff→StaffProfile 映射）。"""

    def fill_legacy_links(self, prop):
        from apps.core.models import LegacyRecordLink
        from apps.identity.models import StaffProfile
        from hotelapp.models import Staff as LegacyStaff
        count = 0
        for ls in LegacyStaff.objects.all():
            profile = StaffProfile.objects.filter(legacy_staff=ls).first()
            if profile is None:
                # 按姓名匹配候选
                profile = StaffProfile.objects.filter(full_name=ls.姓名).first()
                status = "MATCH_CANDIDATE" if profile else "UNREVIEWED"
            else:
                status = "LINKED"
            if profile is None:
                continue
            _, created = LegacyRecordLink.objects.get_or_create(
                property=prop, source_system="HOTELAPP_V1",
                source_model="Staff", source_pk=str(ls.员工id),
                defaults={"target_model": "identity.StaffProfile",
                          "target_pk": str(profile.pk),
                          "mapping_status": status,
                          "match_method": "姓名匹配" if status == "MATCH_CANDIDATE" else "legacy_staff FK"})
            if created:
                count += 1
        # 客人映射
        from hotelapp.models import Client as LegacyClient
        for lc in LegacyClient.objects.all():
            if not lc.身份证号:
                continue
            _, created = LegacyRecordLink.objects.get_or_create(
                property=prop, source_system="HOTELAPP_V1",
                source_model="Client", source_pk=str(lc.顾客id),
                defaults={"target_model": "guests.Guest",
                          "target_pk": "待核验",
                          "mapping_status": "UNREVIEWED",
                          "match_method": "待核验（身份证需加密迁移）"})
            if created:
                count += 1
        return count
