"""模块③批次 M3-2：E2/E3/E4 读路径切换（旧表直读 → 归档查询通道）新旧输出等价对比。

双口径（派发单 GOV-DISPATCH-M3M4M5-001 M3-2）：
* **本地空库恒等对比**——无数据时旧直读路径与归档通道取数结果逐字段恒等
  （含 HTTP 封套 meta 的确定性形状）；
* **造数对比**——旧表造数（Accommodation 覆盖状态七态 + 空状态、Room/Client/Folio
  各若干行，金额字符串多态、联系电话多态、红字负数冲账凭证），分别经「旧直读路径」
  （pre-M3-2 front_desk 逻辑冻结基线）与「归档通道」（legacy_archive_query 适配器
  函数）取数，断言输出 JSON 等价。

等价边界（实施记录 M3-2 设计边界登记）：
* 完全等价：legacy_id/booking_no 编号规则、状态、日期、金额（``_legacy_money`` 同
  口径；folio 凭证金额字符串原样含负数）、guest.id（=快照 client_id）、
  assigned_room.number（快照 room_id 即旧 ``room.房间号`` 主键）、电话脱敏
  （``_mask_phone`` 与通道 ``mask_phone`` 复合口径逐值等价）；
* 登记退化（差异白名单）：``guest.full_name`` →「历史住客」、``room_type.name`` →
  「历史房型」（归档快照未含 client/room 名目表，姓名与房型名不可解析）。

冻结基线：``_legacy_direct_read_dto`` 逐字段复制 pre-M3-2
``apps/api/front_desk.py`` 的 ``_legacy_reservation_dto``（基线 cdc262d 形态），
仅本测试文件消费——生产代码自本批次起不再保留旧表读取。
"""

import uuid
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.test import TestCase
from django.utils import timezone

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（AGENTS.md 会话纪律）。
from django.utils.crypto import get_random_string
from hotelapp.models import Accommodation as LegacyAccommodation
from hotelapp.models import Client as LegacyClient
from hotelapp.models import Folio as LegacyFolio
from hotelapp.models import Room as LegacyRoom
from hotelapp.models import Staff as LegacyStaff

from apps.api.front_desk import _legacy_money, _mask_phone
from apps.api.front_desk import (
    _legacy_reservation_dto_from_archive as _archive_dto,
)
from apps.billing.legacy_archive import archive_table, get_spec
from apps.billing.legacy_archive_query import (
    legacy_folio_rows,
    legacy_reservation_row,
    legacy_reservation_rows,
)
from apps.billing.models import LegacyAccommodationArchive, LegacyMigrationBatch
from apps.core.models import AuditEvent, Property
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

_TEST_PW_EQ64A2C9 = get_random_string(24)

# 旧订单状态机七态（hotelapp.models.ORDER_STATUS）+ 空状态 = 造数覆盖面。
_SEVEN_STATES = ("待确认", "已确认", "已入住", "已退房", "已取消", "未到", "已冲账")

# 新旧输出允许出现的差异路径（= 实施记录登记的归档快照名目退化，不得扩大）。
_ALLOWED_DIFF_PATHS = {"guest.full_name", "room_type.name", "rooms[0].room_type.name"}

_FIRST_ORDER_ID = 950001


def _legacy_direct_read_dto(item):
    """pre-M3-2 旧直读路径冻结基线：逐字段复制原 ``_legacy_reservation_dto``。

    来源：git show cdc262d:apps/api/front_desk.py（``_legacy_reservation_dto``）。
    本函数是「旧直读路径取数」的等价对照物，禁止随生产代码演进修改。
    """
    guest = item.顾客id
    room = item.客房编号
    return {
        "booking_no": f"LEGACY-{item.pk}",
        "legacy_id": item.pk,
        "status": item.状态 or "历史记录",
        "status_label": item.状态 or "历史记录",
        "source": "LEGACY",
        "source_label": "历史 hotelapp 订单",
        "guest": {
            "id": guest.pk if guest else None,
            "full_name": (guest.姓名 if guest else "") or "历史住客",
            "phone_masked": _mask_phone(item.联系电话),
        },
        "check_in": _iso_date(item.入住时间),
        "check_out": _iso_date(item.退房时间),
        "room_count": 1,
        "adult_count": None,
        "child_count": None,
        "total_amount": None,
        "currency": "CNY",
        "rooms": [
            {
                "id": None,
                "room_type": {"name": room.客房类型 if room else "历史房型"},
                "assigned_room": {"number": str(room.房间号)} if room else None,
            }
        ],
        "room_type": {"name": room.客房类型 if room else "历史房型"},
        "assigned_room": {"number": str(room.房间号)} if room else None,
        "legacy_paid_amount": _legacy_money(item.收款金额),
        "legacy_deposit_amount": _legacy_money(item.押金),
        "folio_summary": {"status": "LEGACY_ARCHIVE", "currency": "CNY"},
        "source_system": "LEGACY",
        "read_only": True,
    }


