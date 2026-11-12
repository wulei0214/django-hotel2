"""P0 链路真实 HTTP 冒烟（实施方案 §9.11，标记 @pytest.mark.p0）。

覆盖 8 条 P0 冒烟面（全部经真实 HTTP 打到 PMS_BASE_URL 指向的已种子化实例）：

  1. 公开面可达：/reserve/ 200；/api/v1/availability 200 且 data 结构正确。
  2. 公开预订创建：POST /api/v1/reservations（唯一 Idempotency-Key）→
     201 + booking_no + access_token（一次性下发）；同 key 重放 → 200 幂等。
  3. token 查询：X-Booking-Token → 200 姓名脱敏且不含完整手机号；错 token → 404。
  4. token 取消：→ 200 status CANCELLED 且库存释放（availability 前后对比）。
  5. 员工登录流 → /home/ 200；无凭据访问前台 overview → 401（POST 受
     require_GET 方法门短路为 405，一并接受 302/401/405 拒绝族）。
  6. 权限拒绝：非超管员工访问 /permissions/ → 302 兼容跳转，
     /platform/permissions/ → 403（平台管理面仅限超管）。
  7. 旧路由退役抽查：POST /insert_client/ → 410；GET /booking/ → 302 /reserve/。
  8. OpenAPI 契约：/api/v1/openapi.json → 200 且 openapi 3.1 且 operation ≥ 60。

安全约束：不在断言/日志/报告中记录口令、Session Cookie、CSRF token、
Booking Access Token 明文或完整手机号——token 只用于请求头。
"""

from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.p0

NIGHTS = 2  # 冒烟预订连住 2 晚
ROOMS = 1  # 单间
AVAILABILITY_KEYS = {
    "room_type_id",
    "room_type_name",
    "rate_plan_id",
    "available_count",
    "total_amount",
    "nightly_details",
}


def _today():
    """本机日期（与被测实例同时区部署时的口径一致；仅供测试选期）。"""
    return date.today()


# ---------------------------------------------------------------------------
# 模块级共享：一笔真实的 P0 预订（创建 → 查询 → 取消 顺序消费）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def p0_booking(api, unique_phone, idempotency_key):
    """创建一笔公开预订并缓存上下文（booking_no / token / 库存前照）。

    access_token 明文只保存在本 fixture 内存中用于后续请求头/请求体，
    绝不输出到日志或断言消息。
    """
    check_in = _today() + timedelta(days=1)
    check_out = check_in + timedelta(days=NIGHTS)

    # 公开写接口需要 X-CSRFToken（CSRF_USE_SESSIONS=True，从公开页获取令牌）
    api.ensure_csrf("/reserve/")

    # 创建前库存前照（取消测试用）：room_type_id -> available_count
    before = {}
    snap = api.get(
        "/api/v1/availability",
        params={
            "check_in": check_in.isoformat(),
            "check_out": check_out.isoformat(),
            "rooms": ROOMS,
        },
    )
    assert snap.status_code == 200
    for item in snap.json().get("data", []):
        before[item["room_type_id"]] = item["available_count"]

    target = next(
        (
            item
            for item in snap.json().get("data", [])
            if item.get("available_count") is not None and item["available_count"] >= ROOMS
        ),
        None,
    )
    assert (
        target is not None
    ), "实例无可用房型库存：请先按 README 完成演示数据种子（房型/房价计划/库存日历）"

    payload = {
        "full_name": "冒烟住客",
        "phone": unique_phone,
        "room_type_id": target["room_type_id"],
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "rooms": ROOMS,
        "adults": 1,
        "children": 0,
    }
    response = api.post_json(
        "/api/v1/reservations",
        payload,
        headers={"Idempotency-Key": idempotency_key},
    )
    assert (
        response.status_code == 201
    ), f"公开预订创建失败（HTTP {response.status_code}）：请确认实例已种子化且未触发限流"
    body = response.json()
    assert body.get("booking_no")
    assert body.get("access_token"), "首次创建必须一次性下发 access_token"
    assert body.get("created") is True
    assert body.get("status") == "PENDING"

    # 持有中的库存快照（取消测试用）：创建已占用 ROOMS 间
    availability_held = _availability_map(
        api,
        {
            "payload": payload,
        },
    )
    return {
        "booking_no": body["booking_no"],
        "token": body["access_token"],
        "payload": payload,
        "idempotency_key": idempotency_key,
        "room_type_id": target["room_type_id"],
        "availability_before": before,
        "availability_held": availability_held,
    }


