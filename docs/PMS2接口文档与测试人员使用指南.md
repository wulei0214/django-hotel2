# PMS2 API 接口文档与测试人员使用指南

> 文档类型：接口现状目录 + 测试规范  
> 适用项目：django-hotel2 / PMS2  
> 核对分支：main  
> 核对日期：2026-11-04  
> 状态：第一版基线；接口清单已按当前路由登记，部分管理接口的字段级契约仍需随实现补齐。  
> 重要：本文描述当前代码可见的接口，不代表所有接口均已完成业务验收。

## 1. 结论：当前是否已有 API 文档

项目已经存在一个运行时 OpenAPI JSON 入口：

- 本地地址：`http://127.0.0.1:8000/api/v1/openapi.json`
- 作用：向 Apifox 等工具提供 OpenAPI 文档导入源。
- 当前覆盖范围：公开预订、房态查询和本地模拟支付的 6 个接口。
- 当前不足：`apps/api/urls.py` 中还有前台、夜审、住客、房态、财务摘要、审批、预订管理、审计、员工和会员等接口，但现有 OpenAPI JSON 尚未完整描述这些接口；部分响应结构、字段约束、权限要求、错误码和示例也未达到完整测试契约标准。

因此，准确结论是：**项目已有局部 OpenAPI 文档入口，但还没有覆盖全部 PMS2 API 的完整接口文档。** 本文先建立接口目录和测试方法基线，后续逐接口补齐 OpenAPI 契约，不允许把未记录的参数或响应字段当成既定事实。

## 2. API 基本约定

| 项目 | 当前约定 |
|---|---|
| API 根路径 | `/api/v1/` |
| 数据格式 | JSON；查询参数使用 URL query string |
| 日期 | `YYYY-MM-DD` |
| 内容类型 | 写请求使用 `Content-Type: application/json` |
| 登录态 | PMS 管理接口按 Django Session / Cookie 认证及 CSRF 机制执行，具体以视图装饰器和权限服务为准 |
| 幂等 | 部分写接口支持 `Idempotency-Key`；不能假设所有 POST 都支持 |
| 成功响应 | 当前存在多种格式；旧公开接口不统一使用 `{data, meta}` |
| 错误响应 | 当前存在 `{error: "..."}` 与统一错误封套等多种格式，按具体接口记录 |
| 环境 | 本地开发、测试、预发布、生产必须分开；禁止使用生产真实数据做破坏性测试 |

### 2.1 当前响应格式的重要差异

`apps/core/api_contract.py` 定义了新接口可使用的统一封套：

成功：`{ "data": ..., "meta": { "request_id": "...", "source_system": "PMS2", "generated_at": "..." } }`

错误：`{ "error": { "code": "...", "message": "...", "field_errors": {}, "retryable": false }, "meta": {...} }`

但公开预订接口 `apps/api/views.py` 仍返回自己的 JSON 结构，例如可用房态返回 `property/check_in/check_out/rooms/data`，错误通常为 `{ "error": "..." }`。测试人员必须按接口自身契约断言，不能把所有接口都按统一封套解析。后续新接口应统一采用 `api_response/api_error_response`，旧接口迁移需单独安排，避免无版本变更造成客户端破坏。

## 3. 当前 API 路由目录

以下路径相对于 `/api/v1/`。该目录来自 `apps/api/urls.py`。HTTP 方法、请求字段、响应字段和权限需要进一步以对应视图函数为准；除明确列出的公开接口外，不应仅凭 URL 名称推断方法或字段。

### 3.1 公开预订与支付

| 路径 | 当前用途 | 说明 |
|---|---|---|
| `GET /availability` | 查询房型库存和价格 | 日期、间数等参数；匿名公开 |
| `POST /reservations` | 创建预订 | JSON；支持幂等键路径 |
| `GET /reservations/{booking_no}` | 查询预订详情 | 当前实现使用 booking_no + phone；存在安全加固待办 |
| `POST /reservations/{booking_no}/cancel` | 取消待支付预订 | 当前使用联系电话校验；存在安全加固待办 |
| `POST /payments/intents` | 创建支付意图 | 当前为本地模拟支付路径，必须确认环境门禁 |
| `POST /payments/callback` | 模拟支付回调 | 仅允许 dev/test，不可用于真实支付 |

### 3.2 前台工作台

