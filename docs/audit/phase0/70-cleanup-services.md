# PMS2 Phase0 任务 2：语法审查 + 代码精简（services 清理）报告

- 执行：任务 2 子智能体（code-review + tech-debt-audit + debug-assistant 方法论）
- 日期：2026-11-07
- 范围：`apps/` 与 `hotelapp/` 的 `.py` 代码（语法/死代码/重复逻辑/P3 卫生）；**不改**业务行为、接口字段、权限规则；**不动** docs/ 既有内容、根目录文件、migrations
- 基线：main @ 635d9dd，全量 872 项测试

## 一、语法审查结论（任务清单 1）

| 检查 | 对象 | 结果 |
|---|---|---|
| `py_compile` 逐文件 | 全仓 245 个项目 .py（排除 venv/staticfiles/_idea） | **0 错误** |
| `node --check` | static/js 与 static/public/ui 共 12 个项目自有 JS（排除第三方 min.js） | **0 错误** |
| `manage.py check` | Django 系统检查 | **0 issues** |
| 模板 | 由 `manage.py check` 覆盖 | 无问题 |

**语法问题清单：空**（无需修复项）。

## 二、TD-3 死代码清理（已修 12 项）

删除前逐项做了全仓引用计数（corpus = 378 个 py/html/js/txt/md/json/css/sql 等项目文件，含 templates、tests、management commands、migrations、settings），并排除动态分发可能（`getattr` 字符串调度、`import_string`/`importlib`、`globals()[...]`、通配符 import 复核——全仓唯一通配符 `hotelapp/admin.py` 仅导入 models）。凡名字在其他文件出现过（哪怕只是同名不同实现）一律不做名字级误判，改为逐模块追踪 import 图。

| # | 位置 | 死代码项 | 证据 |
|---|---|---|---|
| 1 | hotelapp/services/audit.py（整文件） | `log_audit` + 私有助手 `_json_safe` | 模块被 0 处 import（`from hotelapp.services import ...`/`import audit` 全仓零命中）；AuditLog 模型仍由 models.py 提供，迁移不受影响 |
| 2 | hotelapp/services/ledger.py | `post_room_night` | 仅定义；ledger.py 只被 services/reservations.py 导入，而后者只用 `money`/`post_entry` |
| 3 | hotelapp/services/ledger.py | `accommodation_summary` | 同上 |
| 4 | hotelapp/services/ledger.py | `reservation_summary` | 同上 |
| 5 | hotelapp/services/ledger.py | `void_accommodation` | 同上 |
| 6 | hotelapp/services/ledger.py | `reverse_entry`（名字与 BillingService.reverse_entry 撞名，名字计数有假阴性，已按模块级 import 图单独验证） | hotelapp 侧零调用 |
| 7 | hotelapp/services/ledger.py | `_active` / `_sum`（仅被 3/4 的死函数引用，随之删除）+ 常量 `CHARGE_TYPES`/`PAYMENT_TYPES` | 仅死函数引用 |
| 8 | hotelapp/services/reservations.py | `confirm_reservation` | 定义外全仓零出现（api.py 只用 `create_reservation`/`confirm_payment`） |
| 9 | hotelapp/services/reservations.py | `cancel_reservation` | 同上（`release_expired_reservations` 命令用的是 apps.inventory.services，与此无关） |
| 10 | hotelapp/management/commands/seed_hotel_data.py | `LegacyLinkFillMixin`（43 行） | 类从未被继承：Command.handle 内联了同逻辑（live 版带 `match_method` 字段，mixin 版是旧稿） |
| 11 | apps/api/openapi_schema.py | `s_num` / `s_null` 构造器 | schema 内零使用（`s_nullable`/`s_str` 等其余构造器均在用，保留） |
| 12 | apps/identity/registry.py | `get_module` / `registry_warnings` | 零调用（同文件的 `get_special`/`registry_errors` 在用，保留） |

