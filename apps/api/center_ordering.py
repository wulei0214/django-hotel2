"""管理中心列表排序白名单（前后端唯一事实来源）。

PMS2 通用管理中心页面（templates/pms/management_center.html）把「筛选 / 排序 / 分页」
同步到 URL，其中排序必须由服务端执行 —— 浏览器内排序只能作用于当前页，
在服务端分页的前提下会给出「整表已排序」的错觉。因此排序键必须同时满足：

1. 后端只接受本表登记的键（apps.core.api_contract.apply_ordering 白名单制）；
2. 前端只把本表登记的键渲染成可排序表头（apps/api/pages.py 的 columns[*].sort）。

两边共用本表，避免「前端能点、后端不认」或反之的漂移。
apps/api/test_center_ordering.py 会断言 pages.py 声明的每个 sort 键都在本表内，
且与对应列的展示路径同源。

键名对外（URL / 前端），值为 ORM 字段路径（后端）。新增可排序列时，
必须同时在本表登记键、在 pages.py 对应列上声明 sort，两处缺一即测试失败。
"""

# 中心键与 apps/api/pages.py 的 MANAGEMENT_CENTERS 键一一对应。
CENTER_ORDERING: dict[str, dict[str, str]] = {
    # 客人 360：只允许本表字段排序（该列表用 distinct()，跨表排序会触发
    # MySQL/PostgreSQL 的 DISTINCT + ORDER BY 限制，故只开放本表字段）。
    "guests": {
        "full_name": "full_name",
    },
    # 房态与库存中心
    "rooms": {
        "number": "number",
        "floor": "floor",
        "room_type_name": "room_type__name",
        "occupancy_status": "occupancy_status",
        "housekeeping_status": "housekeeping_status",
        "maintenance_status": "maintenance_status",
    },
    # 会员中心
    "memberships": {
        "member_no": "member_no",
        "guest_name": "guest__full_name",
        "level_code": "level_code",
        "status": "status",
        "joined_at": "joined_at",
        "expires_at": "expires_at",
    },
    # 预订与入住中心
    "reservations": {
        "booking_no": "booking_no",
        "guest_name": "guest__full_name",
        "status": "status",
        "check_in": "check_in",
        "check_out": "check_out",
        "total_amount": "total_amount",
    },
}


def ordering_for(center: str) -> dict[str, str]:
    """取某中心的排序白名单；未登记的中心返回空表（调用方保持默认排序）。"""
    return CENTER_ORDERING.get(center, {})
