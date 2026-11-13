"""P0-03 统一分页契约 + P0-07 排序与列表一致性 回归测试包。

=====================================================================
P0-07 全仓列表接口扫描结论清单（基线 2.0 §P0-07）
=====================================================================

一、已修复（apps/api 范围，本测试包断言覆盖）：
  * apps/core/api_contract.py paginate/parse_page_args
      唯一权威分页解析器；非法 page/page_size 统一 400 VALIDATION_ERROR；
      ensure_stable_ordering：调用方排序缺主键兜底时自动追加主键（含
      Meta.ordering 场景，运行时兜底无法修改的调用方）。
  * apps/api/front_desk.py
      - _page_args 改为委托 api_contract.parse_page_args（原第二套解析器删除）；
        5 个调用点 except 收敛为 (ValueError, ApiError)。
      - reservations 列表：-created_at → -created_at,-id（修复无稳定排序）；
        房间明细 N+1 → 主查询 prefetch_related。
      - arrivals/departures/in_house：排序本已带主键；no_show 逐行查询与
        room_segments 逐 Stay 查询 N+1 → prefetch_related。
      - reservation_detail：stays 显式 order_by("id") + room_segments prefetch。
      - folio_detail：payments/refunds 嵌套列表排序追加 -id。
  * apps/api/staff_center.py
      - staff_list：-created_at → -created_at,-id（P0-07 指定项）；
        任职/岗位授权/邀请 3×N+1 → Prefetch(to_attr) 一次取回。
      - membership_list：-created_at → -created_at,-id。
      - membership_detail：等级历史排序追加 -id（嵌套小列表）。
  * apps/api/reservation_center.py
      - reservation_list：-created_at → -created_at,-id。
      - audit_timeline：显式 -created_at,-id（不再依赖模型 Meta）；actor
        select_related 防 N+1。
  * apps/api/guest_center.py
      - guest_list：-created_at → -created_at,-id；电话（2-3 次/行）与
        当前物业会员关系（1 次/行）N+1 → Prefetch(to_attr)。
      - room_status_list：room_type select_related 防 N+1；number 有
        (property, number) 唯一约束，排序本已稳定，仍补 id 兜底。
      - rate_calendar：库存/房价按 stay_date 排序同一日期多房型多计划行 →
        补 room_type_id/rate_plan_id/id 兜底；room_type/rate_plan select_related。
      - guest_detail：最近预订 -created_at → -created_at,-id。
      - financial_summary_api：统计响应补 metadata（物业、近 7 天区间、
        营业日/自然日口径、状态范围、币种、生成时间、来源系统）。
  * apps/api/dashboard.py
      - dashboard_warnings：order_by("check_in") → ("check_in","id")。
  * apps/api/views.py
      - reservation_detail（公共面）：payments 嵌套排序追加 -id。

二、记录不改（他人治理域 / 只读过渡页，禁止本专项修改）：
  * apps/api/approval.py approval_list（P0-01/02 治理域，禁碰，已在并行
      治理中加固认证与 billing.approval_view 只读权限）：列表 QuerySet 仍无
      显式 order_by，依赖 ApprovalRecord Meta("-created_at") 且无主键兜底 →
      由 api_contract.paginate 的 ensure_stable_ordering 在运行时追加主键
      兜底（本包含运行时行为断言）。
  * hotelapp/views.py（只读过渡页，多按主键排序，只记录不修改）：
      index（order_field=±员工id，主键，稳定）、show_client/show_food/
      show_vip/search_*（主键序）、前台看板 accommodation 分组（id 序）、
      finance/night_audit 列表（-id 序）。旧导出/PII 面属 P0-04 治理域。
  * hotelapp 旧模型无物业外键：front_desk.legacy_reservations /
      legacy_reservation_detail 无跨物业过滤属历史数据面限制（只读适配器）。
  * apps/identity/staff_center_views.py 员工中心工作台内部查询存在
      -created_at 无主键兜底（员工中心智能体领域，记录）。
  * guest_center.guest_list 的"当前在住 Stay"查询仍为每行 1 次（深层反向链
      prefetch 复杂度高、收益有限，保留；每页 ≤100 行有界）。
  * apps/reservations/tests.py:621
      FrontDeskStayLifecycleServiceTests.
      test_in_house_room_change_preserves_room_segment_history 以
      order_by("started_at") 取两段住宿记录且断言下标顺序——同微秒时间戳
      打平时偶发不稳定（全量回归中偶现 1 次错误，隔离复跑 3 次均通过）。
      与 P0-07 同类问题但属服务层测试文件，非列表接口，移交预订域处理。

=====================================================================
P0-03 分页契约统一规则（所有列表接口同一口径）：
  * page/page_size 缺省 → 1 / 接口默认（20；audit_timeline 为 30）；
  * 1、最大值（page_size=100）→ 200；
  * 0、负数、非整数、空字符串、page_size 超过 100、超大数字 → 400
    VALIDATION_ERROR（不做静默截断或静默回退）；
  * page 上限 PAGE_NUMBER_MAX=100000（防御病态 OFFSET）。
=====================================================================
"""
from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.test import RequestFactory, TestCase
from django.utils import timezone

