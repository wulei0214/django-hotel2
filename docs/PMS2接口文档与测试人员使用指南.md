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
| `GET /reservations/{booking_no}` | 查询预订详情 | 当前使用 Booking Access Token（X-Booking-Token 请求头或 token 查询参数） |
| `POST /reservations/{booking_no}/cancel` | 取消待支付预订 | 请求体使用 Booking Access Token，不使用手机号作为凭据 |
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
- 订单详情已使用 Booking Access Token，并实施限流、访问审计和字段白名单脱敏；仍需持续执行安全回归测试。
- 不得用生产数据验证退款、夜审、入住、退房、取消、库存调整或敏感字段揭示。
- 所有破坏性测试必须在可重建的测试数据库中执行。

## 8. 当前缺口与后续交付要求

### P0：先补齐可测性和安全契约

- 为全部已注册路由补充准确 HTTP 方法、认证方式、角色权限、请求 Schema、成功响应 Schema、错误响应和示例。
- 完善 `/api/v1/openapi.json`，覆盖当前路由，不得只列公开预订接口。
- 公开订单详情/取消/支付已使用 Booking Access Token；后续重点是回归令牌过期、撤销、重放和限流行为。
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


---

# 10. 全系统入口总览：从预约、登录到 PMS 业务模块

> 本节补充全站 URL 入口。必须区分“页面路由”“表单动作”“JSON API”。页面 URL 不等于 REST API；页面通过 HTML 表单提交的写操作也必须纳入测试，但不能伪装成 JSON API。

## 10.1 用户旅程与入口地图

| 业务旅程 | 页面入口 | 实际数据接口/动作 | 当前状态 |
|---|---|---|---|
| 官网首页 | `/`、`/home/` | 首页视图；登录后工作台 | 已实现；根路径行为需结合登录状态验证 |
| 官网客房预订 | `/reserve/` | `GET /api/v1/availability` | 已实现 |
| 提交预订 | `/reserve/` 页面内 | `POST /api/v1/reservations` | 已实现 |
| 预订确认/成功 | `/reserve/` 页面内结果区 | 创建预订响应；非独立成功页 URL | 已实现为同页结果区 |
| 创建支付意图 | `/reserve/` 页面内 | `POST /api/v1/payments/intents` | 本地模拟支付；真实支付未实现 |
| 订单查询 | 当前预订页流程/API | `GET /api/v1/reservations/{booking_no}` | 已实现；使用 Booking Access Token，持续安全回归 |
| 订单取消 | 当前预订页流程/API | `POST /api/v1/reservations/{booking_no}/cancel` | 已实现；使用 Booking Access Token，持续安全回归 |
| 员工登录 | `/login/` | Django Session 登录，HTML Form POST | 已实现；不是 JSON 登录 API |
| 员工退出 | `/logout/` | Django Session logout | 已实现 |
| 强制修改密码 | `/force-password-change/` | HTML Form POST | 已实现；密码策略/页面行为需回归 |
| 公众注册 | `/register/` | 不创建账户，重定向到 `/reserve/` | 已关闭员工/住客公开账号创建 |
| 前台工作台 | `/checkin/` | `/api/v1/front-desk/*` | 页面及部分 API 已实现 |
| 旧预订入口 | `/booking/` | 重定向至 `/reserve/` | 旧写入口冻结 |
| 员工管理 | `/staff_accounts/` | 页面表单 + EmployeeLifecycleService | 页面已实现；迁移至 Employee Center 的工作仍需验收 |
| 管理中心页面 | `/manage/{center}/` | 对应只读 API | 页面框架已实现；不等于所有中心写操作已实现 |
| 夜审 | 无独立新页面承诺 | `/api/v1/night-audit/*` | PMS2 API 已实现部分流程；按权限/状态机验收 |
| Django Admin | `/admin/` | Django Admin | 受控管理入口；不得作为日常业务写入入口 |

## 10.2 认证与账户页面路由

