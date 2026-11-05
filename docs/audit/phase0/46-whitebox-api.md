# 46 · 白盒 API/视图/中间件层深度审查报告（子智能体 2·重派）

- 范围：`apps/api/` 全部 API 模块 + `apps/identity/staff_center_views.py`、`session_middleware.py`、`middleware.py`、`permission_center.py` 逐端点白盒审查 + 缺陷修复 + 针对性回归测试
- 分支：main（`git pull` 后与 origin 一致，基线 **753 项测试全过**）
- 结论：**确认缺陷 13 项（P2×5、P3×8），全部修复；新增针对性测试 31 项；全量回归 784 项通过**（753 基线 + 31 新增）
- 交付文件：
  - 修复：`apps/api/night_audit.py`、`apps/api/staff_center.py`、`apps/api/guest_center.py`、`apps/api/front_desk.py`、`apps/api/views.py`、`apps/api/billing_center.py`、`apps/api/openapi_schema.py`、`apps/api/test_openapi_schema.py`（契约矩阵同步）
  - 新增测试：`apps/api/test_whitebox_api.py`（31 项）
- 纪律：未改 `apps/*/services*.py`、`approval_registry.py`、模板/registry/migration、`hotelapp/views.py`；未做业务规则变更；未删除既有测试。
- 说明：工作树中另有并行工作流产物（`templates/pms/staff_center.html`、`static/css/staff_center.css`、`docs/audit/phase0/50-ui-staff-center.md`，属 UI 子智能体），本报告不涉及、不回退、不引用其内容；基线与回归两次全量运行均包含该工作树状态，口径一致。

## 1. 方法

1. 逐文件通读 14 个目标模块（apps/api 11 个 API 模块 + identity 4 个模块），按八类清单逐端点核对：认证四态 / 水平越权 / 参数校验 / 错误处理 / 响应契约 / CSRF-方法 / 中间件分类边界 / 日志与 PII 泄露。
2. 服务层异常面交叉核对：通读 `front_desk_services.py`、`lifecycle_services.py`、`services_v2.py::collect_payment` 的 raise 面，确认 API 层 except 分支与被调异常闭合（不吞异常、不漏 TypeError/ValueError）。
3. 每个疑似缺陷先确认根因（含 Django 4.1 行为验证，如 `filter(pk="abc")` 抛 `ValueError`），再修；修复最小化，只动 API 层。
4. 中间件按 §B/§F 规格表核对路径分类边界（`/platform-foo`、`/administrator`、末尾斜杠、query string 均验证不误判）。

## 2. 已修复缺陷清单