# --- test-only generated passwords (no literals in source) ---
# 测试专用密码：运行时随机生成，源码不落凭据字面量（Mimosa L3 门禁）。
from django.utils.crypto import get_random_string  # noqa: E402

from apps.billing.models import ApprovalRecord
from apps.core.api_contract import (
    PAGE_NUMBER_MAX,
    PAGE_SIZE_MAX,
    ApiError,
    ensure_stable_ordering,
    paginate,
    parse_page_args,
)
from apps.core.models import AuditEvent, Property
from apps.guests.models import Guest
from apps.identity.models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from apps.reservations.models import Reservation

_TEST_PW_510C52C1 = get_random_string(24)



ENVELOPE_ENDPOINTS = (
    # (url, 说明) — api_contract {data, meta.pagination} 封套族
    "/api/v1/staff",
    "/api/v1/guests",
    "/api/v1/rooms/status",
    "/api/v1/memberships",
    "/api/v1/reservations-mgmt",
    "/api/v1/audit-timeline",
    "/api/v1/approvals",
    # 基线 2.0 §4.4 审计与合规中心（读=core.audit_view，setUp 角色已含 audit_view）
    "/api/v1/audit/events",
    # 基线 2.0 §4.6 通知与待办中心（门禁=认证+当前物业有效任职，无专项权限码）
    "/api/v1/notifications",
)
FRONT_DESK_ENDPOINTS = (
    # front_desk {data, meta} 封套族（page_size 上限同为 100）
    "/api/v1/front-desk/reservations",
    "/api/v1/front-desk/arrivals",
    "/api/v1/front-desk/legacy-reservations",
)