| URL | 方法/类型 | 认证 | 当前行为 | 测试重点 |
|---|---|---|---|---|
| `GET /login/` | 页面 GET | 匿名可访问 | 登录页 | 页面加载、next 参数 |
| `POST /login/` | HTML Form POST | 匿名提交 | username/password 认证；要求有效员工任职 | 正确/错误密码、停用用户、无有效任职、失败审计、重定向安全 |
| `GET /logout/` | 页面/动作 | 登录态 | 注销并跳转登录页 | 注销后 Session 失效 |
| `POST /force-password-change/` | HTML Form POST | 登录态且需强制改密 | 修改密码并清除 must_change_password | 密码校验、两次密码不一致、旧密码失效、标志清除 |
| `GET /force-password-change/` | 页面 GET | 登录态 | 展示强制改密页面 | 无需改密时重定向 |
| `GET /register/` | 页面重定向 | 匿名 | 重定向至 /reserve/，不创建 User | 不得创建 User/Guest/员工账号 |
| `GET /staff_accounts/` | 页面 GET | 员工登录及权限 | 员工账号工作台 | 角色访问控制、分页、搜索 |
| `POST /staff_accounts/create/` | 表单动作 | 员工管理权限 | 创建员工档案/任职 | 参数校验、工号唯一、审计 |
| `GET /staff_accounts/{pk}/` | 页面 GET | 员工管理权限 | 员工详情 | 数据范围、敏感字段遮罩 |
| `POST /staff_accounts/{pk}/provision/` | 表单动作 | 员工管理权限 | 开通登录账号 | 用户名唯一、初始密码策略、强制改密 |
| `POST /staff_accounts/{pk}/assign_role/` | 表单动作 | 员工管理权限 | 分配岗位角色 | 角色范围、授权审计 |
| `POST /staff_accounts/{pk}/end_assignment/{assignment_id}/` | 表单动作 | 员工管理权限 | 结束岗位授权 | 历史保留、不可越权 |
| `POST /staff_accounts/{pk}/set_status/` | 表单动作 | 员工管理权限 | 修改员工/任职状态 | 停用后立即失权 |
| `POST /staff_accounts/{pk}/reset_password/` | 表单动作 | 员工管理权限 | 管理员重置密码 | must_change_password=True、审计 |
| `GET /permissions/` | 页面 GET | 管理权限 | 权限管理页面 | 角色权限显示与服务端一致 |
| `POST /permissions/save/` | 表单动作 | 管理权限 | 保存角色权限 | 防 CSRF、权限变更审计、禁止越权 |

说明：员工邀请链接、邀请 token 生命周期、邀请撤销/重发等属于 2.0 目标设计；在当前代码没有对应 URL/API 前，必须标记为“待开发”，不得由测试人员假设已存在。

## 10.3 官网预订页面与公开接口

| URL | 方法 | 认证 | 请求/响应概要 | 状态 |
|---|---|---|---|---|
| `GET /reserve/` | GET | 匿名 | 返回官网预订页 | 已实现 |
| `GET /api/v1/availability` | GET | 匿名 | query: check_in、check_out、rooms、room_type_id；返回物业、日期、间数、房型可售数据 | 已实现 |
| `POST /api/v1/reservations` | POST | 匿名 | JSON: full_name、phone、room_type_id、可选 rate_plan_id、check_in、check_out、rooms、adults、children；支持 Idempotency-Key | 已实现 |
| `GET /api/v1/reservations/{booking_no}` | GET | 匿名凭据校验 | 通过 X-Booking-Token 请求头或 token 查询参数校验 | 已实现；高风险安全待整改 |
| `POST /api/v1/reservations/{booking_no}/cancel` | POST | 匿名凭据校验 | JSON token；取消待支付预订 | 已实现；高风险安全待整改 |
| `POST /api/v1/payments/intents` | POST | 匿名凭据校验 | JSON booking_no、token；返回模拟支付意图 | 仅 dev/test 模拟支付 |
| `POST /api/v1/payments/callback` | POST | 回调签名 | JSON provider_ref、amount、status；X-Signature | dev/test only |

当前预订接口的成功响应与错误格式并非统一封套。测试断言应以 `apps/api/views.py` 真实响应为准。公开订单访问凭据为 Booking Access Token；不得在测试用例中继续使用手机号作为订单访问凭据。