**登记不修（TD-3，证据不足/超所有权）**：
- 【配置债·移交仓库清理智能体】`.gitignore:37` 把整个 `hotelapp/services/` 目录排除在版本控制外（本次 ledger.py 精简 149→41 行、reservations.py 193→158 行、audit.py 删除均无法进 git）。服务层不入库是高风险漂移点，建议尽快 `git add -f hotelapp/services/` 或修正 .gitignore。根目录 .gitignore 不在本任务所有权内，故仅登记。
- 测试类（Django runner 自动发现）、`@admin.register` 装饰器注册的 ModelAdmin、`hotelapp/apps.py:HotelappConfig`（INSTALLED_APPS 隐式加载）、`hotelapp/views.py:71 from . import handlers`（信号订阅副作用 + noqa + 业务注释）均为隐式引用，非死代码，已从扫描中排除。

## 三、TD-2 重复逻辑收敛（已修 6 组，消除 10 份重复副本）

原则：只改消费方 import/调用点；权威实现语义一字未动；收敛前后异常类型与文案逐字节比对一致；无循环 import（逐组验证依赖方向）。

| # | 重复实现 | 权威 | 收敛动作 |
|---|---|---|---|
| 1 | `models_q_valid_to`（inventory/services.py 与 reservations/services.py 两份逐字相同） | apps/inventory/services.py | reservations/services.py 删本地副本，扩展现有 `from apps.inventory.services import` |
| 2 | `_property_today` ×4（reservations/front_desk_services、reservations/lifecycle_services、inventory/services、api/report_center，语义逐字相同） | apps/inventory/services.py（依赖图底层，其余三方本就依赖它） | 三份副本删除，改 import |
| 3 | `_business_date`（api/front_desk.py，与 `_property_today` 同算法的第 5 份日期副本） | 同上 | front_desk 删本地实现，4 处调用点改调 `_property_today` |
| 4 | `_body` ×4 两两相同（front_desk≡views 抛 ValueError；housekeeping_center≡night_audit 抛 ApiError） | views._body（ValueError 口径）、night_audit._body（ApiError 口径，含 WB-46-01 注释） | front_desk 扩展现有 `from .views import`；housekeeping_center 改 `from .night_audit import _body` |
| 5 | `_money_str`（api/billing_center 与 api/report_center 行为相同） | apps/api/billing_center.py（账务契约口径） | report_center 删副本改 import |
| 6 | `_parse_day` + `_property_tz_bounds`（audit_center 与 report_center 逐字相同，audit 注释自认"同 report_center"） | apps/api/report_center.py | audit_center 删两份副本，改一行 import |

**护栏测试**：新增 `apps/api/test_dup_convergence_guards.py`（5 个用例，对象同一性 `is` 断言锁定全部权威引用，防止副本再分叉）。

**登记不修（TD-2，收敛会改变行为/语义不同）**：
| 项 | 位置 | 不修理由 |
|---|---|---|
| `_parse_date` ×3 | api/front_desk.py、api/night_audit.py、hotelapp/api.py | 三版异常类型与回退语义均不同（ValueError+fallback / ApiError / strptime 严格格式），收敛即改行为 |
| `normalize_phone` ×2 | reservations/services.py（纯数字+6-20 校验）vs guests/services.py（保留 + 号，无校验） | 语义不同，收敛会改变校验行为 |
| `_money` ×2 | reservations/front_desk_services.py vs lifecycle_services.py | 异常类型与文案不同（FrontDeskOperationError vs LifecycleOperationError），且分位校验细节有差异 |
| `_json_body` ×4 | api/approval.py（无 dict 校验）、api/billing_center.py（dict+ApiError）、identity/permission_center.py（JsonResponse 封套）、hotelapp/api.py（错误返 None） | 四版契约互不相同 |
| `_context` ×5 | api 五个中心 | 权限拒绝文案按中心定制（清洁维修/夜审/报表/审计等），属业务语义 |
| `_make_user` ×3 | identity/test_module_gates.py、test_staff_center.py、hotelapp/tests.py | 测试夹具口径不同（是否建组/状态参数）；test_auth_hardening 已示范按需复用 hotelapp.tests._make_user |
| `mask_idcard`/`mask_tel`/`_mask`/`_mask_phone` | hotelapp/templatetags/pms_filters.py、guests/services.py、api/front_desk.py | 短值行为不同（模板透传 vs "已隐藏" vs 全打码）；`_mask_phone` 注释已声明与中心口径对齐阈值为 8，但回退行为仍有差异——统一属脱敏口径决策（TD-1 候选），超出"不影响功能"红线，移交后续批次 |
| `BillingService._business_date`（staticmethod） | apps/billing/services_v2.py | 与 `_property_today` 同算法；但收敛需改服务类公共接口及其内部多个调用点，本批不动，登记排期 |
| `_post_json` ×3 | 三个 API 测试文件 | 测试辅助，行为有差异，收敛收益低 |