class PaginationContractTestBase(TestCase):
    """DEFAULT 物业 + 一站式权限岗位；覆盖两套响应封套的全部列表端点。"""

    maxDiff = None

    def setUp(self):
        self.property = Property.objects.get(code="DEFAULT")
        self.user = User.objects.create_user(
            username="p0-contract-user", password=_TEST_PW_510C52C1)
        self.profile = StaffProfile.objects.create(
            user=self.user, full_name="契约测试员工",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        self.membership = PropertyStaffMembership.objects.create(
            property=self.property, staff_profile=self.profile,
            employee_no="P0-001",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        self.role = Group.objects.get(name="前台")
        self.role.permissions.add(*Permission.objects.filter(
            content_type__app_label__in=("identity", "hotelapp", "billing",
                                         "core"),
            codename__in=("staff_manage", "view_client", "view_room",
                          "view_vip", "view_accommodation", "finance_access",
                          "approval_view", "audit_view"),  # P0-04：审计查看专项权
        ))
        StaffRoleAssignment.objects.create(
            membership=self.membership, group=self.role,
            starts_at=timezone.now() - timedelta(minutes=1),
        )
        self.client.force_login(self.user)

    def _make_colleague(self, index):
        """额外的本物业员工（staff_list 稳定性/分页数据用，无需角色授权）。"""
        profile = StaffProfile.objects.create(
            full_name=f"契约同事{index:02d}",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        )
        PropertyStaffMembership.objects.create(
            property=self.property, staff_profile=profile,
            employee_no=f"P0-{index:03d}",
            status=PropertyStaffMembership.Status.ACTIVE,
        )
        return profile


class ParsePageArgsContractTests(PaginationContractTestBase):
    """P0-03 唯一解析器单元测试：全部边界按同一规则。"""

    def _args(self, query):
        request = RequestFactory().get("/api/v1/staff", query)
        return parse_page_args(request)

    def test_default_page_and_size(self):
        self.assertEqual(self._args({}), (1, 20))
        self.assertEqual(self._args({"page": "3"}), (3, 20))
        self.assertEqual(self._args({"page_size": "5"}), (1, 5))

    def test_valid_boundaries(self):
        self.assertEqual(self._args({"page": "1", "page_size": "1"}), (1, 1))
        self.assertEqual(
            self._args({"page": "2", "page_size": str(PAGE_SIZE_MAX)}),
            (2, PAGE_SIZE_MAX))

    def test_zero_negative_and_overflow_rejected(self):
        for bad in ({"page": "0"}, {"page": "-1"}, {"page": "-100"},
                    {"page_size": "0"}, {"page_size": "-3"},
                    {"page_size": "101"}, {"page_size": "10000000"}):
            with self.assertRaises(ApiError) as ctx:
                self._args(bad)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_non_integer_empty_and_huge_rejected(self):
        for bad in ({"page": "abc"}, {"page": "1.5"}, {"page": ""},
                    {"page": " "}, {"page_size": "abc"}, {"page_size": ""},
                    {"page": "9" * 30}, {"page_size": "9" * 30},
                    {"page": str(PAGE_NUMBER_MAX + 1)}):
            with self.assertRaises(ApiError) as ctx:
                self._args(bad)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_custom_default_and_max(self):
        request = RequestFactory().get("/x", {"page_size": "50"})
        self.assertEqual(parse_page_args(request, page_size_default=30), (1, 50))
        request = RequestFactory().get("/x", {"page_size": "31"})
        with self.assertRaises(ApiError):
            parse_page_args(request, page_size_default=30, page_size_max=30)


class EnsureStableOrderingTests(PaginationContractTestBase):
    """P0-03/P0-07：解析器级主键兜底保证（含无法修改的调用方运行时兜底）。"""

    def test_unordered_queryset_gets_pk(self):
        ordered = ensure_stable_ordering(Guest.objects.order_by())
        self.assertEqual(tuple(ordered.query.order_by), ("pk",))

    def test_ordering_without_pk_gets_pk_appended(self):
        ordered = ensure_stable_ordering(Guest.objects.order_by("-created_at"))
        self.assertEqual(tuple(ordered.query.order_by), ("-created_at", "id"))

    def test_meta_ordering_without_pk_gets_pk_appended(self):
        # PropertyStaffMembership Meta.ordering = ("property_id", "employee_no")
        ordered = ensure_stable_ordering(PropertyStaffMembership.objects.all())
        self.assertEqual(
            tuple(ordered.query.order_by),
            ("property_id", "employee_no", "id"))

    def test_ordering_with_pk_unchanged(self):
        queryset = Guest.objects.order_by("-created_at", "-id")
        self.assertIs(ensure_stable_ordering(queryset), queryset)

    def test_meta_ordering_with_pk_unchanged(self):
        # ApprovalRecord Meta.ordering = ("-created_at",)（无主键）应被追加；
        # Guest Meta.ordering = ("id",)（已含主键）应保持原样。
        guest_qs = Guest.objects.all()
        self.assertIs(ensure_stable_ordering(guest_qs), guest_qs)
        approval_qs = ensure_stable_ordering(ApprovalRecord.objects.all())
        self.assertEqual(
            tuple(approval_qs.query.order_by), ("-created_at", "id"))

    def test_paginate_returns_stable_meta(self):
        for index in range(3):
            Guest.objects.create(
                property=self.property, full_name=f"分页客人{index}",
                phone=f"13800000{index:03d}",
                phone_normalized=f"13800000{index:03d}")
        request = RequestFactory().get("/x", {"page": "2", "page_size": "2"})
        items, meta = paginate(Guest.objects.order_by("-created_at"), request)
        self.assertEqual(meta, {"page": 2, "page_size": 2, "total": 3,
                                "has_next": False, "has_previous": True})
        # 解析器兜底追加升序主键；created_at 打平时顺序确定。
        self.assertEqual([g.pk for g in items],
                         list(Guest.objects.order_by("-created_at", "id")
                              .values_list("pk", flat=True))[2:3])


class StaffListPaginationContractTests(PaginationContractTestBase):
    """/api/v1/staff 全边界 + 排序稳定性（created_at 打平 → 主键兜底）。"""

    ENDPOINT = "/api/v1/staff"

    def test_full_boundary_matrix(self):
        baseline = self.client.get(self.ENDPOINT).json()["meta"]["pagination"]["total"]
        for index in range(3):
            self._make_colleague(index + 2)
        # 缺省 → 200，默认 page=1 / page_size=20
        body = self.client.get(self.ENDPOINT).json()
        self.assertEqual(body["meta"]["pagination"]["page"], 1)
        self.assertEqual(body["meta"]["pagination"]["page_size"], 20)
        self.assertEqual(body["meta"]["pagination"]["total"], baseline + 3)
        # 1 / 最大值 → 200
        ok = self.client.get(self.ENDPOINT, {"page": "1", "page_size": "1"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["meta"]["pagination"]["page_size"], 1)
        ok = self.client.get(self.ENDPOINT, {"page_size": str(PAGE_SIZE_MAX)})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["meta"]["pagination"]["page_size"], PAGE_SIZE_MAX)
        # page 翻过全部数据 → 空页仍 200
        ok = self.client.get(
            self.ENDPOINT, {"page": "2", "page_size": str(baseline + 3)})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["data"], [])
        # 0/负数/超过最大/字母/空字符串/超大数字 → 400 VALIDATION_ERROR
        for bad in ({"page": "0"}, {"page": "-1"}, {"page": "abc"},
                    {"page": ""}, {"page": "9" * 30},
                    {"page_size": "0"}, {"page_size": "-2"},
                    {"page_size": "101"}, {"page_size": "abc"},
                    {"page_size": ""}, {"page_size": "9" * 30}):
            bad_response = self.client.get(self.ENDPOINT, bad)
            self.assertEqual(bad_response.status_code, 400, bad)
            self.assertEqual(
                bad_response.json()["error"]["code"], "VALIDATION_ERROR", bad)

    def test_ordering_is_deterministic_when_created_at_ties(self):
        baseline = self.client.get(self.ENDPOINT).json()["meta"]["pagination"]["total"]
        for index in range(2, 9):
            self._make_colleague(index)
        StaffProfile.objects.update(created_at=timezone.now())
        first = self.client.get(self.ENDPOINT, {"page_size": "100"}).json()
        second = self.client.get(self.ENDPOINT, {"page_size": "100"}).json()
        ids_first = [row["id"] for row in first["data"]]
        ids_second = [row["id"] for row in second["data"]]
        self.assertEqual(ids_first, ids_second)
        self.assertEqual(len(ids_first), baseline + 7)
        self.assertEqual(ids_first, sorted(ids_first, reverse=True))


class FrontDeskPaginationContractTests(PaginationContractTestBase):
    """front_desk 封套族全边界 + 预订列表排序稳定性（P0-07 指定项）。"""

    ENDPOINT = "/api/v1/front-desk/reservations"

    def _make_reservation(self, index):
        guest = Guest.objects.create(
            property=self.property, full_name=f"前台契约客人{index}",
            phone=f"13800110{index:03d}",
            phone_normalized=f"13800110{index:03d}")
        return Reservation.objects.create(
            property=self.property,
            booking_no=f"P0CONTRACT-{index:04d}",
            guest=guest,
            check_in=timezone.localdate() + timedelta(days=2),
            check_out=timezone.localdate() + timedelta(days=4),
        )

    def test_full_boundary_matrix(self):
        baseline = self.client.get(self.ENDPOINT).json()["meta"]["total"]
        self._make_reservation(1)
        # 缺省 → 200，默认 page=1 / page_size=20
        body = self.client.get(self.ENDPOINT).json()
        self.assertEqual(body["meta"]["page"], 1)
        self.assertEqual(body["meta"]["page_size"], 20)
        self.assertEqual(body["meta"]["total"], baseline + 1)
        # 1 / 最大值 → 200
        ok = self.client.get(self.ENDPOINT, {"page": "1", "page_size": "1"})
        self.assertEqual(ok.status_code, 200)
        ok = self.client.get(self.ENDPOINT, {"page_size": str(PAGE_SIZE_MAX)})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["meta"]["page_size"], PAGE_SIZE_MAX)
        # 0/负数/超过最大/字母/空字符串/超大数字 → 400 VALIDATION_ERROR
        for bad in ({"page": "0"}, {"page": "-1"}, {"page": "abc"},
                    {"page": ""}, {"page": "9" * 30},
                    {"page_size": "0"}, {"page_size": "-2"},
                    {"page_size": "101"}, {"page_size": "abc"},
                    {"page_size": ""}, {"page_size": "9" * 30}):
            bad_response = self.client.get(self.ENDPOINT, bad)
            self.assertEqual(bad_response.status_code, 400, bad)
            self.assertEqual(
                bad_response.json()["error"]["code"], "VALIDATION_ERROR", bad)

    def test_reservations_ordering_is_deterministic_when_created_at_ties(self):
        for index in range(5):
            self._make_reservation(index)
        Reservation.objects.update(created_at=timezone.now())
        first = self.client.get(self.ENDPOINT, {"page_size": "100"}).json()
        second = self.client.get(self.ENDPOINT, {"page_size": "100"}).json()
        nos_first = [row["booking_no"] for row in first["data"]]
        self.assertEqual(nos_first,
                         [row["booking_no"] for row in second["data"]])
        ids = list(Reservation.objects.filter(property=self.property)
                   .order_by("-created_at", "-id")
                   .values_list("booking_no", flat=True))
        self.assertEqual(nos_first, ids)

    def test_arrivals_rejects_invalid_page_before_business_logic(self):
        bad_response = self.client.get(
            "/api/v1/front-desk/arrivals", {"page": "abc"})
        self.assertEqual(bad_response.status_code, 400)
        self.assertEqual(
            bad_response.json()["error"]["code"], "VALIDATION_ERROR")

    def test_legacy_reservations_boundary(self):
        ok = self.client.get(
            "/api/v1/front-desk/legacy-reservations", {"page": "-3"})
        self.assertEqual(ok.status_code, 400)
        self.assertEqual(
            ok.json()["error"]["code"], "VALIDATION_ERROR")