## 10.4 PMS 页面路由（HTML 页面，不是 JSON API）

下列路由来自 `hotel/urls.py`。除特别说明外，页面路由的访问权限由对应视图装饰器控制。路由清单不代表每条路径都已达到 PMS2 新业务验收标准。

| URL | 页面/用途 | 当前说明 |
|---|---|---|
| `/` | 根入口 | 进入首页视图 |
| `/home/` | PMS 首页 | 登录员工仪表盘 |
| `/manage/{center}/` | 通用管理中心 | 当前包括 staff、guests、rooms、memberships 等配置 |
| `/checkin/` | 前台接待工作台 | PMS2 前台工作台 |
| `/checkin/legacy/` | 旧入住管理页 | 旧 Accommodation 数据；仅历史兼容，需按退役计划治理 |
| `/checkin_confirm/` | 旧排房确认动作 | 旧业务写路径，待退役核查 |
| `/checkin_in/` | 旧办理入住动作 | 旧业务写路径，待退役核查 |
| `/cancel_booking/` | 旧取消预订动作 | 旧业务写路径，待退役核查 |
| `/checkout/` | 旧退房动作 | 旧业务写路径，待退役核查 |
| `/uncheckout/` | 旧反结账动作 | 财务高风险旧写路径，需重点核查/退役 |
| `/extend_stay/` | 旧延住动作 | 旧业务写路径，待退役核查 |
| `/change_room/` | 旧换房动作 | 旧业务写路径，待退役核查 |
| `/express_checkout/` | 旧快速退房动作 | 旧业务写路径，待退役核查 |
| `/walkin_create/` | 旧散客开单动作 | 旧业务写路径，待退役核查 |
| `/no_show_one/` | 旧 No-show 动作 | 旧业务写路径，待退役核查 |
| `/folio/` | 旧账单查询/动作入口 | 需核查是否仍有写行为 |
| `/nightaudit/` | 旧夜审页面 | 与 PMS2 夜审服务边界需核查 |
| `/nightaudit_run/` | 旧夜审执行 | 已退役，预期 410 Gone |
| `/room_status/` | 旧房态变更动作 | 旧写路径，待退役核查 |
| `/finance/` | 旧财务页面 | 旧模块兼容入口 |
| `/finance_add/` | 旧财务新增动作 | 财务旧写路径，重点核查/退役 |
| `/system/` | 系统信息页面 | 登录员工 |
| `/staff_toggle/` | 旧员工启停动作 | 旧写路径，需核查是否仍可修改旧 Staff |
| `/export/staff/` | 员工导出 | 敏感数据导出，审批和审计要求 |
| `/export/staff/request/` | 员工导出申请 | 高敏感数据导出审批流程 |
| `/export/staff/approve/{approval_id}/` | 员工导出审批 | 申请人与审批人分离 |
| `/export/{module}/` | 模块导出 | 数据范围、敏感字段和审计 |
| `/check_unique/` | 旧表单查重 JSON | 旧兼容接口；不等于正式 REST API |
| `/client/reveal_id/` | 旧住客证件揭示 | 高敏感旧入口，必须核查权限码与审计 |
| `/vip/reveal_tel/` | 旧会员电话揭示 | 高敏感旧入口，必须核查权限码与审计 |
| `/admin/` | Django Admin | 受控后台，不作为日常业务写入口 |

## 10.5 历史 hotelapp 模块路由

以下路由仍在 URL 配置中注册。它们属于历史模块兼容入口，不得因此被理解为 PMS2 新 API。凡可能写入旧模型的入口，应按统一业务模型治理方案逐条冻结、只读化或退役。