def _iso_date(value):
    """pre-M3-2 ``_legacy_date`` 冻结副本（front_desk 同名逻辑，语义不变）。"""
    from datetime import date as _date

    value = str(value or "").strip()
    if not value:
        return None
    try:
        return _date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return value


def _json_diff_paths(old, new, prefix=""):
    """递归收集 JSON 结构差异路径（等价对比报告口径）。"""
    diffs = set()
    if isinstance(old, dict) and isinstance(new, dict):
        for key in set(old) | set(new):
            child = f"{prefix}.{key}" if prefix else str(key)
            diffs |= _json_diff_paths(old.get(key), new.get(key), child)
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            diffs.add(f"{prefix}#len")
        for index, (o, n) in enumerate(zip(old, new)):
            diffs |= _json_diff_paths(o, n, f"{prefix}[{index}]")
    elif old != new:
        diffs.add(prefix)
    return diffs


class LegacyEquivalenceBase(TestCase):
    """DEFAULT 物业 + 前台岗位；旧表造数 + 归档逐测试重建（事务回滚语义安全）。"""

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.user = User.objects.create_user(username="m3-equiv-user", password=_TEST_PW_EQ64A2C9)
        profile = StaffProfile.objects.create(
            user=self.user,
            full_name="等价对比员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            property=self.property,
            staff_profile=profile,
            employee_no="M3E-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        role = Group.objects.get(name="前台")
        role.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="hotelapp", codename="view_accommodation"
            )
        )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        self.front = self.user
        self._seed_legacy_tables()
        self._archive()

    def _seed_legacy_tables(self):
        """旧表造数：状态七态 + 空状态 × 金额/电话/日期多态 + 红字冲账凭证。"""
        staffer = LegacyStaff.objects.create(姓名="等价保洁", 职务="服务员", 月薪="3000", 工龄="1")
        self.rooms = [
            LegacyRoom.objects.create(
                房间号=9401 + index,
                客房类型="商务大床房",
                价格=388,
                负责人编号=staffer.pk,
                房间状态="空闲",
            )
            for index in range(2)
        ]
        self.clients = [
            LegacyClient.objects.create(
                姓名=f"等价住客{index}", 身份证号=f"11010119880101{index:04d}"
            )
            for index in range(2)
        ]
        money_series = ("300", "688.5", "0", "12.345", "abc", None, "", "99999.99")
        phone_series = (
            "13812345678",
            "123",
            None,
            "",
            "0571-888",
            "0755-83153288",
            None,
            "19900001111",
        )
        deposit_series = ("0.5", "10.5", None, "30.5", "40.5", "50.5", "60.5", "70.5")
        self.orders = []
        for index, status in enumerate((*_SEVEN_STATES, None)):
            self.orders.append(
                LegacyAccommodation.objects.create(
                    id=_FIRST_ORDER_ID + index,
                    顾客id=self.clients[index % 2],
                    客房编号=self.rooms[index % 2],
                    状态=status,
                    # 2026-10-1 为非法日期字符串：旧口径"非 ISO 原样返回"，双路径同函数等价。
                    入住时间="2026-10-1" if index == 3 else f"2026-10-{index + 1:02d}",
                    退房时间=None if index % 3 == 0 else f"2026-10-{index + 2:02d}",
                    收款金额=money_series[index],
                    押金=deposit_series[index],
                    联系电话=phone_series[index],
                )
            )
        # 红字冲账 + 空经手人 + 多类型凭证（金额字符串原样口径）。
        LegacyFolio.objects.create(
            order=self.orders[0],
            类型="押金",
            金额="200.00",
            事由="等价押金",
            日期="2026-10-01",
            经手人="等价核验",
        )
        LegacyFolio.objects.create(
            order=self.orders[0],
            类型="房费",
            金额="300",
            事由="等价房费",
            日期="2026-10-01",
            经手人=None,
        )
        LegacyFolio.objects.create(
            order=self.orders[0],
            类型="消费",
            金额="45.5",
            事由="等价消费",
            日期="2026-10-02",
            经手人="等价核验",
        )
        LegacyFolio.objects.create(
            order=self.orders[0],
            类型="冲账",
            金额="-100.00",
            事由="等价红字冲账",
            日期="2026-10-03",
            经手人=None,
        )
        LegacyFolio.objects.create(
            order=self.orders[1],
            类型="收款",
            金额="688.5",
            事由="等价收款",
            日期="2026-10-02",
            经手人="等价核验",
        )

    def _archive(self, specs=("Accommodation", "Folio"), batch_uid=None):
        """把当前旧表内容整体归档（独立批次；归档行主键=源主键，同一行不可二次归档）。"""
        uid = batch_uid or f"M3E-{uuid.uuid4().hex[:12]}"
        for name in specs:
            archive_table(spec=get_spec(name), batch_uid=uid, operator="等价对比")
        return uid

    def _baseline_dtos_by_id(self):
        """旧直读路径取数：冻结基线 DTO，按 legacy_id 索引。"""
        return {
            item.pk: _legacy_direct_read_dto(item)
            for item in LegacyAccommodation.objects.order_by("-id")
        }