class MultiEndpointPaginationSamplingTests(PaginationContractTestBase):
    """多端点抽查：同一规则跨两套封套族一致。"""

    def test_valid_default_and_max_across_envelope_endpoints(self):
        for url in ENVELOPE_ENDPOINTS:
            with self.subTest(endpoint=url):
                body = self.client.get(url)
                self.assertEqual(body.status_code, 200, url)
                payload = body.json()
                self.assertIn("data", payload)
                self.assertEqual(payload["meta"]["pagination"]["page"], 1)
                # page_size=100（上限）合法
                ok = self.client.get(url, {"page_size": str(PAGE_SIZE_MAX)})
                self.assertEqual(ok.status_code, 200, url)
                self.assertEqual(
                    ok.json()["meta"]["pagination"]["page_size"], PAGE_SIZE_MAX)

    def test_invalid_params_rejected_identically_across_envelope_endpoints(self):
        for url in ENVELOPE_ENDPOINTS:
            for bad in ({"page": "abc"}, {"page": "0"},
                        {"page_size": "-1"}, {"page_size": "101"}):
                with self.subTest(endpoint=url, params=bad):
                    response = self.client.get(url, bad)
                    self.assertEqual(response.status_code, 400, (url, bad))
                    error = response.json()["error"]
                    self.assertEqual(error["code"], "VALIDATION_ERROR", (url, bad))

    def test_invalid_params_rejected_identically_across_front_desk_endpoints(self):
        for url in FRONT_DESK_ENDPOINTS:
            for bad in ({"page": "abc"}, {"page": "-1"},
                        {"page_size": "0"}, {"page_size": "101"}):
                with self.subTest(endpoint=url, params=bad):
                    response = self.client.get(url, bad)
                    self.assertEqual(response.status_code, 400, (url, bad))
                    error = response.json()["error"]
                    self.assertEqual(error["code"], "VALIDATION_ERROR", (url, bad))

    def test_audit_timeline_default_page_size_is_30_and_max_still_100(self):
        response = self.client.get("/api/v1/audit-timeline")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["meta"]["pagination"]["page_size"], 30)
        over = self.client.get("/api/v1/audit-timeline", {"page_size": "101"})
        self.assertEqual(over.status_code, 400)

    def test_approval_list_is_stable_ordering_at_runtime(self):
        """approval.py 属禁碰治理域；列表默认按创建时间和主键倒序稳定排序。"""
        for index in range(3):
            ApprovalRecord.objects.create(
                property=self.property,
                action_code="billing.refund",
                entity_type="Payment", entity_id=str(index),
                requested_by=self.user,
                reason="契约测试", idempotency_key=f"p0-contract-{index}",
            )
        ApprovalRecord._base_manager.all().update(created_at=timezone.now())
        first = self.client.get("/api/v1/approvals", {"page_size": "100"}).json()
        second = self.client.get("/api/v1/approvals", {"page_size": "100"}).json()
        ids_first = [row["id"] for row in first["data"]]
        self.assertEqual(ids_first, [row["id"] for row in second["data"]])
        expected = list(
            ApprovalRecord.objects.filter(requested_by=self.user)
            .order_by("-created_at", "-id").values_list("id", flat=True))
        self.assertEqual(ids_first, expected)
        self.assertEqual(len(ids_first), 3)


