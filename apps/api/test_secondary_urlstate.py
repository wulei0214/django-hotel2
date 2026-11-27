"""次级列表（每日房价 / 房型每日库存）URL 状态同步的后端契约用例。

业务目的
--------
房态与库存中心有两个可搜索的次级列表（房价、库存）。历史实现里两个小节
共用同一个查询参数 ``q``，后写覆盖先写 → 一个搜索词同时污染两个小节，
且无法通过 URL 还原各自状态。本组用例锁定如下口径：

1. ``rate_q`` 只作用于房价集合、``inventory_q`` 只作用于库存集合；
2. 两者同时给出时互不干扰；
3. ``q`` 保留为兼容参数（同时作用于两个集合），某个小节自有参数存在时
   以它为准，未给出自有参数的小节回退到 ``q``；
4. 分页参数继续按小节前缀隔离（rate_* 与 inventory_* 互不影响），
   非法值仍按统一分页契约返回 400 VALIDATION_ERROR；
5. OpenAPI 契约必须声明视图实际读取的全部查询参数。
"""

from decimal import Decimal

from django.test import override_settings

from apps.api.test_e2e_flows import E2EFlowTestBase
from apps.inventory.models import DailyRate, RoomType, RoomTypeInventory


@override_settings(DEBUG=True, DJANGO_ENV="test")
class SecondaryUrlStateApiTests(E2EFlowTestBase):
    """两个房型 × 逐日房价/库存，用于验证两个次级集合的搜索彼此独立。"""

    def setUp(self):
        super().setUp()
        self.front = self._make_staff("sec_urlstate_front", "前台")
        self.twin_type = RoomType.objects.create(
            property=self.property,
            code="E2E-TWIN",
            name="E2E 测试双床房",
            capacity=2,
        )
        for day in self.inventory_dates[:4]:
            DailyRate.objects.create(
                property=self.property,
                rate_plan=self.rate_plan,
                room_type=self.twin_type,
                stay_date=day,
                price=Decimal("258.00"),
            )
            RoomTypeInventory.objects.create(
                property=self.property,
                room_type=self.twin_type,
                stay_date=day,
                total_rooms=3,
            )
        self.client.force_login(self.front)

    # ------------------------------------------------------------------
    # 助手
    # ------------------------------------------------------------------
    def _get(self, query=""):
        response = self.client.get("/api/v1/rate-calendar" + query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]

    def _window(self, extra=""):
        """固定窗口（前 4 天），保证两个房型的逐日数据都在窗口内。"""
        return f"?start={self.today.isoformat()}&days=4{extra}"

    @staticmethod
    def _codes(rows):
        return {row["room_type"]["code"] for row in rows}

    # ------------------------------------------------------------------
    # 搜索：小节之间互不影响
    # ------------------------------------------------------------------
    def test_rate_q_filters_only_the_rates_collection(self):
        """rate_q 只过滤房价集合：库存集合必须保持全量（不被污染）。"""
        data = self._get(self._window("&rate_q=E2E-TWIN"))
        self.assertTrue(data["rates"])
        self.assertEqual(self._codes(data["rates"]), {"E2E-TWIN"})
        self.assertEqual(self._codes(data["inventory"]), {"E2E-KING", "E2E-TWIN"})

    def test_inventory_q_filters_only_the_inventory_collection(self):
        """inventory_q 只过滤库存集合：房价集合必须保持全量（不被污染）。"""
        data = self._get(self._window("&inventory_q=E2E-TWIN"))
        self.assertTrue(data["inventory"])
        self.assertEqual(self._codes(data["inventory"]), {"E2E-TWIN"})
        self.assertEqual(self._codes(data["rates"]), {"E2E-KING", "E2E-TWIN"})

    def test_both_section_search_params_are_independent(self):
        """两个小节各自给出搜索词时互不干扰（这正是共用 q 时的缺陷场景）。"""
        data = self._get(self._window("&rate_q=E2E-KING&inventory_q=E2E-TWIN"))
        self.assertEqual(self._codes(data["rates"]), {"E2E-KING"})
        self.assertEqual(self._codes(data["inventory"]), {"E2E-TWIN"})

    # ------------------------------------------------------------------
    # 兼容参数 q 与优先级
    # ------------------------------------------------------------------
    def test_legacy_q_still_filters_both_collections(self):
        """向后兼容：只给 q 时两个集合同时按 q 过滤。"""
        data = self._get(self._window("&q=E2E-TWIN"))
        self.assertEqual(self._codes(data["rates"]), {"E2E-TWIN"})
        self.assertEqual(self._codes(data["inventory"]), {"E2E-TWIN"})

    def test_section_param_wins_and_missing_section_falls_back_to_q(self):
        """优先级：小节自有参数以它为准，未给出自有参数的小节回退到 q。"""
        data = self._get(self._window("&rate_q=E2E-KING&q=E2E-TWIN"))
        self.assertEqual(self._codes(data["rates"]), {"E2E-KING"})
        self.assertEqual(self._codes(data["inventory"]), {"E2E-TWIN"})

        data = self._get(self._window("&inventory_q=E2E-TWIN&q=E2E-KING"))
        self.assertEqual(self._codes(data["rates"]), {"E2E-KING"})
        self.assertEqual(self._codes(data["inventory"]), {"E2E-TWIN"})

    # ------------------------------------------------------------------
    # 分页：仍按小节前缀隔离，非法值仍 400
    # ------------------------------------------------------------------
    def test_pagination_params_stay_isolated_by_section_prefix(self):
        """rate_page / inventory_page 各自独立：rate_page=2 不得影响库存分页。"""
        data = self._get(self._window("&rate_page=2&rate_page_size=1"))
        self.assertEqual(data["rates_pagination"]["page"], 2)
        self.assertEqual(data["rates_pagination"]["page_size"], 1)
        self.assertEqual(data["inventory_pagination"]["page"], 1)
        self.assertEqual(data["inventory_pagination"]["page_size"], 20)
        self.assertEqual(len(data["rates"]), 1)
        self.assertEqual(len(data["inventory"]), 8)

    def test_invalid_secondary_pagination_params_still_return_400(self):
        """分页契约不放宽：0 / 负数 / 非整数 / 超上限一律 400。"""
        for query in (
            "&rate_page=0",
            "&rate_page=-1",
            "&rate_page=abc",
            "&rate_page_size=0",
            "&rate_page_size=101",
            "&rate_page_size=abc",
            "&inventory_page=0",
            "&inventory_page=-1",
            "&inventory_page=abc",
            "&inventory_page_size=0",
            "&inventory_page_size=101",
            "&inventory_page_size=abc",
        ):
            with self.subTest(query=query):
                response = self.client.get("/api/v1/rate-calendar" + self._window(query))
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(
                    response.json()["error"]["code"], "VALIDATION_ERROR", response.content
                )

    # ------------------------------------------------------------------
    # 契约：OpenAPI 声明必须覆盖视图实际读取的查询参数
    # ------------------------------------------------------------------
    def test_openapi_declares_every_query_param_the_view_reads(self):
        """契约漏声明参数 → 前后端按契约对接会漏传（本次新增 7 个查询参数）。"""
        from apps.api.openapi_schema import build_openapi

        operation = build_openapi()["paths"]["/api/v1/rate-calendar"]["get"]
        query_params = {p["name"] for p in operation["parameters"] if p["in"] == "query"}
        self.assertEqual(
            query_params,
            {
                "start",
                "days",
                "room_type_id",
                "q",
                "rate_q",
                "inventory_q",
                "rate_page",
                "rate_page_size",
                "inventory_page",
                "inventory_page_size",
            },
            "rate-calendar 契约查询参数与视图实际读取不一致",
        )