| 编号 | 等级 | 位置 | 缺陷 | 根因 | 修复 | 测试 |
|---|---|---|---|---|---|---|
| WB-46-01 | P2 | `apps/api/night_audit.py::_body` | 夜审 run/reopen/break-glass 系列写端点收到非对象 JSON（如 `[]`/`"x"`）时 `body.get` 抛 AttributeError → 500 | `_body` 只做 JSON 解码，未校验 `isinstance(dict)`（其他中心均有校验） | 补 dict 校验 → 400 VALIDATION_ERROR | `NonObjectJsonBodyTests::test_night_audit_run_array_body_returns_400` |
| WB-46-02 | P2 | `apps/api/staff_center.py::_membership_body`（membership_create / level / status 三端点共用） | 同上：`[]`/字符串体 → AttributeError → 500 | 同上 | 补 dict 校验 → 400 | `NonObjectJsonBodyTests`×3（create / status / 契约码断言） |
| WB-46-03 | P2 | `apps/api/guest_center.py::_parse_body`（guest_create / guest_update / 双 reveal / adjust_daily_rate 共用） | 同上：`[]` 体 → AttributeError → 500 | 同上 | 补 dict 校验 → 400 | `NonObjectJsonBodyTests`×3（guests/create、证件揭示、房价调整） |
| WB-46-04 | P2 | `apps/api/front_desk.py::assign_room` | `room_id` 缺失/为 null → `int(None)` 抛 TypeError 未捕获 → 500；同文件 `change_stay_room_api` 已捕获 `(ValueError, TypeError)`，属漏改 | 复制修改时 except 面未对齐 | 捕获 `(ValueError, TypeError)` → 400"room_id 必须是整数。"（消息统一中文） | `ParamValidationTests::test_front_desk_assign_room_missing_room_id_returns_400`、`..._non_integer_room_id_returns_400` |
| WB-46-05 | P2 | `apps/api/staff_center.py::membership_create` | `guest_id="abc"` 等非整数串直接进 `filter(pk=...)`，Django 字段转换抛未捕获 `ValueError` → 500 | body 值未做 int 预转换（billing_center 同类参数均有 `_integer` 守卫） | `int(str(raw))` 转换 + 400"guest_id 必须是整数。" | `ParamValidationTests::test_membership_create_non_integer_guest_id_returns_400` |
| WB-46-06 | P3 | `apps/api/staff_center.py::_staff_dto` | `phone_masked` 对 <8 位电话原样返回（脱敏失效=明文下发），与已修复的 WB-10（服务层 `_mask`）同根因未同步 | 短值分支 `else profile.phone or None` 直落原文 | 非空短值一律全打码 `*`×len；空值保持 None；≥8 位口径不变 | `MaskingTests::test_staff_short_phone_fully_masked`、`test_staff_full_phone_masked` |
| WB-46-07 | P3 | `apps/api/guest_center.py::_guest_dto` | 主电话 `phone_masked` 对 <8 位原样返回；同函数 `phones` 列表分支已用"已隐藏"，同函数双口径不一致 | 同 WB-46-06 | <8 位统一"已隐藏"（与同文件 phones 列表口径一致） | `MaskingTests::test_guest_short_primary_phone_hidden` |
| WB-46-08 | P3 | `apps/api/front_desk.py::_mask_phone` | 7 位数字串 `digits[:3]+"****"+digits[-4:]` 拼出 11 字符恰好覆盖全部 7 位原数字（伪脱敏=可完整还原）；阈值 `<7` 过松 | 3+4 掩码要求位数 ≥8 才不可还原 | 阈值提高到 `<8` → "已隐藏"；11 位口径不变；空值保持"—" | `MaskingTests::test_front_desk_mask_phone_short_digits_not_recoverable` |
| WB-46-09 | P3 | `apps/api/openapi_schema.py` `/audit-timeline`（+ `test_openapi_schema.py::PERMISSION_MATRIX`） | 契约声明权限 `identity.staff_manage`，而视图自 P0-04 起实际门禁为 `core.audit_view`——文档教唆以错误权限码申请访问 | P0-04 改门禁时未同步契约文档与测试矩阵 | schema 与矩阵同步改为 `core.audit_view`（不改运行时行为） | `ContractConsistencyTests::test_openapi_audit_timeline_declares_audit_view_permission` |
| WB-46-10 | P3 | `apps/api/views.py::_integer` | `bool` 被 `int()` 静默接受：`{"rooms": true}` → 1 间、`{"room_type_id": true}` → pk 1，类型混淆穿透业务边界校验 | Python bool 是 int 子类，`int(value)` 无类型判别 | `isinstance(value, bool)` 显式拒绝 → 400"必须是整数" | `ParamValidationTests::test_public_reservation_create_rejects_bool_rooms`、`..._bool_adults` |
| WB-46-11 | P3 | `apps/api/views.py::reservation_detail / payment_intent_create / reservation_cancel` | DEFAULT 物业缺失/停用（`request.property=None`）时 `property_obj.pk` 抛 AttributeError → 500；同文件 `availability`/`reservations_create` 同边界为 503，口径分裂 | 后写的三个端点漏加物业守卫 | 三端点统一补 503"酒店物业不可用。"（payment_intent 的物业守卫先于环境门禁） | `PublicPropertyGuardTests`×4（三端点 + availability 参照） |
| WB-46-12 | P3 | `apps/api/billing_center.py::collect_payment_api` | replay 预检 `provider_ref` 未按物业过滤：跨物业同名参考号会被误判为"本物业重放"（201/200 语义失真；服务层会独立拒绝跨物业占用，故无越权数据泄露，纯防御性修正） | 预检查询漏 `property=property_obj` | 预检补物业过滤，与服务层六要素幂等口径对齐 | `ContractConsistencyTests::test_cross_property_payment_reference_rejected_not_replayed`（跨物业护栏：400 且零支付落库） |
| WB-46-13 | P3 | `apps/api/views.py`（availability / reservations_create / reservation_detail / cancel / payment_intent_create / payment_callback / openapi_document，7 处装饰器序） | `api_guard` 文档承诺"405 方法错误也统一为 JSON"，但 `@require_GET/@require_POST` 在 `api_guard` 外层，405 `HttpResponseNotAllowed` 直接返回（HTML），JSON 统一契约不生效（front_desk 同类端点序正确，属两套口径） | 装饰器应用顺序：require_* 最外层短路返回 | 7 处统一改为 `@api_guard` 外层（csrf_exempt 仍最外层，属性经 functools.wraps 传播不受影响） | `AuthStateTests::test_get_only_endpoints_reject_post`（api_view 面 405 + api_guard 面 JSON 405 双断言） |