class AuditTimelineOrderingTests(PaginationContractTestBase):
    """P0-07：审计时间线显式 -created_at,-id，不依赖模型 Meta。"""

    def test_ordering_is_deterministic_when_created_at_ties(self):
        for index in range(6):
            AuditEvent.objects.create(
                property=self.property,
                action="contract.test.event",
                entity_type="ContractProbe",
                entity_id=str(index),
            )
        AuditEvent._base_manager.all().update(created_at=timezone.now())
        first = self.client.get(
            "/api/v1/audit-timeline", {"page_size": "100"}).json()
        second = self.client.get(
            "/api/v1/audit-timeline", {"page_size": "100"}).json()
        ids_first = [row["id"] for row in first["data"]]
        self.assertEqual(ids_first, [row["id"] for row in second["data"]])
        expected = list(
            AuditEvent.objects.filter(action="contract.test.event")
            .order_by("-created_at", "-id").values_list("id", flat=True))
        self.assertEqual(ids_first, expected)


class GuestsListOrderingTests(PaginationContractTestBase):
    """P0-07：客人列表稳定排序 + 分页与 N+1 prefetch 后行为不变。"""

    def test_ordering_is_deterministic_and_search_still_works(self):
        for index in range(5):
            Guest.objects.create(
                property=self.property, full_name=f"契约客人{index}",
                phone=f"13800220{index:03d}",
                phone_normalized=f"13800220{index:03d}")
        Guest.objects.update(created_at=timezone.now())
        first = self.client.get("/api/v1/guests", {"page_size": "100"}).json()
        second = self.client.get("/api/v1/guests", {"page_size": "100"}).json()
        ids_first = [row["id"] for row in first["data"]]
        self.assertEqual(ids_first, [row["id"] for row in second["data"]])
        self.assertEqual(
            ids_first,
            list(Guest.objects.filter(property=self.property)
                 .order_by("-created_at", "-id")
                 .values_list("id", flat=True)))
        searched = self.client.get(
            "/api/v1/guests", {"q": "契约客人3"}).json()
        self.assertEqual([row["full_name"] for row in searched["data"]],
                         ["契约客人3"])