| 模块 | 路由 |
|---|---|
| 员工 | `/index/`、`/insert/`、`/delete_staff/`、`/search_staff/`、`/update_staff/` |
| 住客 | `/show_client/`、`/insert_client/`、`/delete_client/`、`/update_client/`、`/search_client/` |
| 餐饮 | `/insert_food/`、`/show_food/`、`/delete_food/`、`/update_food/`、`/search_food/` |
| 会员 | `/insert_vip/`、`/show_vip/`、`/delete_vip/`、`/update_vip/`、`/search_vip/` |
| 停车 | `/insert_park/`、`/show_park/`、`/delete_park/`、`/update_park/`、`/search_park/` |
| 客房 | `/insert_room/`、`/show_room/`、`/delete_room/`、`/update_room/`、`/search_room/` |
| 餐饮订单 | `/insert_order/`、`/show_order/`、`/delete_order/`、`/search_order/` |
| 历史住宿单 | `/insert_accomodation/`、`/delete_accomodation/`、`/search_accomodation/`、`/show_accomodation/` |

注意：`Order` 是历史餐饮订单模型，不是 PMS2 客房预订；`Accommodation` 是历史住宿单模型。测试人员不得混淆它们与 `apps.reservations.Reservation`。

## 10.6 当前已注册 PMS2 API 完整路径清单

以下均以 `/api/v1/` 为前缀。HTTP 方法必须以视图上的 `require_GET/require_POST` 或等价装饰器为准；本清单列出全部已注册路径，不以 REST 命名习惯猜测方法。

### 公开预订和支付
- `GET /availability`
- `POST /reservations`
- `GET /reservations/{booking_no}`
- `POST /reservations/{booking_no}/cancel`
- `POST /payments/intents`
- `POST /payments/callback`
- `GET /openapi.json`

### 前台接待
- `/front-desk/overview`
- `/front-desk/arrivals`
- `/front-desk/departures`
- `/front-desk/in-house`
- `/front-desk/reservations`
- `/front-desk/reservations/{booking_no}`
- `/front-desk/rooms`
- `/front-desk/rooms/assignable`
- `/front-desk/folios/{booking_no}`
- `/front-desk/legacy-reservations`
- `/front-desk/legacy-reservations/{legacy_id}`
- `/front-desk/stays/{stay_id}/available-rooms`
- `/front-desk/stays/{stay_id}/extend`
- `/front-desk/stays/{stay_id}/change-room`
- `/front-desk/reservation-rooms/{reservation_room_id}/no-show`
- `/front-desk/reservations/{booking_no}/deposits`
- `/front-desk/payments/{payment_id}/refunds`
- `/front-desk/refunds/{refund_id}/approve`
- `/front-desk/reservation-rooms/{reservation_room_id}/assign`
- `/front-desk/reservation-rooms/{reservation_room_id}/check-in`
- `/front-desk/reservations/{booking_no}/payments`
- `/front-desk/stays/{stay_id}/check-out`

### 夜审与紧急访问
- `/night-audit/preview`
- `/night-audit/run`
- `/night-audit/business-dates/{business_date_id}/reopen-request`
- `/night-audit/business-dates/{business_date_id}/approvals/{approval_id}/approve`
- `/night-audit/business-dates/{business_date_id}/break-glass`
- `/night-audit/business-dates/{business_date_id}/break-glass/{record_id}/execute`
- `/night-audit/break-glass/{record_id}/review`

### 住客、客房、房价和财务摘要
- `/guests`
- `/guests/{guest_id}`
- `/guests/create`
- `/guests/{guest_id}/update`
- `/guests/{guest_id}/documents/{document_id}/reveal`
- `/guests/{guest_id}/phones/{phone_id}/reveal`
- `/rooms/status`
- `/room-types`
- `/rate-calendar`
- `/financial-summary`
- `/daily-rates/{rate_id}/adjust`

### 审批、预订管理、审计、员工和会员
- `/approvals`
- `/approvals/request`
- `/approvals/{record_id}/approve`
- `/approvals/{record_id}/reject`
- `/reservations-mgmt`
- `/reservations-mgmt/{booking_no}`
- `/audit-timeline`
- `/staff`
- `/staff/{profile_id}`
- `/memberships`
- `/memberships/{membership_id}`
- `/memberships/create`
- `/memberships/{membership_id}/level`

## 11. 登录和账户测试场景（必须执行）