等级依据：P0 资金/权限绕过——**未发现可经 HTTP 边界利用的 P0/P1**；P2 = 错误处理/参数校验缺陷（500 语义错误、类型混淆）；P3 = 契约/脱敏/一致性缺陷。

## 3. 白盒清单核对结论（未发现问题的方面）

| 清单项 | 结论 |
|---|---|
| 认证四态 | 各中心统一：匿名 401（AUTHENTICATION_REQUIRED）、无任职/无物业上下文 403 或 404（口径见各模块 docstring，抽查 notification/billing/approval/housekeeping 一致）、无专项权限 403、有权限 200；`platform_superadmin_required` 三不变量 + 非超管 403 不跳登录、半状态超管 403+审计复核在位；`staff_required`+`pms_permission_required` 与旧 /staff_accounts/ 等价。 |
| 水平越权 | 详情/写端点全部强制 `property` 过滤或对象级复核（folio/payment/ledger/shift/reservation/room/staff/membership/guest/notification/approval/audit event/business date/night audit run/break-glass）；跨物业一律 404 不泄露存在性（抽查测试锁定 guest/membership/staff/folio/notification 五面）。请求体 property_id 面无此类入口（写端点均以 URL 对象+当前物业上下文取对象）。 |
| 参数校验 | 分页全走 `api_contract.parse_page_args` 唯一解析器（front_desk `_page_args` 为委托薄适配）；枚举筛选（status/category/severity/housekeeping/maintenance/folio_type/entry_type/review_decision）全部白名单校验 400；日期参数统一 fromisoformat 400；期间上限 92 天；金额 str 化经 `_money_str`/`quantize`。缺陷 WB-46-04/05/10 修复后无已知的非受控类型入口。 |
| 错误处理 | 服务层异常面交叉核对闭合：`FrontDeskOperationError`/`LifecycleOperationError`/`NightAuditError`/`HousekeepingOperationError`/`GuestServiceError`/`GuestServiceError→BUSINESS_CONFLICT`/`ValueError→400`/`DoesNotExist→404` 均正确映射；未预期异常有 `api_guard`/`api_view` 双兜底返回 JSON 500 无堆栈（`logger.exception` 只落服务端日志）。 |
| 响应契约 | `{data, meta}` / `{error:{code,message,field_errors,retryable}, meta}` 封套一致；金额一律 Decimal→str 两位小数；label 缺失一律 `get(..., code)` 兜底不产生 None 崩溃；分页 meta（page/page_size/total/has_next/has_previous）完整且排序全部含主键兜底（ensure_stable_ordering）。 |
| CSRF/方法 | 写端点全部 require_POST（读端点 require_GET）；`payment_callback` 为 csrf_exempt+签名校验+DEBUG 门禁三重防护；permission_center/staff_center_views 走 Django CSRF 中间件（CSRF_USE_SESSIONS）；GET 端点无副作用（notification refresh/read-all 均为 POST）。 |
| 中间件 | `resolve_session_cookie_spec` 边界核对：`/platform-foo`、`/administrator`、`/adminx` 均归 PMS 域（前缀精确匹配含 `/` 结尾判定）；末尾斜杠与 query string 经 path_info 归一不误判；`/permissions*` 走 cleanup_only 兼容跳转不读任何 Session；未知路径默认 PMS（"未知即平台"禁止）。`PropertyContextMiddleware` 拒绝语义核对：无 DEFAULT 任职明确拒绝并写 `security.property_context_denied` 审计，不静默回退；平台路径跳过任职解析无审计噪声（F-10）。 |
| 日志与 PII | 全部端点无 Cookie/Session Key 落日志或响应；揭示类审计只存 masked 值（guest_center reveal 两端点 `after={"masked": ...}`）；audit_center 列表不下发 before/after；错误响应统一中文文案无堆栈；rate limit 指纹为 HMAC 非明文。 |