class FinancialSummaryMetadataTests(PaginationContractTestBase):
    """P0-07：统计口径注记（物业/日期口径/状态范围/币种/生成时间）。"""

    def test_metadata_fields_present(self):
        response = self.client.get("/api/v1/financial-summary")
        self.assertEqual(response.status_code, 200)
        summary = response.json()["data"]
        metadata = summary["metadata"]
        self.assertEqual(metadata["property"]["code"], self.property.code)
        self.assertEqual(metadata["property"]["name"], self.property.name)
        self.assertEqual(metadata["period"], summary["period"])
        self.assertIn("ledger_entries", metadata["date_basis"])
        self.assertIn("payments_refunds", metadata["date_basis"])
        self.assertIn("status_scope", metadata)
        self.assertTrue(metadata["currency"])
        self.assertIn("generated_at", metadata)
        self.assertEqual(metadata["source_system"], "PMS2")


class CrossPropertyFilterSamplingTests(PaginationContractTestBase):
    """P0-07：列表端点跨物业过滤抽查（数据不越物业）。"""

    def setUp(self):
        super().setUp()
        self.other_property = Property.objects.create(
            code="P0OTHER", name="契约测试外店")

    def test_guest_list_excludes_other_property(self):
        foreign = Guest.objects.create(
            property=self.other_property, full_name="外店契约客人",
            phone="13899000001", phone_normalized="13899000001")
        internal = Guest.objects.create(
            property=self.property, full_name="本店契约客人",
            phone="13899000002", phone_normalized="13899000002")
        response = self.client.get("/api/v1/guests", {"page_size": "100"})
        ids = {row["id"] for row in response.json()["data"]}
        self.assertIn(internal.pk, ids)
        self.assertNotIn(foreign.pk, ids)

    def test_reservation_mgmt_list_excludes_other_property(self):
        guest = Guest.objects.create(
            property=self.other_property, full_name="外店预订客人",
            phone="13899000003", phone_normalized="13899000003")
        Reservation.objects.create(
            property=self.other_property, booking_no="P0OTHER-0001",
            guest=guest,
            check_in=timezone.localdate() + timedelta(days=1),
            check_out=timezone.localdate() + timedelta(days=2))
        response = self.client.get(
            "/api/v1/reservations-mgmt", {"page_size": "100"})
        self.assertEqual(
            [row["booking_no"] for row in response.json()["data"]], [])
