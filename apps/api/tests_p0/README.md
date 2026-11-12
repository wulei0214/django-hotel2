# apps/api/tests_p0 — P0 真实 HTTP 冒烟套件（Pytest + Requests）

《PMS2 平台身份权限与业务全链路治理开发实施方案》§9.11 交付物：
对**已启动的真实 HTTP 实例**执行 P0 链路冒烟（不使用 Django test client，
不加载 Django settings，与既有 Django 单测完全隔离，二者可并行维护）。

> 位置说明（2026-11-13 迁移，多 AI 协作治理方案 §1.3 方案 B）：
> 原独立目录 `api_tests/` 已并入 `apps/api/tests_p0/`。未命名为 `tests/`
> 是因为 `apps/api/tests.py`（Django 单测）与 `tests/` 包存在模块名冲突；
> 统一的 pytest 配置收敛在根 `pyproject.toml`（原 `api_tests/pytest.ini` 已废除）。

## 覆盖范围（`@pytest.mark.p0`）

| # | 用例 | 断言要点 |
|---|------|----------|
| 1 | 公开面 | `GET /reserve/` 200；`GET /api/v1/availability` 200 且 `data` 结构 |
| 2 | 公开预订 | `POST /api/v1/reservations`（唯一 `Idempotency-Key`）→ 201 + `booking_no` + `access_token`；同 key 重放 → 200 幂等 |
| 3 | token 查询 | `X-Booking-Token` → 200 姓名脱敏、无完整手机号；错 token → 404 |
| 4 | token 取消 | → 200 `status=CANCELLED` 且库存释放（availability 前后对比） |
| 5 | 员工登录流 | `POST /login/` 表单流 → `GET /home/` 200；无凭据 `front-desk/overview` → 401（POST 另受 require_GET 短路为 405，一并接受 302/401/405） |
| 6 | 权限拒绝 | 非超管员工 `GET /permissions/` → 302；`GET /platform/permissions/` → 403 |
| 7 | 旧路由退役 | `POST /insert_client/` → 410；`GET /booking/` → 302 `/reserve/` |
| 8 | OpenAPI | `GET /api/v1/openapi.json` → 200 且 `openapi` 3.1 且 operation 数 ≥ 60 |

## 依赖

Python 3.10+，仅需 `pytest`、`requests`（本仓库 `.venv` 已安装）：

```powershell
.venv\Scripts\python.exe -m pip install pytest requests
```

## 运行前：准备被测实例

用例打的是**真实 HTTP 服务**，必须先有一个已迁移、已种子化的实例。
两种方式任选：

### 方式 A：本机新建冒烟实例（推荐，文件库可随时重建）