## 4. 未修复登记项（不改契约/既有行为，移交后续裁决）

| 编号 | 等级 | 位置 | 描述 | 不修原因 |
|---|---|---|---|---|
| API-R-01 | P3 | `apps/api/reservation_center.py::audit_timeline`（P1-14 旧端点） | 列表响应仍下发 `before_json/after_json` 完整载荷，与 audit_center 定版的"列表不下发 before/after（PII 最小化）"口径不一致（同一 core.audit_view 门禁下无提权，但属双口径） | `test_audit_center.py::test_existing_audit_timeline_endpoint_untouched` 明文锁定该端点行为不变（"不改既有端点"是上一阶段显式决议）；收敛需迁移消费方后统一，且涉及 `AuditEventDto` 契约联动 |
| API-R-02 | P3 | `apps/api/dashboard.py::dashboard_warnings` | `tel` 字段 <8 位电话原样返回（WB-46-06/07 同根因） | `dashboard.py` 不在本授权范围清单（11 个 API 模块之外）；消费方为 `hotelapp/views.py`（禁改）；修复建议随 UI/页面专项统一处理 |
| API-R-03 | P3 | `apps/api/views.py` 幂等装饰器（api_contract.idempotent） | 并发同 Idempotency-Key 双请求均可能进入视图（get_or_create 竞态后 status=PROCESSING 不阻断）；依赖服务层幂等（provider_ref/唯一约束）兜底 | 服务层已有 WB-03/11/13/14 撞键幂等兜底（白盒-1 修复）；装饰器层进程间互斥需存储网关，属架构专项 |
| API-R-04 | P3 | `apps/api/staff_center.py::staff_detail`、`guest_center.py::guest_update` 等 | 物业上下文缺失（`property=None`）时返回 403 PERMISSION_DENIED，而同模块列表端点返回 404 RESOURCE_NOT_FOUND，同域双口径 | 已登录无任职返回 403 不泄露存在性、语义可接受；统一为 404 涉及既有测试契约（openapi E_STAFF 错误集），收益低 |
| API-R-05 | P3 | `apps/api/front_desk.py::rooms`（/front-desk/rooms） | 房态列表不分页（全量返回 + `meta.total`），与统一分页契约不完全一致 | 物理房数量级小（单店 ≤ 数百）；前端房态图需全量渲染；分页需前端联动改造 |

## 5. 测试

### 5.1 新增针对性回归（`apps/api/test_whitebox_api.py`，31 项）