## 四、P3 卫生（已修）

- **未使用 import**：AST 全仓扫描 + 逐项人工复核（含字符串常量、动态引用排除），清理 **66 个未使用导入名 / 38 个文件**（apps/api/views.py 8 项、lifecycle_services.py 9 项等；其中 ~20 项是本次死代码删除/收敛产生的连带孤儿导入，其余为存量）。有意保留：`hotelapp/views.py:71 from . import handlers`（信号注册副作用 + noqa + 业务注释）。
- **空函数/空类**：扫描结果 **0**（无 pass-only 函数、无空壳类）。
- **注释掉的代码块（≥10 行陈旧块）**：扫描结果 **0**。
- **业务语义注释**：一律未改动（含 WB-46-01、P0 验收修复等口径说明原样保留）。

## 五、回归验证

| 验证项 | 结果 |
|---|---|
| 清理后全量 `manage.py test --noinput`（DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite） | **Ran 872 tests — OK**（零回归） |
| 含新增护栏的全量复跑 | 872 + 5 = **877 tests — OK** |
| `manage.py check` | **0 issues** |
| `py_compile` 全仓 245 文件 | **0 错误** |
| 基线期发现 | 首跑 `test_login_throttled_after_failures` 失败（200≠429）为**墙钟对齐窗口跨界**间歇性失败：`apps/core/rate_limit.py` 的窗口按 `int(now.timestamp()) % window` 对齐墙钟，测试 6 次请求跨 5 分钟边界时计数被拆分进两个窗口、均不超阈。隔离复跑通过、后续两次全量均通过。登记为测试债（建议改用滑动窗口或注入时钟），本批不修 |

## 六、变更量

- git 跟踪区：40 个 .py 文件，+41 / −206 行（docs/PMS3.0 方案文档 +62 行为并行智能体改动，与本任务无关）
- gitignore 区（hotelapp/services/，见上文配置债登记）：ledger.py 149→41、reservations.py 193→158、audit.py 删除（-38），合计净 −181 行
- 新增：apps/api/test_dup_convergence_guards.py（护栏 5 用例）

## 七、TD 登记册汇总（本批次）

| 编号 | 级别 | 维度 | 处置 |
|---|---|---|---|
| TD-3-01~12 | TD-3 | 死代码 | **已修**（§二） |
| TD-2-01~06 | TD-2 | 重复逻辑 | **已修**（§三）+ 护栏测试 |
| TD-2-R1~R9 | TD-2 | 重复逻辑（语义不同/需口径决策） | 登记不修（§三表） |
| TD-CFG-01 | 配置债 | `hotelapp/services/` 被 .gitignore 排除，服务层不入库 | 登记移交（所有权：根目录文件归仓库清理智能体） |
| TD-TEST-01 | 测试债 | 登录限流测试墙钟窗口跨界 flaky（rate_limit.py 时钟对齐设计） | 登记不修（修复需引入可注入时钟，涉及行为面） |