```powershell
# 1. 迁移到独立文件库（不会动开发/生产库）
$env:DJANGO_DB_ENGINE="sqlite"
$env:DJANGO_DB_NAME="_pytest_smoke.sqlite3"
.venv\Scripts\python.exe manage.py migrate

# 2. 种子演示数据：店长账号 + 房型/房价计划/库存日历
#    （交互式 shell，按提示粘贴；或用你自己的种子脚本）
.venv\Scripts\python.exe manage.py shell
#   >>> 示例种子（Group「店长」由迁移 0001 自动创建）：
#   >>> from django.contrib.auth.models import User, Group
#   >>> from apps.identity.models import StaffProfile, PropertyStaffMembership, StaffRoleAssignment
#   >>> from apps.core.models import Property
#   >>> from apps.inventory.models import RoomType, RatePlan, DailyRate, RoomTypeInventory
#   >>> from django.utils import timezone; from datetime import timedelta; from decimal import Decimal
#   >>> p = Property.objects.get(code="DEFAULT")
#   >>> u = User.objects.create_user("demo_owner", password="Demo@Owner2026")
#   >>> prof = StaffProfile.objects.create(user=u, full_name="演示店长", employment_status="ACTIVE")
#   >>> m = PropertyStaffMembership.objects.create(staff_profile=prof, property=p, employee_no="SMOKE-001", status="ACTIVE")
#   >>> StaffRoleAssignment.objects.create(membership=m, group=Group.objects.get(name="店长"), starts_at=timezone.now()-timedelta(minutes=1))
#   >>> rt = RoomType.objects.create(property=p, code="SMOKE-KING", name="冒烟大床房", capacity=2)
#   >>> rp = RatePlan.objects.create(property=p, code="SMOKE-BAR", name="冒烟门市价", valid_from=timezone.localdate()-timedelta(days=1))
#   >>> for i in range(5):
#   ...     d = timezone.localdate() + timedelta(days=i)
#   ...     DailyRate.objects.create(property=p, rate_plan=rp, room_type=rt, stay_date=d, price=Decimal("300.00"))
#   ...     RoomTypeInventory.objects.create(property=p, room_type=rt, stay_date=d, total_rooms=5)

# 3. 启动（保持窗口不关）
.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

### 方式 B：指向已运行的实例

设 `PMS_BASE_URL` 指向该实例（须满足：`DEFAULT` 物业 ACTIVE、存在
带角色授权的员工账号、未来数日有可售房型库存），跳过方式 A。

## 运行测试

```powershell
# 在仓库根目录运行（统一 pytest 配置来自 pyproject.toml）
.venv\Scripts\python.exe -m pytest apps/api/tests_p0 -m p0
```

常用变体：

```powershell
# 指定其它实例
$env:PMS_BASE_URL="http://127.0.0.1:8001"; .venv\Scripts\python.exe -m pytest apps/api/tests_p0 -m p0

# 指定员工测试账号（缺省 demo_owner / Demo@Owner2026，仅本机演示凭据）
$env:PMS_STAFF_USER="demo_owner"; $env:PMS_STAFF_PASSWORD="你的口令"
```

> 仓库根 `pyproject.toml` 的全局 addopts 默认 `-m "not p0"`——日常裸跑
> `pytest` 只收集 Django 单测，不会误打真实实例；冒烟套件必须显式 `-m p0`。

环境变量一览：

| 变量 | 默认 | 说明 |
|------|------|------|
| `PMS_BASE_URL` | `http://127.0.0.1:8000` | 被测实例地址 |
| `PMS_STAFF_USER` | `demo_owner` | 员工登录测试账号（需有 DEFAULT 有效任职 + 角色授权） |
| `PMS_STAFF_PASSWORD` | `Demo@Owner2026` | 上述账号口令（缺省为本机演示凭据，正式环境必须覆盖） |

## 测试报告（JUnit XML）

每次运行可生成 `reports/junit-p0.xml`（`reports/` 已加入 .gitignore）：

```powershell
.venv\Scripts\python.exe -m pytest apps/api/tests_p0 -m p0 --junitxml=reports/junit-p0.xml
```

CI 或质量门禁直接消费该文件；失败用例的 system-output 不含口令、
Session Cookie、CSRF token、Booking Access Token 或完整手机号。

## 限流提示（§6.3，fail-closed）

- `public.reservation_create`：IP 10 次/10 分钟、同手机号 3 次/10 分钟——
  本套件每轮运行 1 笔预订（创建+重放共 2 次写请求），手机号/幂等键每轮唯一；
  **短时间内高频重跑可能触发 429**，可重启实例（重建文件库）或等待窗口滚动。
- `auth.login`：IP+用户名 5 次/5 分钟——套件每轮仅登录 1 次。

## 目录结构

```
apps/api/tests_p0/
  conftest.py        # BASE_URL / requests 封装 / 表单登录 fixture / 环境变量
  test_p0_smoke.py   # P0 八条冒烟链路（@pytest.mark.p0）
  README.md          # 本文件
reports/             # junit-p0.xml（运行时生成，gitignore）
```