| 路径 | 用途 |
|---|---|
| `front-desk/overview` | 前台概览 |
| `front-desk/arrivals` | 今日抵店 |
| `front-desk/departures` | 今日离店 |
| `front-desk/in-house` | 在住客人 |
| `front-desk/reservations` | 预订列表 |
| `front-desk/reservations/{booking_no}` | 预订详情 |
| `front-desk/rooms` | 客房列表 |
| `front-desk/rooms/assignable` | 可分配客房 |
| `front-desk/folios/{booking_no}` | 账单详情 |
| `front-desk/legacy-reservations` | 历史预订列表 |
| `front-desk/legacy-reservations/{legacy_id}` | 历史预订详情 |
| `front-desk/stays/{stay_id}/available-rooms` | 在住订单可换房列表 |
| `front-desk/stays/{stay_id}/extend` | 延住 |
| `front-desk/stays/{stay_id}/change-room` | 换房 |
| `front-desk/reservation-rooms/{reservation_room_id}/no-show` | 标记 No-show |
| `front-desk/reservations/{booking_no}/deposits` | 收取押金 |
| `front-desk/payments/{payment_id}/refunds` | 申请退款 |
| `front-desk/refunds/{refund_id}/approve` | 审批退款 |
| `front-desk/reservation-rooms/{reservation_room_id}/assign` | 分配房间 |
| `front-desk/reservation-rooms/{reservation_room_id}/check-in` | 办理入住 |
| `front-desk/reservations/{booking_no}/payments` | 收款 |
| `front-desk/stays/{stay_id}/check-out` | 办理退房 |

### 3.3 夜审与高风险操作

| 路径 | 用途 |
|---|---|
| `night-audit/preview` | 夜审预览 |
| `night-audit/run` | 执行夜审 |
| `night-audit/business-dates/{business_date_id}/reopen-request` | 申请重开营业日 |
| `night-audit/business-dates/{business_date_id}/approvals/{approval_id}/approve` | 审批重开申请 |
| `night-audit/business-dates/{business_date_id}/break-glass` | 发起紧急访问申请 |
| `night-audit/business-dates/{business_date_id}/break-glass/{record_id}/execute` | 执行紧急访问 |
| `night-audit/break-glass/{record_id}/review` | 复核紧急访问记录 |

### 3.4 住客、客房、房价和财务查询

| 路径 | 用途 |
|---|---|
| `guests` | 住客列表 |
| `guests/{guest_id}` | 住客详情 |
| `guests/create` | 创建住客档案 |
| `guests/{guest_id}/update` | 更新住客档案 |
| `guests/{guest_id}/documents/{document_id}/reveal` | 揭示证件敏感信息 |
| `guests/{guest_id}/phones/{phone_id}/reveal` | 揭示手机号 |
| `rooms/status` | 客房状态 |
| `room-types` | 房型列表 |
| `rate-calendar` | 房价日历 |
| `financial-summary` | 财务汇总 |

### 3.5 审批、预订管理、审计、员工和会员

| 路径 | 用途 |
|---|---|
| `approvals` | 审批列表 |
| `approvals/request` | 发起审批 |
| `approvals/{record_id}/approve` | 同意审批 |
| `approvals/{record_id}/reject` | 拒绝审批 |
| `reservations-mgmt` | PMS 预订管理列表 |
| `reservations-mgmt/{booking_no}` | PMS 预订管理详情 |
| `audit-timeline` | 审计时间线 |
| `daily-rates/{rate_id}/adjust` | 调整每日房价 |
| `staff` | 员工列表 |
| `staff/{profile_id}` | 员工详情 |
| `memberships` | 会员列表 |
| `memberships/{membership_id}` | 会员详情 |
| `memberships/create` | 创建会员 |
| `memberships/{membership_id}/level` | 调整会员等级 |

### 3.6 OpenAPI 文档入口

| 路径 | 用途 |
|---|---|
| `GET /openapi.json` | 获取当前 OpenAPI 3.1 JSON |

## 4. Apifox（APIfox）使用说明

Apifox 是测试人员的首选图形化工具。它可以导入 OpenAPI、维护环境变量、发送请求、保存测试用例和运行测试集合。

### 4.1 导入现有接口

1. 启动 Django 项目：`python manage.py runserver`。
2. 浏览器打开 `http://127.0.0.1:8000/api/v1/openapi.json`，确认能返回 JSON。
3. 打开 Apifox，新建项目，选择导入。
4. 选择 OpenAPI / Swagger，使用 URL 导入：
   `http://127.0.0.1:8000/api/v1/openapi.json`。