def _availability_map(api, booking):
    response = api.get(
        "/api/v1/availability",
        params={
            "check_in": booking["payload"]["check_in"],
            "check_out": booking["payload"]["check_out"],
            "rooms": ROOMS,
        },
    )
    assert response.status_code == 200
    return {
        item["room_type_id"]: item["available_count"] for item in response.json().get("data", [])
    }


# ---------------------------------------------------------------------------
# 1. 公开面可达
# ---------------------------------------------------------------------------
def test_public_reserve_page_200(api):
    response = api.get("/reserve/")
    assert response.status_code == 200


def test_public_availability_structure(api):
    check_in = _today() + timedelta(days=1)
    check_out = check_in + timedelta(days=1)
    response = api.get(
        "/api/v1/availability",
        params={"check_in": check_in.isoformat(), "check_out": check_out.isoformat(), "rooms": 1},
    )
    assert response.status_code == 200
    body = response.json()
    for key in ("property", "check_in", "check_out", "rooms", "data"):
        assert key in body
    assert body["check_in"] == check_in.isoformat()
    assert body["check_out"] == check_out.isoformat()
    assert body["rooms"] == 1
    assert isinstance(body["data"], list)
    for item in body["data"]:
        assert AVAILABILITY_KEYS.issubset(item.keys())
        assert isinstance(item["available_count"], int)
        assert item["available_count"] >= 0


# ---------------------------------------------------------------------------
# 2. 公开预订创建 + 幂等重放
# ---------------------------------------------------------------------------
def test_public_reservation_create_and_idempotent_replay(api, p0_booking):
    # 首次创建断言（fixture 中已完成请求，此处校验契约）
    assert p0_booking["booking_no"]
    assert p0_booking["token"]

    # 同 Idempotency-Key + 同内容重放 → 200 幂等返回既有订单
    replay = api.post_json(
        "/api/v1/reservations",
        p0_booking["payload"],
        headers={"Idempotency-Key": p0_booking["idempotency_key"]},
    )
    assert replay.status_code == 200
    body = replay.json()
    assert body.get("booking_no") == p0_booking["booking_no"]
    assert body.get("created") is False
    # 重放不再下发 token（明文仅首次返回）
    assert not body.get("access_token")