class EmptyDatabaseIdentityTests(LegacyEquivalenceBase):
    """口径一：本地空库恒等对比——空库下旧直读路径与归档通道输出恒等。"""

    def setUp(self):
        # 空库口径：跳过基类造数，仅保留账号（各测试自行建库面）。
        self.property = Property.objects.get(code="DEFAULT")
        self.user = User.objects.create_user(username="m3-equiv-empty", password=_TEST_PW_EQ64A2C9)
        profile = StaffProfile.objects.create(
            user=self.user,
            full_name="空库对比员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        membership = PropertyStaffMembership.objects.create(
            property=self.property,
            staff_profile=profile,
            employee_no="M3E-002",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        role = Group.objects.get(name="前台")
        role.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="hotelapp", codename="view_accommodation"
            )
        )
        StaffRoleAssignment.objects.create(
            membership=membership,
            group=role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        self.front = self.user

    def test_row_level_empty_identity(self):
        self.assertEqual(list(LegacyAccommodation.objects.order_by("-id")), [])
        rows, meta = legacy_reservation_rows()
        self.assertEqual(rows, [])
        self.assertEqual(
            meta,
            {"page": 1, "page_size": 20, "total": 0, "has_previous": False, "has_next": False},
        )
        self.assertIsNone(legacy_reservation_row(legacy_id=1))
        self.assertEqual(legacy_folio_rows(legacy_id=1), [])

    def test_http_envelope_empty_identity(self):
        """切换前空库响应（旧直读 + _paginate + LEGACY 元数据）逐字段恒等。"""
        self.client.force_login(self.front)
        response = self.client.get("/api/v1/front-desk/legacy-reservations")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "data": [],
                "meta": {
                    "page": 1,
                    "page_size": 20,
                    "total": 0,
                    "has_previous": False,
                    "has_next": False,
                    "source_system": "LEGACY",
                    "read_only": True,
                },
            },
        )
        missing = self.client.get("/api/v1/front-desk/legacy-reservations/1")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.json(),
            {"error": {"code": "RESOURCE_NOT_FOUND", "message": "未找到历史订单。"}},
        )