- `NonObjectJsonBodyTests`（6）：夜审执行、会员创建/状态、客人创建、证件揭示、房价调整——非对象 JSON 体一律 400。
- `ParamValidationTests`（5）：guest_id 非整数 400、排房 room_id 缺失/非整数 400、公共预订 rooms/adults bool 拒收。
- `MaskingTests`（4）：staff 短电话全打码、正常 11 位口径不变、guest 短主电话"已隐藏"（含 phones 列表不回原值）、`_mask_phone` 边界（7/6 位隐藏、11 位 3+4 掩码、空值"—"）。
- `PublicPropertyGuardTests`（4）：三公共端点无物业上下文 503 + availability 参照护栏。
- `HorizontalAccessTests`（5）：guest/membership/staff/folio/notification 跨物业对象一律 404。
- `AuthStateTests`（5）：匿名 401、无任职 404、无权限 403、有权限 200、方法纪律 405（api_view 面 + api_guard JSON 面）。
- `ContractConsistencyTests`（2）：OpenAPI audit-timeline 权限声明 = 实际门禁；跨物业收款参考号拒绝且零落库。

### 5.2 全量回归

- 基线（修复前）：`Ran 753 tests ... OK`。
- 修复后全量：`Ran 784 tests ... OK`（753 + 31 新增，0 失败 0 错误）。

## 6. 端点覆盖清单

| 模块 | 端点数 | 覆盖结论 |
|---|---|---|
| `apps/api/views.py`（公共面） | 7 | 全量逐行（availability / reservations_create / reservation_detail / cancel / payment_intent_create / payment_callback / openapi_document）；WB-46-10/11/13 修复 |
| `apps/api/front_desk.py` | 21 | 全量逐行（overview/arrivals/departures/in-house/reservations/detail/rooms/assignable/folio/legacy×2/stays×3/no-show/deposit/refund×2/assign/check-in/check-out）；WB-46-04/08 修复 |
| `apps/api/billing_center.py` | 12 | 全量逐行（folios 列表/详情、ledger、payments GET/POST、refunds GET/POST、reversals、shifts 列表/开/关、summary）；WB-46-12 修复 |
| `apps/api/guest_center.py` | 12 | 全量逐行（guests 列表/详情/创建/更新/folios/timeline/双 reveal、rooms/status、room-types、rate-calendar、daily-rates adjust、financial-summary）；WB-46-03/07 修复 |
| `apps/api/staff_center.py` | 8 | 全量逐行（staff 列表/详情、memberships 列表/详情/创建/level/status）；WB-46-02/05/06 修复 |
| `apps/api/night_audit.py` | 12 | 全量逐行（preview/run/business-dates×3/break-glass×3/runs×2/reopen×2）；WB-46-01 修复 |
| `apps/api/housekeeping_center.py` | 4 | 全量逐行（rooms/summary/status/maintenance）——通过 |
| `apps/api/notification_center.py` | 5 | 全量逐行（list/unread-count/read-all/refresh/mark-read）——通过 |
| `apps/api/report_center.py` | 5 | 全量逐行（occupancy/adr/revenue/guests/housekeeping）——通过 |
| `apps/api/audit_center.py` | 4 | 全量逐行（events/detail/summary/actions）——通过 |
| `apps/api/reservation_center.py` | 3 | 全量逐行（reservations-mgmt 列表/详情、audit-timeline）；API-R-01 登记 |
| `apps/api/approval.py` | 4 | 全量逐行（list/request/approve/reject）——通过（P0-01/02 加固复核在位） |
| `apps/api/pages.py` / `dashboard.py` | — | 页面门禁与数据聚合走查（dashboard 见 API-R-02 登记） |
| `apps/identity/staff_center_views.py` | 10+1 | 全量逐行（页面 + 9 个写动作 + 邀请三操作限流）——通过 |
| `apps/identity/session_middleware.py` | — | 规格表/分类边界/双 Cookie 写删/legacy 清理全量核对——通过 |
| `apps/identity/middleware.py` | — | 物业上下文解析/拒绝语义/审计核对——通过 |
| `apps/identity/permission_center.py` | 3 | 全量逐行（read/save/confirm-token）：allowed 面注册表派生、version 内容 hash、高危 confirm_token salt 绑定、事务内审计——通过 |
