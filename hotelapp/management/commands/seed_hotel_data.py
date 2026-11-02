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
from apps.inventory.models import DailyRate, RatePlan, Room, RoomType
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
                DailyRate.objects.get_or_create(
                    property=prop, rate_plan=rp, room_type=rt,
                    stay_date=date.today() + timedelta(days=i % 7),
                    defaults={"price": Decimal(str(random.choice(prices)))})
        self.stdout.write("  OK 房型+房间+价格")

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
            Guest.objects.get_or_create(
                property=prop, phone_normalized=phone,
                defaults={"full_name": name + "(" + str(i+1) + ")",
                          "phone": phone, "phone_normalized": phone})
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
        self.stdout.write(self.style.SUCCESS("真实模拟酒店测试数据创建完成"))