### 11.1 登录正向场景
1. 有效员工账号 + 正确密码 + 有效任职：允许登录。
2. 登录成功后跳转默认工作台或经过安全校验的 next 地址。
3. 首次登录/管理员重置密码账号：必须进入强制改密流程。
4. 修改密码成功后 must_change_password 清除，重新登录使用新密码。

### 11.2 登录负向和安全场景
1. 用户名不存在、密码错误：对外错误提示一致，不泄露账号是否存在。
2. User 已停用：拒绝登录。
3. User 存在但没有有效 StaffProfile/任职：拒绝登录。
4. 员工离职、任职结束或账号停用后：旧 Session/API 权限立即失效或按明确会话策略失效。
5. `next=//evil.example`、外域绝对 URL、反斜杠混淆 URL：不得跳转外部站点。
6. 登录失败必须写审计；日志不得记录明文密码。
7. GET 登出与 POST 登出策略需明确；如保留 GET，验证 CSRF/跨站触发风险并按安全决策整改。
8. CSRF 缺失/错误时，所有 Session 写操作均应拒绝。

### 11.3 员工账户生命周期
- 新员工仅建档，不自动产生 User。
- 开通账号时 username 全局唯一。
- 同手机号可由不同员工/住客共用，不得作为唯一账号键。
- 角色分配写入 StaffRoleAssignment，不以 User.groups 作为授权来源。
- 管理员重置密码后必须强制改密。
- 停用员工、结束任职、撤销岗位授权后验证所有 API 入口拒绝访问。
- 邀请激活接口在未开发前标记为待开发；开发完成后补充一次性 token、过期、撤销、重放、错误 token 测试。

## 12. Apifox 项目建议结构

导入 OpenAPI 后，按业务域建立目录，不要把所有请求堆在一个文件夹：

1. `00-环境与认证`
2. `01-官网预约与订单`
3. `02-员工身份与账户`
4. `03-前台接待`
5. `04-住客中心`
6. `05-房态库存与房价`
7. `06-财务收款与退款`
8. `07-审批`
9. `08-夜审与营业日`
10. `09-员工与会员`
11. `10-审计与敏感数据`
12. `90-历史兼容入口`
13. `99-安全与越权测试`

页面表单动作、HTML 重定向和旧系统入口不应强行导入 OpenAPI；它们应建立为页面测试/浏览器测试用例。REST JSON API 才进入 OpenAPI/Apifox API 定义。

## 13. 本文档的下一轮补齐任务

当前版本已经覆盖用户旅程、页面路由、旧兼容入口和全部注册 API 路径，但并不等于所有 API 的完整字段级契约已完成。下一轮按以下顺序逐个读取视图源码并补齐：

1. 认证与员工账户：登录、强制改密、员工创建/开通/角色/停用/重置密码；邀请接口属于待开发。
2. 公开预订：请求/响应 Schema、错误码、幂等、限流、订单访问凭据。
3. 前台接待：列表筛选、分配房间、入住、押金、收款、退房、延住、换房、No-show、退款。
4. 住客与敏感信息：字段遮罩、揭示权限码、审计。
5. 房态/房价/库存：查询筛选、调整规则、库存一致性。
6. 财务与审批：查看权限、退款申请、双人审批、不可篡改账务。
7. 夜审：预览、执行、重开营业日、break-glass 双人复核。
8. 员工与会员：员工查询、会员建档、等级变更。
9. 历史兼容路由：冻结/只读/410 状态逐项核验。

每一组完成后必须同步更新 OpenAPI JSON、Apifox 项目和自动化测试；文档状态应绑定 commit SHA。未经源码核验的字段、状态码、权限码一律标记“待核实”，不得凭经验补写。


---

# 14. Swagger / OpenAPI 在线接口文档（实施版）

## 14.1 在线入口

| 地址 | 用途 |
|---|---|
| `/api/docs/` | Swagger UI：查看接口、填写参数、发送请求 |
| `/api/redoc/` | ReDoc：阅读式接口文档 |
| `/api/v1/openapi.json` | OpenAPI 3.1 JSON；Apifox 导入源 |
| `/api/v1/openapi.json` | 保留既有兼容入口，不改变 URL |