# ---------------------------------------------------------------------------
# 3. token 查询：脱敏 + 错 token 404
# ---------------------------------------------------------------------------
def test_booking_token_query_masking(api, p0_booking):
    response = api.get(
        f"/api/v1/reservations/{p0_booking['booking_no']}",
        headers={"X-Booking-Token": p0_booking["token"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["booking_no"] == p0_booking["booking_no"]
    assert body["status"] == "PENDING"
    # §6.5 脱敏：姓名保留首字符 + 星号；不得出现完整手机号/姓名明文
    masked_name = body.get("guest_name", "")
    assert masked_name and "*" in masked_name
    assert masked_name != p0_booking["payload"]["full_name"]
    text = response.text
    assert p0_booking["payload"]["phone"] not in text, "响应泄露了完整手机号"

    # 错误 token → 404（不泄露订单存在性）
    wrong = api.get(
        f"/api/v1/reservations/{p0_booking['booking_no']}",
        headers={"X-Booking-Token": "definitely-wrong-token"},
    )
    assert wrong.status_code == 404


# ---------------------------------------------------------------------------
# 4. token 取消 + 库存释放
# ---------------------------------------------------------------------------
def test_booking_token_cancel_releases_inventory(api, p0_booking):
    booking_no = p0_booking["booking_no"]

    response = api.post_json(
        f"/api/v1/reservations/{booking_no}/cancel",
        {"token": p0_booking["token"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["booking_no"] == booking_no
    assert body["status"] == "CANCELLED"
    assert body.get("changed") is True

    # 库存释放：取消后可用量回升到不低于创建前水平，
    # 且比"持有中"快照至少多出 ROOMS 间（创建时占用的库存被释放）
    after = _availability_map(api, p0_booking)
    room_type_id = p0_booking["room_type_id"]
    released = after.get(room_type_id)
    assert released is not None
    held = p0_booking["availability_held"].get(room_type_id)
    assert held is not None
    assert released - held >= ROOMS
    assert released >= p0_booking["availability_before"].get(room_type_id, 0)


# ---------------------------------------------------------------------------
# 5. 员工登录流 + 无凭据拒绝
# ---------------------------------------------------------------------------
def test_staff_home_and_unauthenticated_front_desk_denied(api, staff_session):
    # 登录 smoke：staff_session fixture 已完成真实表单登录，/home/ 200
    home = staff_session.get("/home/")
    assert home.status_code == 200

    # 无凭据 GET 前台 overview → 401 JSON（AUTHENTICATION_REQUIRED）
    anonymous_get = api.get("/api/v1/front-desk/overview")
    assert anonymous_get.status_code == 401

    # 无凭据 POST：认证门拒绝（302 登录跳转 / 401 JSON）；
    # 该视图另有 require_GET 方法门，会先短路为 405 JSON——同为"未授权拒绝族"
    anonymous_post = api.post_json("/api/v1/front-desk/overview")
    assert anonymous_post.status_code in (302, 401, 405)


# ---------------------------------------------------------------------------
# 6. 权限拒绝：非超管员工访问平台权限管理面
# ---------------------------------------------------------------------------
def test_permission_denied_on_platform_permissions(staff_session):
    # /permissions/ 旧入口：302 兼容跳转 /platform/permissions/（任何登录态）
    legacy = staff_session.get("/permissions/", allow_redirects=False)
    assert legacy.status_code == 302

    # 平台权限矩阵门禁：非超管被拒——
    # 仅持 PMS 会话的员工（无平台 Cookie）→ 302 平台登录页；
    # 已持平台会话但非 superuser → 403（PermissionDenied）
    denied = staff_session.get("/platform/permissions/", allow_redirects=False)
    assert denied.status_code in (302, 403)


# ---------------------------------------------------------------------------
# 7. 旧路由退役抽查
# ---------------------------------------------------------------------------
def test_legacy_routes_retired(api, staff_session):
    # P1-05：旧写入口已 410——持相应岗位权限的员工命中视图体（永久退役语义）
    staff_post = staff_session.post("/insert_client/", data={}, allow_redirects=False)
    assert staff_post.status_code == 410

    # 匿名命中门禁族：带 CSRF 头 → 302 登录墙；缺 CSRF 头 → 403；
    # 任何情况下都不可达旧写逻辑（410 亦可）
    anonymous_post = api.post("/insert_client/", data={}, allow_redirects=False)
    assert anonymous_post.status_code in (302, 403, 410)

    # D8：旧预订页冻结 → 302 /reserve/
    legacy_booking = api.get("/booking/", allow_redirects=False)
    assert legacy_booking.status_code == 302
    assert legacy_booking.headers.get("Location", "").startswith("/reserve/")


# ---------------------------------------------------------------------------
# 8. OpenAPI 契约
# ---------------------------------------------------------------------------
def test_openapi_contract(api):
    response = api.get("/api/v1/openapi.json")
    assert response.status_code == 200
    doc = response.json()
    assert str(doc.get("openapi", "")).startswith("3.1")
    assert doc.get("info", {}).get("title")

    methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operation_count = sum(
        1
        for path_item in doc.get("paths", {}).values()
        for method in methods
        if method in path_item
    )
    assert operation_count >= 60, "OpenAPI operation 数 %d 低于契约基线 60" % operation_count