class SeededEquivalenceTests(LegacyEquivalenceBase):
    """口径二：造数对比——状态七态 × 旧字段多态，新旧取数输出 JSON 等价。"""

    def test_state_seven_states_and_fields_equivalent(self):
        """状态七态（+空状态）× 旧字段：行级 diff ⊆ 登记退化白名单，关键等价点逐项命中。"""
        baseline = self._baseline_dtos_by_id()
        rows, meta = legacy_reservation_rows(page_size=100)
        self.assertEqual(meta["total"], len(baseline))
        self.assertEqual({row["id"] for row in rows}, set(baseline))
        seen_degradation = set()
        for row in rows:
            switched = _archive_dto(row)
            old = baseline[row["id"]]
            self.assertEqual(set(switched), set(old), f"键结构漂移 legacy_id={row['id']}")
            diffs = _json_diff_paths(old, switched)
            self.assertTrue(
                diffs <= _ALLOWED_DIFF_PATHS,
                f"出现白名单外差异 legacy_id={row['id']}：{sorted(diffs - _ALLOWED_DIFF_PATHS)}",
            )
            seen_degradation |= diffs
            # 等价关键点显式断言（不依赖 diff 收集的实现细节）。
            self.assertEqual(switched["booking_no"], f"LEGACY-{row['id']}")
            self.assertEqual(switched["guest"]["id"], old["guest"]["id"])
            self.assertEqual(switched["guest"]["phone_masked"], old["guest"]["phone_masked"])
            self.assertEqual(
                switched["rooms"][0]["assigned_room"], old["rooms"][0]["assigned_room"]
            )
            self.assertEqual(switched["assigned_room"], old["assigned_room"])
            self.assertEqual(switched["legacy_paid_amount"], old["legacy_paid_amount"])
            self.assertEqual(switched["legacy_deposit_amount"], old["legacy_deposit_amount"])
            self.assertEqual(switched["check_in"], old["check_in"])
            self.assertEqual(switched["check_out"], old["check_out"])
        # 登记退化在实际数据上确有发生（姓名/房型名兜底都被触发）。
        self.assertEqual(
            seen_degradation,
            _ALLOWED_DIFF_PATHS,
            "登记的退化路径未全部触发——造数覆盖不足或退化口径漂移",
        )

    def test_state_values_survive_archive_exactly(self):
        """状态七态原文 + 空状态经归档后逐行恒等（含 LEGACY-{pk} 编号规则）。"""
        rows, _ = legacy_reservation_rows(page_size=100)
        by_id = {row["id"]: row for row in rows}
        for index, status in enumerate((*_SEVEN_STATES, None)):
            row = by_id[_FIRST_ORDER_ID + index]
            self.assertEqual(row["status"], status)
            dto = _archive_dto(row)
            self.assertEqual(dto["status"], status or "历史记录")
            self.assertEqual(dto["booking_no"], f"LEGACY-{_FIRST_ORDER_ID + index}")

    def test_phone_masking_equivalence_all_variants(self):
        """脱敏字段：旧 ``_mask_phone`` 与「通道掩码 + 空值→—」复合口径逐值等价。"""
        baseline = self._baseline_dtos_by_id()
        rows, _ = legacy_reservation_rows(page_size=100)
        by_id = {row["id"]: row for row in rows}
        for legacy_id, row in by_id.items():
            self.assertEqual(
                _archive_dto(row)["guest"]["phone_masked"],
                baseline[legacy_id]["guest"]["phone_masked"],
                f"电话脱敏口径漂移 legacy_id={legacy_id}",
            )
        # 空值/短号/带分隔符长号逐一钉住（<8 位数字串不还原明文的 WB-46-08 口径）。
        masked = {_archive_dto(row)["guest"]["phone_masked"] for row in rows}
        self.assertIn("—", masked)
        self.assertIn("已隐藏", masked)
        self.assertIn("138****5678", masked)
        self.assertIn("075****3288", masked)

    def test_http_list_output_equivalent_to_direct_read_baseline(self):
        """端到端：HTTP 响应 data 行与旧直读基线逐行 diff ⊆ 白名单。"""
        baseline = self._baseline_dtos_by_id()
        self.client.force_login(self.front)
        response = self.client.get(
            "/api/v1/front-desk/legacy-reservations",
            {"page_size": "100"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data), len(baseline))
        for item in data:
            diffs = _json_diff_paths(baseline[item["legacy_id"]], item)
            self.assertTrue(
                diffs <= _ALLOWED_DIFF_PATHS,
                f"HTTP 输出白名单外差异 legacy_id={item['legacy_id']}：{sorted(diffs)}",
            )

    def test_detail_folio_entries_amount_strings_unchanged(self):
        """E4 详情：旧凭证金额字符串原样（含红字负数）、经手人空值、字段逐项等价。"""
        order = self.orders[0]
        old_entries = [
            {
                "id": entry.pk,
                "entry_type": entry.类型,
                "amount_raw": entry.金额,
                "business_date": _iso_date(entry.日期),
                "description": entry.事由,
                "operator": entry.经手人,
                "source_system": "LEGACY",
            }
            for entry in LegacyFolio.objects.filter(order=order).order_by("id")
        ]
        new_entries = [
            {
                "id": row["id"],
                "entry_type": row["entry_type"],
                "amount_raw": row["amount_text"],
                "business_date": _iso_date(row["date_text"]),
                "description": row["subject"],
                "operator": row["handler"],
                "source_system": "LEGACY",
            }
            for row in legacy_folio_rows(legacy_id=order.pk)
        ]
        self.assertEqual(new_entries, old_entries)
        self.assertIn(
            {
                "id": new_entries[3]["id"],
                "entry_type": "冲账",
                "amount_raw": "-100.00",
                "business_date": "2026-10-03",
                "description": "等价红字冲账",
                "operator": None,
                "source_system": "LEGACY",
            },
            new_entries,
            "红字冲账凭证金额字符串必须原样保留",
        )
        # HTTP 详情主行 + legacy_entries 结构与旧口径一致。
        self.client.force_login(self.front)
        response = self.client.get(f"/api/v1/front-desk/legacy-reservations/{order.pk}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["legacy_entries"], new_entries)
        self.assertEqual(
            payload["legacy_note"],
            "历史账单来自旧 hotelapp.Folio，仅按原始凭证展示，不与 PMS2 Folio 汇总或对账。",
        )
        diffs = _json_diff_paths(
            _legacy_direct_read_dto(order),
            {
                key: value
                for key, value in payload.items()
                if key not in ("legacy_entries", "legacy_note", "timeline")
            },
        )
        self.assertTrue(diffs <= _ALLOWED_DIFF_PATHS, sorted(diffs))

    def test_search_phone_and_id_match_name_degrades(self):
        """搜索等价：电话/旧订单 id 命中新旧一致；姓名搜索按登记口径降级不命中。"""
        # 电话尾号命中（旧路径同样按联系电话 icontains）。
        rows, _ = legacy_reservation_rows(search="5678")
        old_hits = {
            item.pk for item in LegacyAccommodation.objects.filter(联系电话__icontains="5678")
        }
        self.assertEqual({row["id"] for row in rows}, old_hits)
        # 纯数字旧订单 id 精确命中。
        rows, _ = legacy_reservation_rows(search=str(_FIRST_ORDER_ID + 1))
        self.assertEqual([row["id"] for row in rows], [_FIRST_ORDER_ID + 1])
        # 姓名搜索：旧直读可命中 → 归档快照无 client 名目表 → 不命中（登记降级）。
        self.assertTrue(
            LegacyAccommodation.objects.filter(顾客id__姓名__icontains="等价住客").exists()
        )
        rows, meta = legacy_reservation_rows(search="等价住客")
        self.assertEqual(rows, [])
        self.assertEqual(meta["total"], 0)

    def test_status_filter_equivalence(self):
        """状态筛选：归档通道与旧直读同一筛选语义（原文精确匹配，空状态不被命中）。"""
        rows, _ = legacy_reservation_rows(status="已入住")
        old_hits = list(
            LegacyAccommodation.objects.filter(状态="已入住")
            .order_by("-id")
            .values_list("id", flat=True)
        )
        self.assertEqual([row["id"] for row in rows], old_hits)
        rows, _ = legacy_reservation_rows(status="不存在的状态")
        self.assertEqual(rows, [])

    def test_no_property_filter_across_batches(self):
        """D3-3 甲：适配器不加物业过滤——不同物业快照的归档行同返回。

        归档行主键=源主键，同一源行全局只归档一次（服务层幂等口径），基线造数
        已占用 accommodation 批次，故本用例以直插快照行模拟"多批次并存"：
        适配器查询跨批次、不经 LegacyArchiveRun，只认 legacy_accommodation_archive
        行本身；property 快照差异（DEFAULT/NULL）不得改变可见性。
        """
        operator = User.objects.create_user(username="m3-equiv-op", password=_TEST_PW_EQ64A2C9)
        LegacyMigrationBatch.objects.create(
            batch_uid="M3E-REGISTERED-PROP",
            scope_property=self.property,
            scope_source_models=["Accommodation"],
            scope_record_range={"id": [960001]},
            executed_by=operator,
        )
        # 已登记批次快照行：property=DEFAULT。
        LegacyAccommodationArchive(
            id=960001,
            client_id=self.clients[0].pk,
            room_id=self.rooms[0].pk,
            check_in_text="2026-10-20",
            status="已入住",
            contact_phone="13900001111",
            received_amount_text=None,
            deposit_text=None,
            check_out_text=None,
            reversed_by=None,
            reversed_at_text=None,
            archive_batch="M3E-REGISTERED-PROP",
            property=self.property,
        ).save()
        # 未登记来源批次快照行：property=NULL。
        LegacyAccommodationArchive(
            id=960002,
            client_id=self.clients[1].pk,
            room_id=self.rooms[1].pk,
            check_in_text="2026-10-21",
            status="已退房",
            contact_phone=None,
            received_amount_text=None,
            deposit_text=None,
            check_out_text=None,
            reversed_by=None,
            reversed_at_text=None,
            archive_batch="M3E-UNREGISTERED",
        ).save()
        rows, meta = legacy_reservation_rows(page_size=100)
        self.assertEqual(meta["total"], len(self.orders) + 2)
        self.assertEqual(
            {row["id"] for row in rows} & {960001, 960002},
            {960001, 960002},
            "不同物业快照（DEFAULT/NULL）的归档行都不得被适配器过滤",
        )

    def test_show_accomodation_rows_equivalent_to_direct_read(self):
        """E2 页面：归档行包装形状与旧直读模型逐字段恒等（房号=room_id 主键等价）。"""
        old_by_id = {item.pk: item for item in LegacyAccommodation.objects.all()}
        self.client.force_login(self.front)
        response = self.client.get("/show_accomodation/")
        self.assertEqual(response.status_code, 200)
        people = list(response.context["people_list"])
        self.assertEqual(len(people), len(old_by_id))
        for wrapped in people:
            old = old_by_id[wrapped.id]
            self.assertEqual(wrapped.状态, old.状态)
            self.assertEqual(wrapped.联系电话, old.联系电话)
            self.assertEqual(wrapped.入住时间, old.入住时间)
            self.assertEqual(wrapped.退房时间, old.退房时间)
            self.assertEqual(wrapped.收款金额, old.收款金额)
            self.assertEqual(wrapped.顾客id.顾客id, old.顾客id_id)  # type: ignore[attr-defined]  # wrapped=归档行+注入形状（views._legacy_archive_row_wrapper），非模型声明属性
            self.assertEqual(wrapped.客房编号.房间号, old.客房编号_id)  # type: ignore[attr-defined]
        # 模板层 mask_tel 脱敏口径保持（服务端渲染输出不含完整值）。
        self.assertContains(response, "138****5678")
        self.assertNotContains(response, "13812345678")
        # 已冲账行灰显徽标语义保持（七态多态覆盖）。
        self.assertContains(response, "voided-row")

    def test_adapter_queries_write_archive_audit_events(self):
        """归档通道审计事件抽样：适配器查询逐次登记 who/when/what（§4.1 第 5 条）。"""
        self.client.force_login(self.front)
        self.client.get("/api/v1/front-desk/legacy-reservations")
        self.client.get(f"/api/v1/front-desk/legacy-reservations/{_FIRST_ORDER_ID}")
        self.client.get("/show_accomodation/")
        entity_ids = set(
            AuditEvent.objects.filter(action="billing.legacy_archive_query").values_list(
                "entity_id", flat=True
            )
        )
        self.assertEqual(
            entity_ids,
            {"adapter/accommodation", "adapter/folio"},
            "适配器读路径必须逐次登记归档查询审计（entity_id=adapter/<表>）",
        )
        sample = AuditEvent.objects.filter(
            action="billing.legacy_archive_query", entity_id="adapter/folio"
        ).latest("id")
        self.assertEqual(sample.actor_id, self.front.pk)
        self.assertEqual(sample.after_json["channel"], "adapter")