5. 导入后设置环境变量：
   - `base_url = http://127.0.0.1:8000`
   - API 前缀为 `/api/v1`
6. 先测试当前 OpenAPI 已覆盖的公开预订接口；其他管理接口按本文件路由目录创建请求，待 OpenAPI 契约补齐后再统一重新导入/同步。

如果 Apifox 无法访问本机 URL，可先在同一网络环境下使用测试服务器地址；不要把本机地址 `127.0.0.1` 配给远程测试人员。

### 4.2 环境变量建议

| 变量 | 示例 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:8000` | 本机或测试服务器 |
| `api_prefix` | `/api/v1` | API 前缀 |
| `booking_no` | 测试数据返回值 | 不要手工猜编号 |
| `test_phone` | 测试专用号码 | 使用虚构测试数据 |
| `csrf_token` | 登录/CSRF 获取后保存 | Session 写接口需要时使用 |
| `session_cookie` | 测试账号登录后获取 | 管理端接口按实际认证要求配置 |

### 4.3 测试用例最低要求

每个接口至少准备：

- 正常成功用例；
- 缺少必填字段；
- 字段类型错误/格式错误；
- 无效 ID 或不存在资源；
- 未登录访问；
- 已登录但无权限；
- 跨角色权限验证；
- 重复提交/幂等；
- 并发或状态冲突；
- 关键业务副作用验证（库存、账务、审计、状态流转）。

高风险接口（退款审批、夜审、break-glass、敏感字段揭示、入住/退房）必须覆盖授权矩阵和审计记录，不允许只验证 HTTP 200。

## 5. Python + Requests + Pytest 自动化测试

### 5.1 安装依赖

```bash
python -m pip install requests pytest
```

建议在项目测试依赖文件中固定版本；不要让测试人员各自随意安装不同版本。

### 5.2 环境变量

Windows PowerShell：

```powershell
$env:PMS_BASE_URL = "http://127.0.0.1:8000"
$env:PMS_API_PREFIX = "/api/v1"
```

macOS / Linux：

```bash
export PMS_BASE_URL="http://127.0.0.1:8000"
export PMS_API_PREFIX="/api/v1"
```

### 5.3 示例：查询房态

```python
# test_public_api.py
import os
import requests

BASE_URL = os.getenv("PMS_BASE_URL", "http://127.0.0.1:8000")
API_PREFIX = os.getenv("PMS_API_PREFIX", "/api/v1")
session = requests.Session()