Swagger UI 和 ReDoc 使用 CDN 加载前端静态资源；部署环境浏览器需能访问对应 CDN。OpenAPI JSON 本身由 Django 本地生成，不依赖第三方文档服务。

## 14.2 Apifox 导入

1. 启动目标 PMS2 环境，先在浏览器访问 `/api/v1/openapi.json`，确认返回 JSON。
2. 在 Apifox 项目中选择导入 → OpenAPI/Swagger。
3. 输入完整地址 `https://你的测试域名/api/v1/openapi.json`，或下载 JSON 后从文件导入。
4. 配置测试环境 Base URL（例如 `http://127.0.0.1:8000`）。
5. 员工接口先通过浏览器登录 `/login/`，确保请求共享 Django `sessionid` Cookie；Session 写操作还需有效 CSRF Cookie 与 `X-CSRFToken` 请求头。
6. 公开预订查询/取消/支付使用 Booking Access Token；首次创建预订后保存响应中的 `access_token`，不要用手机号替代。
7. 模拟支付仅允许 DEBUG 且 DJANGO_ENV 为 dev/test 的环境；不得在生产环境测试模拟支付。

## 14.3 文档范围与状态

本次将当前 `apps/api/urls.py` 注册的业务 API 路径及真实 GET/POST 方法纳入 OpenAPI，包含公开预订、前台接待、夜审、住客、房态、房价、财务摘要、审批、预订管理、审计、员工和会员。

OpenAPI operation 扩展字段 `x-contract-status=implemented-route-field-contract-partial` 表示：路由已注册且方法已从视图装饰器核验，但部分 DTO 字段、全部错误响应和权限码仍需继续逐接口核对。不得将“路由已登记”解释为字段级契约全部验收。

以下不是 JSON API，不会伪造为 Swagger 接口：
- `/login/`、`/logout/`、`/force-password-change/`：Django Session HTML 页面/表单流程；
- `/staff_accounts/`、`/permissions/`：现有 HTML 页面和表单动作；
- `/reserve/`：官网页面；
- 历史 `hotelapp` URL：旧页面/兼容路由。

## 14.4 认证和测试边界

- 员工 API 使用 Django Session Cookie（`sessionid`），不是 Bearer Token。
- Session 写请求受 Django CSRF 保护；Apifox 应先获取 CSRF Cookie，再设置 `X-CSRFToken`。
- 公开预订创建为匿名 API；成功响应的一次性 `access_token` 是订单访问凭据。
- 公开订单详情支持 `X-Booking-Token` 请求头或 `token` 查询参数；取消、模拟支付请求体使用 `token`。
- 统一响应封套仅适用于采用 `apps.core.api_contract.api_view` 的新 API；公开预订及部分前台 API 保留各自响应结构，测试断言必须遵循对应接口 Schema。
- Swagger 的“Try it out”会向当前站点发送真实请求；请仅在测试环境操作，避免对生产数据执行写入。

## 14.5 本次实现文件

- `apps/api/openapi_schema.py`：OpenAPI 3.1 规范生成器、Swagger UI、ReDoc 页面。
- `apps/api/views.py`：既有 `openapi_document` 兼容入口委托给统一规范生成器。
- `hotel/urls.py`：注册 `/api/docs/` 和 `/api/redoc/`。
- `apps/api/test_openapi_schema.py`：文档版本、主要业务路径、Booking Access Token 描述和 UI 路由测试。

## 14.6 尚待继续完成

1. 对所有业务视图逐字段核验请求参数、DTO 响应字段、状态码和业务错误码。
2. 为分页、列表筛选、金额/日期格式和敏感字段遮罩建立可复用 Schema。
3. 将权限码、角色限制和物业数据范围精确到每个 operation。
4. 补充前台入住、退款、夜审、break-glass、住客敏感信息揭示等写操作的集成测试。
5. 由测试人员在实际测试环境导入 Apifox 并验证 Cookie/CSRF、幂等、限流和错误响应。

本次先完成在线文档入口、当前 API 路由/方法覆盖和基础自动化测试；字段级契约的剩余工作继续按模块验收，不宣称已全部完成。