def test_availability():
    response = session.get(
        f"{BASE_URL}{API_PREFIX}/availability",
        params={
            "check_in": "2026-12-01",
            "check_out": "2026-12-03",
            "rooms": 1,
        },
        timeout=15,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert "data" in body
    assert isinstance(body["data"], list)
```

注意：示例日期仅用于说明格式。测试执行前应确认数据库中存在有效房型、价格和库存，并选择未来日期。

### 5.4 示例：创建预订（必须使用测试数据）

```python
import os
import uuid
import requests

BASE_URL = os.getenv("PMS_BASE_URL", "http://127.0.0.1:8000")
API_PREFIX = os.getenv("PMS_API_PREFIX", "/api/v1")


def test_create_reservation():
    payload = {
        "full_name": "自动化测试住客",
        "phone": "13800000000",
        "room_type_id": int(os.environ["PMS_TEST_ROOM_TYPE_ID"]),
        "check_in": os.environ["PMS_TEST_CHECK_IN"],
        "check_out": os.environ["PMS_TEST_CHECK_OUT"],
        "rooms": 1,
        "adults": 1,
        "children": 0,
    }
    response = requests.post(
        f"{BASE_URL}{API_PREFIX}/reservations",
        json=payload,
        headers={"Idempotency-Key": str(uuid.uuid4())},
        timeout=20,
    )

    assert response.status_code in (200, 201), response.text
    body = response.json()
    assert body.get("booking_no")
```

字段及响应断言必须以当前视图实际返回为准；若返回结构变化，应同步更新接口契约和测试代码。不得在生产环境执行此测试。

### 5.5 运行测试

```bash
pytest -q
```

只运行指定文件：

```bash
pytest -q test_public_api.py
```

生成 JUnit XML（便于 CI 收集）：

```bash
pytest -q --junitxml=reports/api-test-results.xml
```

### 5.6 管理端 Session / CSRF

管理端接口如采用 Django Session 认证，测试脚本应先登录取得 Cookie，再按 Django CSRF 规则发送写请求。不能把用户名密码直接写进仓库，也不能在测试报告、日志中输出 Session Cookie 或 CSRF Token。建议为测试环境创建专用测试账号，并按角色分别运行用例。具体登录 URL、CSRF 获取方式和权限角色应由测试环境配置提供。

## 6. 测试人员执行顺序

1. 确认测试环境、数据库快照和测试账号，不连接生产数据库。
2. 检查 `/api/v1/openapi.json` 是否可访问并导入 Apifox。
3. 先执行匿名公开接口：房态查询、预订创建、订单查询、取消和支付意图。
4. 再按角色执行前台、住客、客房、财务、审批、夜审和员工接口。
5. 对每个写接口检查数据库副作用及 AuditEvent，不只看响应码。
6. 执行无权限、越权、跨物业、重复请求、并发、限流和异常输入用例。
7. 保存 Apifox 测试报告及 Pytest JUnit 报告，关联代码 commit、环境和数据库版本。
8. 缺陷记录必须包含接口、请求方法、脱敏后的请求、响应码、响应体、复现步骤、预期结果、实际结果和关联 request_id。

## 7. 安全和测试数据要求

- 禁止在测试文档、Apifox 环境导出文件、代码仓库中保存真实住客姓名、手机号、证件号、密码、支付凭据或 Session。
- 不要将真实支付回调或生产支付密钥用于本地模拟支付。
- `payments/callback` 当前为 dev/test 模拟回调，生产环境预期应拒绝；必须测试 404 门禁。
- 当前订单查询接口仍有 booking_no + phone 凭据风险；在 Booking Access Token、限流、审计和脱敏完成前，不应把它视为已通过安全验收。
- 不得用生产数据验证退款、夜审、入住、退房、取消、库存调整或敏感字段揭示。
- 所有破坏性测试必须在可重建的测试数据库中执行。

## 8. 当前缺口与后续交付要求

### P0：先补齐可测性和安全契约

- 为全部已注册路由补充准确 HTTP 方法、认证方式、角色权限、请求 Schema、成功响应 Schema、错误响应和示例。
- 完善 `/api/v1/openapi.json`，覆盖当前路由，不得只列公开预订接口。
- 对公共订单查询/取消补齐 Booking Access Token、限流、审计、脱敏，再更新文档。
- 统一错误码和响应封套；对既有接口做兼容性评估后分批迁移。
- 将模拟支付接口标注为 dev/test only，并验证生产门禁。
- 为敏感字段揭示、退款、夜审、break-glass 等高风险 API 写权限矩阵测试。

### P1：测试资产与持续集成

- 建立独立的 `tests/api/` 或等价测试目录。
- 以 Pytest + Requests 实现黑盒 API 冒烟与回归测试。
- 对每个 API 维护 Apifox 请求、环境、测试用例和自动化断言。
- CI 运行接口测试，输出 JUnit XML；测试数据可重复初始化。
- 每次接口变更同步更新 OpenAPI、本文档和测试用例。

### 完成定义

接口文档不能仅以“有一个 OpenAPI URL”作为完成。一个接口达到可交付测试状态，必须同时具备：

1. 准确的路径和 HTTP 方法；
2. 认证与权限要求；
3. 请求参数、类型、必填、边界和示例；
4. 成功响应及所有主要错误响应 Schema；
5. Apifox 可执行请求；
6. 自动化测试覆盖正常、异常、权限和关键副作用；
7. 文档、代码、测试三者与同一 commit 对齐。

## 9. 代码依据

- `apps/api/urls.py`：当前 API 路由注册目录。
- `apps/api/views.py`：公开预订、支付及现有 OpenAPI JSON。
- `apps/core/api_contract.py`：统一响应封套、错误码、分页和幂等装饰器。
- `apps/api/front_desk.py`、`night_audit.py`、`guest_center.py`、`approval.py`、`reservation_center.py`、`staff_center.py`：各业务 API 视图实现。

本文是当前接口盘点和测试入门基线。开发人员必须在补齐每个接口的真实契约后，将 OpenAPI 作为 Apifox 与自动化测试共同使用的唯一接口定义来源。
