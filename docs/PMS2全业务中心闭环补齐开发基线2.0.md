# PMS2 全业务中心闭环补齐开发基线 2.0

> 基线日期：2026-11-05  
> 代码基线：修订期间持续变更；最终冻结 SHA 以 MySQL + E2E 验收提交为准。当前状态：**修订中，不得冻结**。  
> 文档性质：正式开发需求与技术约束基线；供后续排期、开发、代码审查和验收使用。  
> 范围：五大业务中心 + 六个缺失运营中心 + 跨模块 P0/P1 基础治理。  
> 当前部署定位：单物业优先、Django 模块化单体、MySQL 为目标数据库；不拆微服务，不以新增页面代替业务闭环。

---

## 修订记录：2026-11-06（2.0 修订中）

本节优先于本文早期章节中与当前代码不一致的路径和实现描述。未经下列门禁验收，不得宣布 PMS2 完成，也不得启动 PMS3.0 功能排期。

### A. 财务中心正式入口与 API 命名

- 唯一正式页面：`/manage/finance/`。
- 唯一正式财务 API 命名空间：`/api/v1/billing/`。
- 旧 `/finance/` 只允许 302 跳转到 `/manage/finance/`，不得继续渲染旧 FinanceRecord 页面。
- 旧写入口 `/finance_add/` 保持 410 Gone。
- FinanceRecord 仅作为历史数据读取/迁移来源；不得与 Folio、Payment、Refund、LedgerEntry 双写。
- 文档中所有 `/api/v1/finance/*` 示例统一按 `/api/v1/billing/*` 执行；若接口尚未实现，标记为待开发，不得把文档路径当作已存在接口。

### B. 维修权限污染专项纠正

- `inventory.maintenance_manage` 与 `hotelapp.no_show_handle` 是不同业务权限，不允许通过 codename 复制授权关系。
- 历史 `inventory.0005` 只作为已发布迁移保留，不得改写。
- 新增纠正迁移 `inventory.0006_align_maintenance_manage_role_grants`：清除错误继承的组授权，再按当前角色决策只授予「值班经理」「店长」。
- 「客房服务员」只保留 `inventory.housekeeping_view`、`inventory.housekeeping_update`，不授予维修管理。
- 前台、未到处理人员不得因拥有 `no_show_handle` 而获得维修权限。
- 验收必须同时检查 Group 权限集合、AuthorizationService、API 403/200 和页面按钮显隐；不能只检查 Permission 行是否存在。

### C. 退款三方分离（申请 / 审批 / 执行）

退款必须是三个独立事实和三个不同账号：
1. 申请人创建 Refund 申请，不能审批或执行自己的申请。
2. 审批人单独审批或驳回；审批只产生 ApprovalRecord，不得写退款 LedgerEntry，也不得把 Payment 改为已退款。
3. 执行人仅能执行已批准退款；执行动作必须记录 `executed_by`、`executed_at`、执行渠道/参考号和幂等键。
4. 申请人、审批人、执行人三者必须互不相同；拒绝、失败、待重试均不得重复入账。
5. 只有执行成功后，才追加唯一 REFUND LedgerEntry 并更新 Payment 退款状态。
6. 部分退款累计金额不得超过原支付可退余额；并发申请和并发执行必须锁定 Payment/Refund，并由唯一约束兜底。

**DEC-10 未完成前不得启用旧的“审批即执行”行为。** 迁移须保留历史 Refund、ApprovalRecord 和 LedgerEntry，不得回写或删除已发生财务事实。

### D. 班次现金口径

班次现金应有金额不得再等于 opening_cash。目标公式：

`expected_cash = opening_cash + cash_payments - cash_refunds + cash_in - cash_out`

- Payment 必须记录所属收银班次（非现金支付可以关联班次，但不得计入现金）。
- Refund 必须记录实际执行班次；只有现金渠道退款才冲减该班次现金。
- 现金支出/备用金调整必须有独立、可审计、不可静默修改的事实记录（金额、原因、操作人、班次、时间、幂等键）。
- 关班事务锁定班次并冻结交易归属；已关闭班次不得新增、改绑或删除现金事实。
- 对账输出必须同时列出现金收入、现金退款、现金支出、开班备用金、应有现金、实盘现金和差异；差异非零必须填写原因并审计。
- 银行卡、转账、线上支付不得计入现金应有金额。

### E. MySQL 验收口径

- `.github/workflows/ci.yml` 使用 MySQL 8 服务，必须先执行完整迁移，再执行完整 Django 测试。
- SQLite CI 仅作快速回归，不可代替 MySQL。
- 本地 MySQL 的连接失败（例如 Access denied）必须先核对本地数据库用户名、密码、授权 Host 和数据库名；不得把失败配置写入仓库或提交真实凭据。
- MySQL 验收必须额外覆盖 select_for_update 并发、唯一约束冲突、幂等重放、迁移前后授权差异和失败事务回滚。
- 任何 MySQL CI 绿灯仅证明 CI 所用 MySQL 版本/配置的迁移与测试通过，不等于生产实例备份恢复、参数和权限配置已验收。

### F. DEC 门禁

| DEC 状态 | 开发规则 |
|---|---|
| 未决 | 只允许不依赖该决策的独立缺陷修复 |
| 已提交方案 | 可评审，不得按个人理解实施 |
| 已批准 | 严格按批准内容开发 |
| 已实施 | 必须完成迁移、测试和文档 |
| 已验收 | 提交证据后才可关闭 |

涉及退款状态机、现金班次事实、审批权限、财务报表口径的 DEC 必须在对应开发开始前批准。实现人员不得自行补充金额阈值、职责分工或权限范围。

### G. 当前代码与验收状态（持续更新）

- 已提交：维修权限纠正迁移 `inventory.0006`，清除从 `no_show_handle` 继承的维修管理授权，仅按正式角色矩阵授权值班经理/店长。
- 已提交：旧 `/finance/` 302 到 `/manage/finance/`，旧写入口 `/finance_add/` 返回 410。
- 已提交：退款三方分离模型与服务（申请/审批/执行）、现金退款执行班次、现金变动事实及班次应有现金聚合。
- 已提交：`billing.0016_refund_sod_state_constraints`，增加 Refund SoD、审批事实和执行事实数据库 CHECK 约束；无法证明新流程的历史记录标记 `legacy_workflow`，不回写历史账务。
- 已提交：修复 Payment.save 与 Refund.save 保护逻辑错位；增加状态迁移、幂等、三方分离和数据库约束旁路测试。
- 已提交：现金退款可用余额校验、现金 OUT 余额校验、班次事实不可变、本人班次关账与主管代关规则。
- 已提交：红字冲销服务强制物业过滤；维修/清洁 API 与 OpenAPI 权限码校准。
- 已提交：审计/审批/财务事实 ORM QuerySet 批量写入保护；增加 MySQL 只追加触发器 SQL、检查命令和部署说明。
- 已提交：财务退款 DTO 暴露申请/审批/执行人、执行时间、参考号、执行班次及历史流程标记；非现金退款页面要求渠道参考号。
- 待验收：最新 main 对应的 MySQL 8 Django CI 与测试 workflow 必须全部完成并通过。
- 待验收：目标 MySQL 实例执行 `billing.0016`、部署 6 条 append-only triggers，并运行 `python manage.py check_mysql_append_only_guards`。
- 待验收：真实浏览器 Playwright E2E；当前 Django HTTP/模板回归不能代替真实浏览器。
- 待处理：`docs/接口文档/openapi.json` 由当前 `build_openapi()` 重新生成，确保静态快照与运行时路径集合一致。
- **本基线仍为修订中，不得标记冻结。**

---

## 0. 总体结论与执行原则

当前 PMS2 处于“骨架可用、闭环不足”状态。已有核心模型、领域服务和部分 API，但不能据此认定对应运营中心已经完成。尤其财务、夜审、审计等领域已有较厚的后端基础，却缺少可供员工日常使用的列表 API、工作台、筛选、异常处理和操作反馈。

本基线把以下 11 个中心纳入同一治理范围：

1. 员工中心
2. Guest 360 客人中心
3. 房态与库存中心
4. 会员中心
5. 预订与入住中心
6. 财务与账单中心
7. 夜审与营业日中心
8. 清洁与维修工作台
9. 审计与合规中心
10. 报表分析中心
11. 通知与待办中心

开发不得按“每个中心单独造一套数据”实施。统一围绕物业、员工任职、Guest、Reservation、Stay、Room、Folio、LedgerEntry、Payment、BusinessDate、AuditEvent 等事实模型协作。

### 0.1 强制原则

- **一个事实只有一个权威写入口**：页面不得直接写模型；API 只负责认证、授权、参数校验、DTO 和调用领域服务。
- **新旧数据不能双写**：PMS2 新模型是新业务写入权威；旧模型只允许受控读取、映射、核对和迁移。任何旧入口退役前须有替代入口与数据核对报告。
- **状态必须有合法迁移**：禁止页面自行拼接状态、绕过领域服务修改状态字段。
- **物业隔离默认拒绝**：任何列表、详情、导出、统计、待办、异步任务均按当前有效物业任职过滤；缺少物业上下文、任职或授权时拒绝，不允许依赖 DEFAULT 物业上下文推断授权。
- **授权后端强制执行**：导航隐藏只是体验，不是安全控制。每个 API、对象、字段、导出和任务执行点都必须检查权限。
- **金额使用 Decimal**：金额、税额、退款、转账、汇总不得经 float 中转。
- **账本只追加**：LedgerEntry 不更新、不删除；错账通过红字冲销或受控调整凭证表达。
- **幂等 + 事务 + 并发锁**：预订、库存、入住、收款、退款、冲销、夜审、审批均须定义幂等键、事务边界和并发策略。
- **先核对后迁移**：Client/Guest、Staff/StaffProfile、Vip/GuestMembership、旧订单/Reservation 的迁移均须可追溯，不得按手机号单字段自动合并。
- **先补核心闭环，不扩展新产品能力**：积分、优惠券、发票直连、集团多物业、复杂收益管理等未列入本期上线范围，除非另行决策。

### 0.2 优先级定义

| 级别 | 定义 | 处理要求 |
|---|---|---|
| P0 | 安全、账务、权限、数据完整性阻断项 | 必须先修复；不允许带缺陷进入分页/统计冻结或上线验收 |
| P1 | 当前运营中心完成闭环所必需 | 按模块阶段交付，必须有 API、页面、自动化测试和验收记录 |
| P2 | 提升效率、分析能力或增强治理 | 核心业务稳定后安排 |
| 暂缓 | 明确不属于当前单物业上线范围 | 保留边界，不提前实现 |

---

# 1. 全局架构与统一技术约束

## 1.1 当前架构定位

保持现有 Django 模块化单体，不拆分微服务。沿用现有应用边界：

| 领域 | 当前代码边界 | 责任 |
|---|---|---|
| 核心物业/审计/营业日 | `apps/core/` | Property、BusinessDate、AuditEvent、LegacyRecordLink |
| 身份/授权 | `apps/identity/` | StaffProfile、Membership、RoleAssignment、Invitation、AuthorizationService |
| 客人/会员 | `apps/guests/` | Guest、GuestPhone、GuestDocument、GuestMembership |
| 房态/库存 | `apps/inventory/` | Room、RoomType、DailyRate、RoomTypeInventory、清洁/维修模型 |
| 预订/入住 | `apps/reservations/` | Reservation、ReservationRoom、Stay、生命周期服务 |
| 财务账务 | `apps/billing/` | Folio、LedgerEntry、Payment、Refund、ApprovalRecord、FolioTransfer、CashierShift |
| API | `apps/api/` | HTTP 边界、权限检查、序列化 DTO、分页、错误码 |
| 旧业务兼容 | `hotelapp/` | 仅保留明确标记的历史读取/映射/退役入口 |

当前 API 采用项目自有 `api_view/api_response/ApiError` 契约，不应为本次补齐无必要地引入第二套 API 框架。前端沿用项目现有模板、静态 JS/CSS 与 API 调用方式，所有页面复用统一组件和错误处理。

## 1.2 统一 API 契约

所有新接口遵循：

- 成功：`{ "ok": true, "data": ..., "meta": ... }`（以现有 `api_response` 实际封套为准，不得另造格式）。
- 失败：统一错误码、中文可理解消息；不得向客户端暴露堆栈、SQL、密钥或内部路径。
- 列表参数：`page`、`page_size`、可选 `q`、`sort`、明确的筛选字段。
- 页码必须为正整数；page_size 必须在统一边界内；非法参数统一 400，不得一个接口回退 1、另一个接口报错。
- 默认排序必须稳定：业务时间倒序时追加主键倒序，如 `-created_at,-id`。
- 日期使用 ISO 8601；时间使用带时区 ISO 8601；金额使用十进制字符串；枚举返回 code + label。
- 所有写接口要求 CSRF（浏览器 Session 场景）、权限、物业范围、幂等键（适用时）和审计。
- 详情接口必须按 `property=current_property` 过滤对象，不能先按 ID 取对象再补查物业。
- 导出接口必须复用列表筛选、同一授权和字段遮罩规则，不能另开权限旁路。

建议统一错误码：`AUTHENTICATION_REQUIRED`、`PERMISSION_DENIED`、`PROPERTY_CONTEXT_REQUIRED`、`RESOURCE_NOT_FOUND`、`VALIDATION_ERROR`、`BUSINESS_CONFLICT`、`IDEMPOTENCY_CONFLICT`、`RATE_LIMITED`。

## 1.3 统一授权模型

权限码必须进入 PermissionRegistry / RoleRegistry 的正式授权面，不允许出现“页面有按钮但授权面没有该权限”的矩阵外权限。

新增/确认权限码：

| 权限码 | 用途 | 默认建议角色 |
|---|---|---|
| `audit.view` | 审计事件查询 | 店长、值班经理；审计只读角色仅在正式启用后 |
| `audit.export` | 审计导出 | 店长；单独审批或原因码 |
| `billing.folio_view` | 账单查看 | 前台、财务、值班经理、店长 |
| `billing.payment_view` | 收款/支付查询 | 财务、前台、值班经理、店长（字段按角色遮罩） |
| `billing.refund_view` | 退款查询 | 财务、值班经理、店长 |
| `billing.cashier_shift_manage` | 班次开闭与交接 | 财务、前台（按具体动作分权） |
| `billing.reconcile` | 对账 | 财务、店长 |
| `billing.export` | 财务导出 | 财务、店长；导出原因必填并审计 |
| `night_audit.view` | 夜审/营业日查看 | 夜审员、值班经理、店长 |
| `night_audit.execute` | 执行夜审 | 夜审员、值班经理、店长（遵循现有决策） |
| `housekeeping.view` | 房态清洁任务查看 | 客房服务员、前台、值班经理、店长 |
| `housekeeping.update` | 清洁任务状态更新 | 客房服务员、值班经理 |
| `maintenance.manage` | 维修工单管理 | 值班经理、店长 |
| `reports.view` | 经营报表查看 | 店长、财务；按报表分类 |
| `reports.export` | 报表导出 | 店长、财务；按报表分类 |
| `tasks.view` | 本人/岗位待办查看 | 所有存在待办的岗位 |
| `tasks.assign` | 待办指派 | 值班经理、店长 |

以上是权限码需求，不代表当前角色已经自动获得这些权限。必须由权限基线确认角色矩阵，并逐一验证真实执行点。敏感字段继续使用现有 PII reveal 权限和原因码审计，不得由普通列表权限顺带授权。

## 1.4 数据库与基础设施

- 目标数据库以 MySQL 为主；SQLite 只用于快速单元测试，不能替代 MySQL 的锁、约束、索引和并发验收。
- 所有跨物业业务表应有 property 外键或可证明的物业归属链，并建立列表常用复合索引。
- 所有关系删除优先 `PROTECT`，账务、审批、审计、营业日等事实禁止物理删除。
- 金额使用 `DecimalField`；数量用整数；业务日期用 `DateField`；时间统一启用时区。
- 生产/多进程环境禁止依赖 `LocMemCache` 实现登录、邀请、公开接口限流或幂等锁。配置共享 Redis Cache；若项目暂不引入 Redis，必须在生产启动检查中拒绝多 worker 使用本地缓存承载安全限流。
- 定时夜审首期不强制引入 Celery。可由管理命令 + OS Scheduler/cron 执行，任务本身必须可重复、安全幂等、有运行记录和失败告警。未来确需分布式任务队列再单独决策。
- 审计文件日志不是 AuditEvent 数据库事实的替代品。审计记录应进入数据库并具备只读权限、备份、集中收集/归档策略。
- 对 AuditEvent 的不可变性不能只依赖 Django Model 的 save/delete 覆盖；需用数据库权限、触发器或独立只写数据库账号/审计存储方案建立数据库层保护，并在目标 MySQL 验证。
- 所有迁移采用可回滚的 expand/contract：先新增兼容字段/表，数据回填与核对，再切换读写，最后退役旧字段/入口。

---

# 2. P0 全局阻断项（先于任何新中心开发）

以下问题必须建立单独修复提交与回归测试，不应被埋入页面开发任务。

## P0-01 审批列表匿名读取

代码位置：`apps/api/approval.py::approval_list`。

现状：列表接口取 `request.property` 后查询审批记录，没有显式认证和 `AuthorizationService` 检查；物业中间件给匿名请求附加 DEFAULT 物业上下文时，会形成匿名读取审批记录风险。

要求：
1. 必须验证 `request.user.is_authenticated`、`is_active`、有效 PropertyStaffMembership。
2. 增加明确只读权限（建议 `billing.approval_view` 或统一 `approval.view`），不得把“有物业上下文”当授权。
3. QuerySet 必须按当前物业过滤；列表默认只返回当前用户可见范围。
4. 测试匿名、已登录无任职、其他物业员工、无权限员工均返回 401/403/404，不泄露记录数量和存在性。
5. 对列表、详情、审批动作全部做对象级物业校验。

## P0-02 审批动作代码白名单

代码位置：`apps/api/approval.py::approval_request` 与 `ApprovalService.request_approval`。

要求：
1. 建立唯一 `ApprovalActionRegistry`，白名单至少包含已实际实施的退款审批、冲销审批、营业日重开审批、PII 批量导出审批等；未实现动作不得注册。
2. 每个 action_code 绑定 entity_type、申请权限、审批权限、执行服务、SoD 规则、payload schema、有效期。
3. API 与 Service 双层校验；不能只在前端下拉框限制。
4. 不接受任意 entity_type/entity_id；必须验证目标对象存在、属于当前物业、状态允许申请。
5. payload 做字段白名单和长度限制，不存身份证、密码、支付密钥等敏感值。
6. 对未知 action_code 返回 400，不写 ApprovalRecord、不写 AuditEvent。
7. 测试任意 action_code、错 entity_type、跨物业对象、重复请求、申请人自批、过期审批和重复执行。

## P0-03 统一分页契约

代码位置：`apps/core/api_contract.py::paginate`、`apps/api/front_desk.py::_page_args`。

要求：
- 收敛为一个分页解析器；page/page_size 非整数、0、负数、超限全部按同一规则返回 400。
- 统一最大 page_size（建议 100）；不得一个接口静默截断，一个接口报错。
- QuerySet 必须稳定排序，默认追加主键。
- 分页总数、筛选条件、排序条件在 API 文档和前端组件一致。
- 测试边界：缺省、1、最大值、超过最大、负数、字母、空字符串、超大数字。

## P0-04 审批/审计/导出权限纠偏

- `reservation_center.audit_timeline` 从 `identity.staff_manage` 切换为独立 `audit.view`。
- `hotelapp.views.export_module` 移除 `_has_role("店长","财务")` 硬编码，改用 AuthorizationService + 专项导出权限。
- 盘点并关闭或 410 所有旧导出入口，包括 `/export/client/`、`/export/vip/` 等；迁移期只允许明确白名单并记录调用审计。
- 导出字段由服务器端按权限组装；Guest 身份证、完整电话等必须调用既有 reveal policy，不能仅做 CSV 静态遮罩后绕过 reveal reason/audit。
- 导出必须记录导出人、物业、模块、筛选条件摘要、字段集合、记录数、原因码、时间和结果；不得记录完整 PII 到审计 payload。
- 清理 `SPECIAL_PERMISSIONS` 中无真实执行点的 `hotelapp.reverse_checkout`、`hotelapp.room_release_ready`；`hotelapp.export_pii` 迁移到统一授权；`inventory.manage_rates`、`identity.staff_manage` 必须决定进入正式授权面或从矩阵移除。
- 不允许权限矩阵显示“已授权”但执行点不存在，也不允许执行点绕过注册表。

## P0-05 生产缓存与限流

- 将 `CACHES.default` 从 LocMemCache 改为环境配置的共享 Redis Cache（生产强制配置，开发环境可保留 LocMem）。
- 登录失败、员工邀请、公开预订/订单查询、幂等保护、验证码/敏感揭示限流使用共享存储。
- 限流键按 IP + 账号/业务标识做不可逆摘要，避免缓存 key 暴露手机号等个人信息。
- Redis 不可用时对敏感写操作 fail closed 或进入明确受控降级，不能静默退回每进程独立计数。
- 增加多进程/多 worker 限流集成测试。

## P0-06 审计数据库层不可变性

- AuditEvent 业务模型保持只增不改不删。
- MySQL 数据库账号分离：应用写账号仅允许 INSERT/必要 SELECT；审计读取账号只读；迁移/运维账号单独保管。
- 若应用同一账号必须兼顾其他表写入，采用 AuditEvent 专用触发器拒绝 UPDATE/DELETE 或独立审计表写入账号。
- 备份、归档、保留期、恢复、完整性校验形成文档和自动化检查。
- 不能只依赖本地 RotatingFileHandler；文件日志作为运行日志，数据库 AuditEvent 作为业务审计事实，二者职责分开。
- 保留期限由适用法律、合同和酒店政策确认后配置；本基线不虚构固定法定年限。

## P0-07 排序与列表一致性

- 员工列表 `-created_at` 追加 `-id`。
- 审计时间线不覆盖模型稳定排序，明确 `-created_at,-id`。
- 全仓库列表接口扫描无稳定排序、N+1、跨物业过滤遗漏和分页不一致问题。
- 统计口径必须注明物业、营业日/自然日、状态范围、币种和更新时间。

---

# 3. 五大中心开发要求

# 3.1 员工中心

### 现有基础

主要模型：`StaffProfile`、`PropertyStaffMembership`、`StaffRoleAssignment`、`Invitation`；现有 API：`GET /api/v1/staff`、`GET /api/v1/staff/{profile_id}`，另有会员相关接口。员工列表 DTO 已包含员工档案、任职、岗位、账号状态和邀请摘要。

### 目标职责

管理员工自然人身份、PMS 登录账号、物业任职关系、岗位分配、员工生命周期和邀请激活；不负责定义全局权限矩阵（权限管理中心负责）。

### 字段要求

沿用现有字段，补齐前先核对模型，不重复创建同义字段：

- StaffProfile：`id`、`user_id`、`full_name`、`phone`（未验证仅联系方式）、`employment_status`、`created_at`、`updated_at`。
- PropertyStaffMembership：`id`、`staff_profile_id`、`property_id`、`employee_no`、`position_code`、`status`、`joined_at`、`left_at`（如当前模型没有则评估补充）、`created_at`。
- StaffRoleAssignment：`id`、`membership_id`、`group_id/role_code`、`starts_at`、`ends_at`、`assigned_by_id`（如缺失则补齐）、`reason`（如缺失则补齐）、`created_at`。
- Invitation：`id`、`staff_profile_id`、`membership_id`、`token_hash`（不得存明文 token）、`status`、`expires_at`、`used_at`、`revoked_at`、`resend_count`、`created_by_id`。

字段新增必须以实际模型差异为准；禁止同一个状态在 StaffProfile 与 Membership 两处互相覆盖。员工整体离职与单物业任职结束是不同事实。

### 接口要求

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/staff` | 分页、搜索、任职状态、角色筛选 |
| GET `/api/v1/staff/{id}` | 员工身份 + 物业任职 + 角色历史 + 邀请状态 |
| POST `/api/v1/staff` | 建档/绑定账号，事务化 |
| PATCH `/api/v1/staff/{id}` | 更新非敏感档案字段 |
| POST `/api/v1/staff/{id}/memberships` | 新增物业任职 |
| PATCH `/api/v1/memberships/{id}` | 调整任职状态/岗位资料 |
| POST `/api/v1/memberships/{id}/roles` | 分配/替换角色，记录起止时间与原因 |
| POST `/api/v1/staff/{id}/invitations` | 创建邀请，返回一次性 URL 仅一次 |
| POST `/api/v1/invitations/{id}/resend` | 撤销旧 token 并生成新 token |
| POST `/api/v1/invitations/{id}/revoke` | 撤销邀请 |
| POST `/api/v1/staff/{id}/suspend` | 停用员工或指定任职，明确范围 |
| POST `/api/v1/staff/{id}/terminate` | 离职，关闭有效任职与角色授权 |

写接口统一调用 EmployeeLifecycleService；禁止视图直接更新 User、StaffProfile、Membership 和 Group。

### 页面要求

员工列表、员工详情/编辑、任职与岗位历史、邀请状态、员工状态操作。页面明确区分“账号状态”“员工身份状态”“当前物业任职状态”“岗位授权状态”。不允许用一个启用开关代替四种状态。

### 闭环验收

建档 → 账号绑定 → 创建物业任职 → 分配九类岗位之一 → 邀请激活 → 首次登录改密 → 调岗/停职/离职 → 会话失效 → 权限即时撤销 → 全程 AuditEvent。九个测试角色账号必须通过真实授权链，不允许仅创建 User 或使用 User.groups 直接授权。

---

# 3.2 Guest 360 客人中心

### 现有基础

主要模型：`Guest`、`GuestPhone`、`GuestDocument`；已有 `guest_center.py` 列表、详情、创建、更新、敏感字段揭示接口。手机号不是自然人唯一身份键，可能共享。

### 目标职责

以 Guest 作为住客主档，聚合该客人的预订、入住、账单、支付摘要、会员和操作时间线。Guest 360 是查询与客人资料治理入口，不得成为账务、预订、入住的第二写入口。

### 字段要求

- Guest：`id`、`property_id`、`full_name`、`gender`（可选）、`nationality`（可选）、`preferred_language`（可选）、`source`、`created_at`、`updated_at`。
- GuestPhone：`id`、`guest_id`、`phone`（存储策略按当前实现）、`is_primary`、`verified_at`、`shared_review_required`、`created_at`。
- GuestDocument：`id`、`guest_id`、`document_type`、`encrypted_value/ciphertext`、`masked_value`、`last_four`（如适用）、`verified_at`、`created_at`；不得在普通 DTO 或审计日志返回明文。
- 关联汇总不重复落库：Reservation、Stay、Folio、GuestMembership、AuditEvent 通过外键查询或专用只读查询服务聚合。

### 接口要求

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/guests` | 分页搜索客人，默认脱敏 |
| GET `/api/v1/guests/{id}` | 客人基本资料与关联摘要 |
| GET `/api/v1/guests/{id}/timeline` | 预订/入住/账务/会员/审计时间线 |
| GET `/api/v1/guests/{id}/reservations` | 预订历史 |
| GET `/api/v1/guests/{id}/stays` | 入住历史 |
| GET `/api/v1/guests/{id}/folios` | 账单摘要，敏感字段按角色 |
| POST/PATCH `/api/v1/guests`、`/api/v1/guests/{id}` | 通过 GuestService 创建/修改 |
| POST `/api/v1/guests/{id}/documents/{document_id}/reveal` | 证件揭示，原因码+专项权限+审计 |
| POST `/api/v1/guests/{id}/phones/{phone_id}/reveal` | 电话揭示，原因码+专项权限+审计 |

### 数据统一与去重

- Reservation.guest 必须指向 Guest。
- 旧 Client 通过 LegacyRecordLink 建立 source_system/source_model/source_pk → target Guest 映射。
- 匹配候选使用姓名、多个联系方式、证件摘要等组合；手机号相同只提高候选置信度，不自动合并。
- 合并须人工确认，保留主档、被合并档案、字段冲突、关联迁移清单、操作者、原因和回滚证据。
- 禁止 Guest 与 Client 并行长期写入；旧入口迁移后冻结写入或返回明确退役提示。

### 闭环验收

从预订创建 Guest → 入住核验/补充档案 → Guest 360 展示预订/Stay/Folio/会员 → 敏感字段按权限揭示 → 历史 Client 映射可追溯 → 重复档案合并有审计且关联不丢失。

---

# 3.3 房态与库存中心

### 现有基础

主要模型：`RoomType`、`Room`、`RatePlan`、`DailyRate`、`RoomTypeInventory`。Room 已区分 occupancy_status、housekeeping_status、maintenance_status；DailyRate 有 price/closed/min_stay/max_stay；库存模型有 total_rooms/held 等字段和版本控制设计。

### 目标职责

管理物理房间状态、房型、房价、房型每日可售库存。必须区分物理房态、销售库存和预订分配，不允许一个“空房”状态代表全部。

### 字段要求

- Room：现有 `property_id`、`room_type_id`、`number`、`floor`、`occupancy_status`、`housekeeping_status`、`maintenance_status`、`is_active`；可评估补充 `building`、`wing`、`notes`。
- RoomType：现有 `code`、`name`、`capacity`、`bed_type`、`amenities`、`is_active`。
- DailyRate：现有 `rate_plan_id`、`room_type_id`、`stay_date`、`price`、`currency`、`closed`、`min_stay`、`max_stay`。
- RoomTypeInventory：保留现有 `total_rooms`、`held`、`sold`（以实际模型为准）、`version`；补齐库存计算说明与约束，不重复存储可售量作为可独立修改事实。
- 清洁/维修任务见第 3.8 节。

### 接口要求

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/rooms/status` | 房态总览，按楼层/房型/三种状态筛选 |
| GET `/api/v1/rooms/{id}` | 房间详情、当前 Stay、下一预订、清洁/维修任务 |
| GET `/api/v1/room-types` | 房型列表 |
| POST/PATCH `/api/v1/room-types`、`/api/v1/room-types/{id}` | 房型维护 |
| GET `/api/v1/inventory/calendar` | 房型日历库存 |
| GET `/api/v1/rate-calendar` | 价格日历 |
| POST `/api/v1/daily-rates/batch-adjust` | 批量调价，权限+原因+审计 |
| POST `/api/v1/inventory/close` | 关房/停售 |
| POST `/api/v1/inventory/reopen` | 恢复可售 |
| POST `/api/v1/rooms/{id}/out-of-order` | 维修停用 |
| POST `/api/v1/rooms/{id}/release` | 维修完成并申请放行；净房检查独立 |

### 业务规则

- 当前可售房物理条件：启用、空闲、净房、正常维修状态。
- 未来日期可售库存：以 RoomTypeInventory 和有效 Reservation/Hold 计算，不等同于当前空闲房间数。
- 排房必须校验入住日期区间每一晚库存和物理房间冲突，使用事务、锁或乐观版本号。
- 取消、No-Show、改期、换房、续住必须正确释放/调整 Hold、Sold 和房间占用。
- 房价变更记录变更前后值、操作者、原因、生效日期；不能静默覆盖历史价格事实。
- 维修停用期间不可分配；维修完成不自动变成净房。

### 页面要求

房态看板（楼层/房型/状态）、房间详情抽屉、库存日历、房价日历、关房/维修状态操作；每种状态显示独立标签和可执行动作，不把房态图标作为唯一状态说明。

### 闭环验收

预订占库存 → 排房 → 入住改变 occupancy → 退房转 DIRTY → 创建清洁任务 → 清洁/检查完成转 CLEAN → 恢复可售；维修停用与库存停售同步影响可售计算，取消/改期释放库存且不产生超卖。

---

# 3.4 会员中心

### 现有基础

主要模型：`GuestMembership`、`MembershipLevelHistory`；现有 API：`GET /api/v1/memberships`、详情、创建和等级变更。旧 `hotelapp.Vip` 通过 `legacy_vip_id` 关联线索。

### 目标职责

会员是 Guest 的业务关系，不是另一份住客主档。当前阶段只实现会员资料、状态、等级历史和旧数据映射，不擅自增加积分、优惠券、自动升级等未决策功能。

### 字段要求

- GuestMembership：`id`、`property_id`、`guest_id`、`member_no`、`level_code`、`status`、`joined_at`、`expires_at`、`legacy_vip_id`、`created_at`、`updated_at`。
- MembershipLevelHistory：`membership_id`、`from_level`、`to_level`、`reason`、`changed_by_id`（如缺失补齐）、`created_at`。
- 会员编号在物业范围内唯一；会员状态、等级变化必须经 GuestMembershipService。

### 接口要求

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/memberships` | 列表、搜索、等级、状态筛选 |
| GET `/api/v1/memberships/{id}` | 会员详情与等级历史 |
| POST `/api/v1/memberships` | 为已存在 Guest 创建会员 |
| PATCH `/api/v1/memberships/{id}` | 修改允许修改的资料 |
| POST `/api/v1/memberships/{id}/level` | 等级变更，原因必填 |
| POST `/api/v1/memberships/{id}/suspend` | 暂停会员 |
| POST `/api/v1/memberships/{id}/restore` | 恢复会员 |

### 闭环验收

Guest 详情建立会员关系 → 会员中心查询 → 级别变更产生历史 → 暂停/恢复影响会员有效状态 → 旧 Vip 记录通过 LegacyRecordLink 核对，不按手机号自动合并，不丢失历史等级来源。

---

# 3.5 预订与入住中心

### 现有基础

主要模型：`Reservation`、`ReservationRoom`、`Stay`、`NoShowRecord`；现有前台 API 覆盖预订、排房、入住、收款、退房、续住、换房、No-Show、押金、退款申请/审批等部分动作。

### 目标职责

预订主单是预订事实；ReservationRoom 是房型/房间分配明细；Stay 是真实入住事实；Folio 是账单户头；所有状态变化经生命周期服务。

### 字段要求

Reservation 主单至少明确：`property_id`、`booking_no`、`guest_id`、`source`、`status`、`check_in`、`check_out`、`room_count`、`adult_count`、`child_count`、`total_amount`、`currency`、`created_at`、`updated_at`、`idempotency_key`（如现有未实现需补）。
ReservationRoom 至少明确：`reservation_id`、`room_type_id`、`assigned_room_id`、`check_in`、`check_out`、`amount`、`status`。
Stay 至少明确：`property_id`、`reservation_room_id`、`guest_id`、`room_id`、`status`、`actual_check_in`、`actual_check_out`、`created_at`。
以上字段须先对照现有模型，不重复增加已有字段；金额快照保留订单成交事实。

### 接口要求

保留并完善现有 `/api/v1/front-desk/*`，逐步统一旧预约管理接口，不再为相同动作保留多个写入口。

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/front-desk/overview` | 今日抵店/离店/在住/待排房摘要 |
| GET `/api/v1/front-desk/reservations` | 预订列表 |
| GET `/api/v1/front-desk/reservations/{booking_no}` | 预订详情 |
| POST `/api/v1/reservations` | 公共/员工预订创建入口，按渠道区分授权 |
| POST `/api/v1/front-desk/reservation-rooms/{id}/assign` | 排房 |
| POST `/api/v1/front-desk/reservation-rooms/{id}/check-in` | 办理入住 |
| POST `/api/v1/front-desk/stays/{id}/extend` | 续住 |
| POST `/api/v1/front-desk/stays/{id}/change-room` | 换房 |
| POST `/api/v1/front-desk/reservation-rooms/{id}/no-show` | 未到处理 |
| POST `/api/v1/front-desk/stays/{id}/check-out` | 退房 |
| POST `/api/v1/front-desk/reservations/{booking_no}/cancel` | 取消预订 |
| POST `/api/v1/front-desk/payments/{id}/refunds` | 退款申请 |
| POST `/api/v1/front-desk/refunds/{id}/approve` | 退款审批/执行按 SoD 拆分，不得合并为同一人操作 |

### 核心流程和一致性

- 预订创建：Guest 校验/建立 → 库存锁定 → Reservation + ReservationRoom → Folio MASTER → Hold/账务必要记录 → 审计。
- 排房：校验房型、入住区间、房间三状态、其他 Reservation/Stay 冲突；在一个事务中分配。
- 入住：核验客人和证件（按 PII 权限揭示）→ 排房确认 → Stay 创建/激活 → Room OCCUPIED → 押金/账单关联 → 审计。
- 续住：先校验新增日期库存、房价和房间冲突，再更新入住日期/库存；失败时不得只改 Stay 日期。
- 换房：锁定原 Stay/Room 和目标 Room → 验证新房可用 → 原房释放并变脏 → 新房占用 → Stay/ReservationRoom 关联更新 → 账务差额/房费按规则入账 → 审计。
- No-Show：确认宽限/政策 → 状态变更 → 释放库存或转为保留（由规则决定）→ 费用/押金处理 → NoShowRecord → 审计。
- 退房：检查未结账阻挡 → 结清/授权挂账 → Stay 完结 → Room 转 DIRTY → 创建清洁任务 → Folio 关闭规则 → 审计。
- 取消：按取消政策校验 → 释放 Hold → 必要时退款审批 → 取消状态 → 审计。
- 快速退房、反结账等高风险动作必须依据已批准的角色/审批规则，不得用过时的无执行点权限码。

---

# 4. 六个必须补入治理范围的运营中心

# 4.1 财务与账单中心

### 现有基础

模型与服务较厚：`Folio`、不可变 `LedgerEntry`、`Payment`、`Refund`、`FolioTransfer`、`CashierShift`、`ApprovalRecord`、`BillingService`。目前缺少完整运营界面与列表 API；`financial-summary` 是固定 7 天摘要，不能当作财务工作台。

### 目标页面

1. 财务总览：今日营业日收入、收款、退款、未结余额、待审批。
2. 账单列表/详情：按预订、客人、账单号、状态、日期、币种筛选。
3. 流水列表：LedgerEntry 只读列表，支持凭证详情和冲销关联。
4. 支付列表：支付方式、状态、金额、交易参考号、来源。
5. 退款工作台：待申请、待审批、已批准待执行、已完成、失败/驳回。
6. 收银班次：开班、收款汇总、退款汇总、现金盘点、交班、关班。
7. 对账工作台：支付渠道/现金班次/账本差异。
8. 财务导出：按权限、原因、字段白名单导出。
9. 发票信息：本期只保留开票资料/状态记录能力；真实税控/电子发票服务商接入暂缓，除非另行确定地区和供应商。

### 字段要求

- Folio：沿用 `property_id`、`reservation_id`、`folio_type`、`guest_id`、`status`、`currency`、`opened_at`、`closed_at`、`master_reservation_key`。
- LedgerEntry：沿用 `property_id`、`folio_id`、`stay_id`、`entry_type`、`amount`、`currency`、`business_date`、`reference`、`description`、`posted_by_id`、`void_of_id`、`void_reason`、`created_at`。列表不允许编辑/删除。
- Payment：至少展示/核对 `folio_id`、`amount`、`currency`、`method`、`status`、`provider_reference`、`received_by_id`、`business_date`、`created_at`（以现有字段为准）。
- Refund：至少明确 `payment_id`、`amount`、`reason`、`status`、`requested_by_id`、`approved_by_id`、`executed_by_id`、`provider_reference`、`created_at`、`executed_at`；缺失字段按实际退款服务和审批事实补充。
- CashierShift：明确 `property_id`、`cashier_id`、`opened_at`、`closed_at`、`opening_cash`、`expected_cash`、`counted_cash`、`variance`、`status`、`business_date`。
- ReconciliationRun（建议新增）：`property_id`、`business_date`、`source_type`、`source_reference`、`expected_amount`、`actual_amount`、`difference_amount`、`status`、`started_at`、`finished_at`、`created_by_id`。
- InvoiceRecord（如确认纳入本期轻量开票资料登记）：`property_id`、`folio_id`、`invoice_no`、`buyer_name`、`buyer_tax_no`（加密/严格权限）、`amount`、`currency`、`status`、`issued_at`、`external_reference`。不接税控系统时不得虚构“已开票”。

### API 要求

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/billing/summary?from=&to=&business_date=` | 可配置期间汇总，明确营业日口径 |
| GET `/api/v1/billing/folios`、`/folios/{id}` | 账单列表/详情 |
| GET `/api/v1/billing/ledger-entries`、`/ledger-entries/{id}` | 不可变流水列表/详情 |
| GET `/api/v1/billing/payments`、`/payments/{id}` | 支付列表/详情 |
| GET `/api/v1/billing/refunds`、`/refunds/{id}` | 退款列表/详情 |
| POST `/api/v1/billing/refunds/{id}/execute` | 审批通过后执行退款，独立权限和幂等 |
| GET `/api/v1/billing/cashier-shifts` | 班次列表 |
| POST `/api/v1/billing/cashier-shifts/open` | 开班 |
| POST `/api/v1/billing/cashier-shifts/{id}/close` | 关班/交班 |
| GET `/api/v1/billing/reconciliation` | 对账差异列表 |
| POST `/api/v1/billing/reconciliation/run` | 发起对账 |
| GET `/api/v1/billing/export` | 审计导出，不直接流出任意字段 |
| GET/POST `/api/v1/billing/invoices` | 开票资料登记（仅在本期确认后实施） |

### 账务规则

- Folio 余额 = 所属账单 LedgerEntry 带符号金额汇总；不得把 Payment 表汇总和账本余额混为同一口径。
- 收款必须先校验 Folio 未关闭、金额/币种、支付方式、幂等 reference；支付成功事实与账本凭证在一致事务/可靠事件机制中关联。
- 退款：申请人、审批人、执行人分离；金额不得超过可退余额；同一支付不得超额退款；退款执行失败可重试但不得重复入账。
- 冲销：只新增等额反向 LedgerEntry，关联原凭证；原凭证不可变；原凭证只允许冲销一次。
- FolioTransfer：两账单同物业、同币种、金额正数；成对记账，事务原子完成；原始凭证不搬迁。
- CashierShift 关班：汇总该班次现金/非现金交易、退款、撤销；差异必须填写说明并审计。
- 对账：外部支付渠道原始交易、Payment、LedgerEntry 三方核对；缺少渠道对账文件时不得伪称已完成外部对账。

### 闭环验收

入住生成 Folio → 房费/消费入账 → 收款 → 退款申请 → 双人审批 → 执行退款 → 账本反向凭证/支付状态一致 → 班次交接 → 营业日汇总 → 对账差异可追踪 → 导出全程审计。

---

# 4.2 夜审与营业日中心

### 现有基础

已有 `BusinessDate`、`NightAuditRun`、`NightAuditService`；API 已覆盖 preview/run/reopen/break-glass 的部分动作，但没有完整历史列表、营业日列表、异常清单、未结账阻挡清单和运营工作台。

### 目标页面

- 当前营业日状态与营业日历；
- 夜审预览（收入/支付/退款/在住/抵店/离店/未结账）；
- 阻挡项清单和可跳转业务对象；
- 历史夜审运行记录、执行耗时、成功/失败原因；
- 营业日重开申请/审批/执行/复核；
- 夜审任务运行状态和失败告警。

### 字段要求

BusinessDate：`property_id`、`business_date`、`status`、`opened_at`、`closed_at`、`opened_by_id`、`closed_by_id`、`reopened_by_id`、`reopened_at`、`version`（以当前模型为准）。
NightAuditRun：`business_date_id`、`status`、`started_at`、`finished_at`、`summary`、`error_code`、`error_message`（脱敏）、`executed_by_id`。
NightAuditBlocker（建议新增，或由规则服务动态生成）：`run_id/business_date_id`、`blocker_code`、`severity`、`entity_type`、`entity_id`、`message`、`resolved_at`、`resolved_by_id`。

### API 要求

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/night-audit/business-dates` | 营业日列表 |
| GET `/api/v1/night-audit/business-dates/current` | 当前营业日 |
| GET `/api/v1/night-audit/business-dates/{id}` | 营业日详情 |
| GET `/api/v1/night-audit/preview?date=` | 夜审预览 |
| GET `/api/v1/night-audit/blockers?date=` | 阻挡/警告清单 |
| GET `/api/v1/night-audit/runs` | 夜审历史列表 |
| GET `/api/v1/night-audit/runs/{id}` | 单次运行详情 |
| POST `/api/v1/night-audit/run` | 手动执行 |
| POST `/api/v1/night-audit/schedule/trigger` | 受保护的调度触发，仅内部任务凭证 |
| POST `/api/v1/night-audit/business-dates/{id}/reopen-request` | 重开申请 |
| POST `/api/v1/night-audit/business-dates/{id}/reopen-approve` | 审批执行 |
| POST `/api/v1/night-audit/break-glass` | 紧急重开流程，保留现有二次验证和事后复核 |

### 阻挡规则

至少识别：未完成入住/退房状态转换、待处理退款/审批（按业务策略）、未结账账单、未入账房费、库存异常、重复或失败夜审、未完成班次关账、营业日不匹配交易。每类阻挡必须有明确 code、严重程度、业务对象和解决指引。警告与硬阻挡必须区分。

### 自动调度

- 每个物业的营业日切换时间可配置；不得把服务器自然日直接当营业日。
- 调度命令支持重复执行、并发锁、失败重试和超时记录。
- 定时执行与人工执行调用同一个 NightAuditService。
- 首期可用 Windows Task Scheduler（开发）/cron（部署）触发管理命令；不新增任务队列依赖。
- 任务执行必须校验物业、当前营业日、幂等键和权限/系统身份；不能通过公开匿名 URL 触发。

### 闭环验收

营业日开始 → 运营交易写入营业日 → 夜审预览 → 阻挡逐项处理 → 执行夜审 → 营业日关闭 → 历史可查询 → 符合条件时双人申请重开 → 复核 → 后续交易与账务归属可追踪。

---

# 4.3 清洁与维修工作台

### 现有基础

`apps/inventory/models.py` 已有清洁任务、房间检查、维修工单模型迁移记录；但需要核对是否有统一服务、状态 API、任务列表和前端工作台。不能仅凭模型存在认定功能已实现。

### 页面

- 清洁任务池：待清洁、清洁中、待检查、返工、已完成；
- 我的任务：按员工/班组分配；
- 房间检查：检查项、结果、不合格原因、复检；
- 维修工单：报修、派工、处理中、待验收、已完成、取消；
- 超时/优先级看板。

### 字段建议

HousekeepingTask：`property_id`、`room_id`、`task_type`、`status`、`priority`、`assigned_to_id`、`created_by_id`、`started_at`、`completed_at`、`due_at`、`notes`、`created_at`。
RoomInspection：`task_id`、`room_id`、`inspector_id`、`result`、`checklist_json`、`failed_items`、`inspected_at`、`reason`。
MaintenanceTicket：`property_id`、`room_id`、`category`、`description`、`severity`、`status`、`reported_by_id`、`assigned_to_id`、`started_at`、`resolved_at`、`verified_by_id`、`out_of_order_from`、`out_of_order_to`、`created_at`。
上述仅作为字段核对表，先复用现有字段，不可重复建表。

### API

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/housekeeping/tasks` | 任务池/我的任务 |
| GET `/api/v1/housekeeping/tasks/{id}` | 任务详情 |
| POST `/api/v1/housekeeping/tasks/{id}/claim` | 接单 |
| POST `/api/v1/housekeeping/tasks/{id}/start` | 开始清洁 |
| POST `/api/v1/housekeeping/tasks/{id}/complete` | 完成清洁 |
| POST `/api/v1/housekeeping/tasks/{id}/inspect` | 检查/复检 |
| GET `/api/v1/maintenance/tickets` | 维修工单列表 |
| POST `/api/v1/maintenance/tickets` | 报修 |
| POST `/api/v1/maintenance/tickets/{id}/assign` | 派工 |
| POST `/api/v1/maintenance/tickets/{id}/resolve` | 维修完成 |
| POST `/api/v1/maintenance/tickets/{id}/verify` | 验收并释放房间 |

### 状态与房态联动

退房 → Room.housekeeping_status=DIRTY → 创建待清洁任务；接单 → CLEANING；完成 → INSPECTING；检查通过 → CLEAN；检查失败 → DIRTY/返工任务。维修报修达到停用条件 → maintenance_status=OUT_OF_ORDER 并影响可售库存；维修完成后需验收，不能直接设 CLEAN；若房间仍脏必须继续清洁流程。任务状态和 Room 状态必须由同一领域服务事务更新。

### 闭环验收

退房自动派生清洁任务 → 客房员工接单/完成 → 检查员检查 → 房间变净房 → 房态中心立即显示可用；报修 → 维修停用 → 排房阻断 → 维修完成 → 验收 → 房态恢复但清洁状态独立判断。

---

# 4.4 审计与合规中心

### 现有基础

已有 `AuditEvent` 与审计中间件/业务服务记录；目前主要依靠 Django Admin 查询，审计时间线 API 使用了语义错误的 `identity.staff_manage` 权限。

### 页面

- 审计检索：时间、操作人、动作、对象类型/ID、结果、来源 IP/请求 ID；
- 详情：before/after 差异、原因、关联业务单；
- 高危事件视图：权限变更、PII 揭示、导出、退款、冲销、营业日重开、break-glass；
- 审计导出与归档状态；
- 保留期与完整性检查状态（仅审计管理员/店长）。

### 字段建议

AuditEvent 当前字段先按模型实际复用；需确保可查询字段至少包含 `property_id`、`actor_id`、`action`、`entity_type`、`entity_id`、`before_json`、`after_json`、`reason`、`request_id`、`ip_address`、`user_agent`（如合规允许）、`created_at`。新增字段须考虑隐私最小化。
审计日志不得写入密码、Session ID、访问令牌、完整证件号码、银行卡数据和密钥。

### API

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/audit/events` | 分页查询 |
| GET `/api/v1/audit/events/{id}` | 事件详情 |
| GET `/api/v1/audit/events/{id}/related` | 关联业务事件 |
| GET `/api/v1/audit/export` | 受控导出 |
| GET `/api/v1/audit/retention/status` | 保留/归档状态 |
| POST `/api/v1/audit/integrity/check` | 受控完整性检查（如采用哈希链/校验摘要） |

### 权限与留存

- 新增独立 `audit.view`、`audit.export`，不借用员工管理权限。
- 审计读取本身也必须被记录，但要防止“读审计日志产生无限审计递归”；可以记录为独立安全访问事件。
- 保留期限必须由法务/业务负责人依据适用法规和合同确认；提供配置和归档机制，不擅自设定法定年限。
- AuditEvent 不可更新/删除需在数据库层落实；定期备份、恢复演练、归档校验。

### 闭环验收

关键业务动作自动生成 AuditEvent → 店长/审计角色按权限查询 → 可关联到 Reservation/Folio/Staff/Room → 导出有原因和审计 → 记录只读且可恢复 → 归档保留策略可验证。

---

# 4.5 报表分析中心

### 目标职责

提供运营决策所需的可解释汇总，不作为第二账本，不自行修改业务数据。所有指标必须定义分子、分母、日期口径、状态范围、币种、时区和更新时间。

### 首期报表范围

P1：
- 每日经营摘要：营业收入、实收、退款、净收款、未结余额；
- 入住经营：入住间夜、在住间数、抵店、离店、No-Show；
- 房态经营：可售房、已售房、停用房、净房/脏房；
- 库存销售：房型每日 total/held/sold/available；
- 会员概览：有效会员数、新增会员、等级分布；
- 财务对账差异；
- 员工操作/高危审批统计（仅授权角色）。

P2：
- RevPAR、ADR、OCC 等收益指标；只有在分母、税费、免费房、内部房、取消和币种规则由业务负责人签字后启用。
- 渠道表现、提前预订天数、取消率、客源结构。

### 数据字段与计算口径

- `business_date` 优先用于营业日报；自然日期仅用于明确标注的趋势报表。
- 收入以已过账 LedgerEntry 为准，按 entry_type 和冲销关系计算；支付金额不能直接当收入。
- 实收以成功 Payment 减成功 Refund 为准；不可将未完成支付意图计入。
- OCC = 已售间夜 / 可售间夜；停用房是否从分母剔除必须明确。
- ADR = 房费收入 / 已售间夜；RevPAR = 房费收入 / 可售间夜。仅在口径确认后展示。
- 多币种不直接相加；单物业默认币种可以展示本币汇总，其他币种分组。

### API

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/reports/daily-operations` | 营业日报 |
| GET `/api/v1/reports/occupancy` | 入住率/间夜 |
| GET `/api/v1/reports/room-status` | 房态统计 |
| GET `/api/v1/reports/revenue` | 收入/实收/退款 |
| GET `/api/v1/reports/inventory` | 库存销售 |
| GET `/api/v1/reports/membership` | 会员概览 |
| GET `/api/v1/reports/export` | 报表导出，复用授权与审计 |

### 技术要求

先使用数据库聚合和只读 QueryService；当实时聚合影响性能时再建立按营业日汇总快照表。快照必须可重算，不能成为账务权威事实。复杂报表查询需索引、Explain、最大日期范围和超时控制。

---

# 4.6 通知与待办中心

### 目标职责

为员工提供统一“需要我处理”的工作入口。通知是提示，待办是可执行业务任务；不能把通知记录当作业务状态权威。

### 首期待办来源

- 退款待审批/待执行；
- 营业日重开待审批；
- 夜审阻挡项；
- 维修超时/待验收；
- 待清洁/待检查；
- 今日抵店但未排房；
- 预订资料待补充；
- 收银班次待交接/未关班；
- 高危 break-glass 事后复核；
- 邀请待激活/即将过期（员工管理员范围）。

### 数据模型

建议新增 `WorkItem` 与 `Notification`，但要避免重复持久化已存在业务状态：

WorkItem：
- `id`、`property_id`、`source_type`、`source_id`、`task_code`；
- `title`、`description`、`priority`、`status`；
- `assigned_to_id`、`assigned_role_code`；
- `due_at`、`completed_at`、`completed_by_id`；
- `dedupe_key`、`created_at`、`updated_at`。
Notification：
- `id`、`property_id`、`recipient_id`、`work_item_id`（可空）；
- `channel`、`title`、`body`、`read_at`、`sent_at`、`delivery_status`、`created_at`。
WorkItem 必须有唯一 dedupe_key，源业务状态变化时同步更新/关闭，不允许重复生成相同待办。

### API

| 方法/路径 | 用途 |
|---|---|
| GET `/api/v1/tasks` | 我的待办/岗位待办 |
| GET `/api/v1/tasks/{id}` | 待办详情 |
| POST `/api/v1/tasks/{id}/claim` | 领取 |
| POST `/api/v1/tasks/{id}/assign` | 指派 |
| POST `/api/v1/tasks/{id}/complete` | 完成（调用源领域服务校验） |
| GET `/api/v1/notifications` | 通知列表 |
| POST `/api/v1/notifications/{id}/read` | 标记已读 |
| POST `/api/v1/notifications/read-all` | 全部已读 |

### 规则

- WorkItem 完成不能只把待办 status 改 DONE；必须验证源业务对象已经完成合法状态迁移。
- 待审批任务不能由申请人自己完成审批。
- 通知发送失败不回滚已经提交的业务事实；采用事务提交后可靠派发/可重试机制。
- 首期站内通知即可；短信、邮件、WhatsApp/第三方消息渠道暂缓。
- 角色待办必须按当前有效任职和角色动态授权，调岗/离职后旧待办重新分配或转交。

### 闭环验收

业务事件产生待办 → 员工工作台可见 → 领取/处理 → 源业务服务执行 → 业务事实完成 → WorkItem 关闭 → 通知已读状态更新；重复事件不重复生成，源业务失败不误关闭待办。

---

# 5. 跨中心统一数据流与端到端闭环

## 5.1 主数据关系

`Property` 是物业隔离根。员工授权链：

`User → StaffProfile → PropertyStaffMembership → StaffRoleAssignment → AuthorizationService`

住客业务链：

`Guest → GuestMembership`  
`Guest → Reservation → ReservationRoom → Stay → Folio → LedgerEntry/Payment/Refund`

客房运营链：

`RoomType → Room → HousekeepingTask/RoomInspection/MaintenanceTicket`  
`RoomType → DailyRate + RoomTypeInventory`

营业财务链：

`BusinessDate → LedgerEntry/Payment/Refund/CashierShift → NightAuditRun`

所有业务写入产生 AuditEvent；需要人工处理的业务事件派生 WorkItem/Notification。

## 5.2 关键业务闭环

| 场景 | 起点 | 中间动作 | 最终一致性要求 |
|---|---|---|---|
| 预订至入住 | Guest/Reservation | Hold、排房、入住 | 库存、房间、Stay、Folio、审计一致 |
| 退房至净房 | Stay checkout | 房间脏房、清洁、检查 | Room 状态与任务状态一致 |
| 维修 | 报修 | 停用、派工、维修、验收 | 库存不可售，验收后再判断净房 |
| 收款退款 | Folio/Payment | 申请、审批、执行、冲销 | Payment、Refund、LedgerEntry 和余额一致 |
| 夜审 | BusinessDate | 预览、阻挡、执行 | 营业日状态、账务归属和运行记录一致 |
| 员工离职 | StaffProfile/Membership | 结束任职、撤销授权 | Session 失效、待办转移、审计留存 |
| PII 揭示 | GuestDocument/Phone | 权限、原因码、揭示 | 字段最小化、操作留痕、导出不旁路 |
| 会员 | Guest | 建立会员、等级变更 | Guest 主档唯一，会员历史可追踪 |

## 5.3 事务与异步边界

- 同一数据库内可原子完成的状态变化必须使用 `transaction.atomic` 和必要的 `select_for_update`。
- 不在数据库事务内调用外部支付、邮件或慢网络；先写本地待处理状态/幂等记录，外部调用后回写结果。
- 外部支付回调必须验证签名、金额、币种、订单关联和幂等 reference。
- 通知、报表快照、外部渠道对账可在事务提交后异步执行；必须有失败重试、幂等和可观测记录。
- 每条关键状态迁移记录 actor、reason、from_status、to_status、entity、property、request_id 和时间。

---

# 6. 前后端交付标准

每个中心页面必须具备：

1. 列表：分页、稳定排序、搜索、筛选、空态、加载态、错误态。
2. 详情：显示主数据、状态、关联对象、历史和允许的操作。
3. 操作：按钮与权限/状态联动；后端仍独立校验。
4. 表单：字段级校验、服务端错误映射、重复提交防护。
5. 反馈：成功/失败提示、操作结果和下一步建议。
6. 数据刷新：写操作成功后刷新受影响的列表、详情、统计卡片和关联中心。
7. 审计：高危操作明确展示原因输入、审批状态和操作者，不暴露不该看的敏感字段。
8. 可访问性：标签、键盘焦点、表格语义、颜色以外的状态表达。

页面不得在浏览器计算财务余额、库存可售量、权限或营业日状态；这些必须由后端领域服务返回。

---

# 7. 数据迁移与旧入口退役

迁移对象：
- `hotelapp.Client → apps.guests.Guest`
- `hotelapp.Staff → apps.identity.StaffProfile/PropertyStaffMembership`
- `hotelapp.Vip → apps.guests.GuestMembership`
- `hotelapp.Room → apps.inventory.Room`
- `hotelapp.Accommodation/旧订单 → Reservation/ReservationRoom/Stay`
- `hotelapp.Folio/FinanceRecord → apps.billing.Folio/LedgerEntry/Payment`（按业务含义逐条映射，不能简单按表名映射）

每一类迁移必须输出：
- 源记录总数、目标映射数、未映射数、冲突数；
- 一对一/一对多/多对一映射规则；
- 字段转换规则和金额核对；
- 关联完整性检查；
- 可重跑和幂等证明；
- 失败回滚方案；
- LegacyRecordLink 映射记录；
- 旧入口关闭日期与访问日志。

禁止直接删除旧表或清空历史数据。旧入口退役顺序：替代功能上线 → 数据核对 → 写入口冻结 → 观察期只读 → 返回明确退役状态 → 经过备份与审批后再评估删除代码/表。

---

# 8. 测试与验收矩阵

每个模块交付至少包含：

| 测试类型 | 最低要求 |
|---|---|
| Model/约束测试 | 外键、唯一、CheckConstraint、不可变性 |
| Service 单元测试 | 合法/非法状态迁移、事务回滚、金额/日期边界 |
| API 权限测试 | 匿名、无任职、无权限、跨物业、对象越权 |
| API 契约测试 | 字段、错误码、分页、排序、过滤、幂等 |
| 并发测试 | 重复预订、超卖、重复收款/退款、并发夜审 |
| 前端集成测试 | 页面加载、筛选、表单、错误反馈、按钮权限 |
| 数据迁移测试 | 重跑、冲突、孤儿记录、账务总额核对 |
| MySQL 集成测试 | 真实目标数据库的锁、索引、事务、约束 |
| 回归测试 | 既有登录、九角色、预订入住、账务、夜审、导出 |

P0 权限测试必须覆盖匿名与跨物业。每一个权限拒绝都应验证数据库没有产生业务副作用。

---

# 9. 分阶段开发排期建议

| 阶段 | 内容 | 交付门槛 |
|---|---|---|
| Phase 0 | P0-01 至 P0-07 安全/权限/分页/缓存/审计修复 | 自动化测试通过；权限矩阵与执行点一致 |
| Phase 1 | 预订入住 + 财务账单 + 夜审营业日闭环 | 从预订到退房、收款退款、夜审完整回归 |
| Phase 2 | 员工中心 + Guest 360 + 房态库存 + 清洁维修 | 五大中心核心数据链打通；旧数据映射可核对 |
| Phase 3 | 会员中心 + 审计合规 + 通知待办 | 会员关联 Guest；高危操作可追踪；待办源闭环 |
| Phase 4 | 报表分析 + 旧入口退役 + MySQL/Apifox/E2E 验收 | 指标口径签字；迁移报告；上线验收证据齐备 |

该排期是依赖关系建议，不代表所有功能都可并行。Phase 1 的账务和夜审依赖 P0 授权、分页、幂等与审计先完成。

---

# 10. 明确不纳入当前开发的事项

除非用户另行批准，本基线不要求：
- 多物业切换和集团级数据汇总；
- 收益/房价经理完整工作台和动态收益优化；
- 积分、优惠券、积分商城、复杂会员自动升降级；
- 外部电子发票/税控平台直连；
- 短信、邮件、WhatsApp 等外部通知渠道；
- 微服务拆分、Kubernetes、复杂事件总线；
- 新的公开住客长期账号体系；
- 重新启用旧 `/booking/` 写入入口或员工自助注册。

---

# 11. 本基线完成定义（Definition of Done）

任何模块只有同时满足以下条件才可标记“已完成”：

1. 模型字段与数据库迁移已提交，约束/索引符合 MySQL。
2. 领域服务实现完整状态机、事务、并发和幂等。
3. API 列表/详情/写操作齐全，统一错误、分页、排序、权限和物业隔离。
4. 页面真实调用 API，不使用静态假数据，不存在按钮空操作。
5. 关联中心的数据刷新和状态联动正确。
6. 审计覆盖所有关键动作，敏感字段遵循 reveal policy。
7. 自动化测试覆盖正常、异常、越权、跨物业、并发和回归。
8. MySQL 验收通过；不能只用 SQLite 结果代替。
9. OpenAPI/Apifox 契约同步，测试集合有执行结果。
10. 中文开发文档、接口文档、更新日志和验收报告同步。
11. 旧入口已明确保留、只读、冻结或退役状态。
12. 无未解释的数据差异、重复写入口或绕过领域服务的路径。

**重要：本文件是待开发与验收基线，不代表上述缺口已经开发完成。每项功能必须先做代码核验，再进入排期；已有实现只补差异，不重复开发。**

---

## 参考资料

- OWASP Authorization Cheat Sheet：最小权限、默认拒绝、每次请求校验授权、授权测试。https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- OWASP Logging Cheat Sheet：安全事件记录、敏感数据排除、日志访问控制、完整性和保留策略。https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
- Django Cache Framework 文档：生产缓存后端与共享缓存配置。https://docs.djangoproject.com/en/stable/topics/cache/
- 项目现有模型/服务/API：`apps/core/`、`apps/identity/`、`apps/guests/`、`apps/inventory/`、`apps/reservations/`、`apps/billing/`、`apps/api/`。


---

# 12. 2026-11-06 财务、权限、数据库与业务闭环整改补充

本节是对基线 2.0 的实施状态补充。若本节与前文“待开发/待验收”描述存在状态差异，以本节及《PMS2财务权限数据库与业务闭环整改实施记录.md》的最新代码证据为准；本节不代表最终上线验收已经完成。

## 12.1 已落实的代码整改

1. **退款三方分离**：申请、审批、执行由不同账号完成；非待审批退款必须关联 ApprovalRecord；只有执行成功才产生 REFUND 账本凭证；历史无法证明三方分离的记录只读标记。
2. **现金班次正确口径**：开班备用金 + 本班现金收入 - 本班现金退款 + 现金补入 - 现金支出；现金支付、现金退款和现金变动分别归属对应班次；现金支出和现金退款不得透支班次。
3. **权限专项迁移**：`inventory.0006_align_maintenance_manage_role_grants` 撤销 `no_show_handle` 对维修权限的错误继承，维修管理只授予值班经理、店长。
4. **个人通知已读**：`NotificationRead` 以员工档案 × 通知保存个人回执，岗位广播已读互不影响。
5. **财务入口统一**：`/finance/` 302 到 `/manage/finance/`；`/finance_add/` 返回 410；财务 API 统一使用 `/api/v1/billing/`。
6. **CSRF 与财务 DOM**：财务工作台使用 Session CSRF token；表单字段在 DOM 替换前读取；增加真实 Django Client CSRF 拒绝/通过测试。
7. **数据库约束**：billing 0016–0019 覆盖退款 SoD、状态事实、现金方向、班次非负金额及审批记录关联；billing/core 财务事实模型阻断 ORM 批量更新/删除旁路。
8. **MySQL 只追加保护**：提供六条 MySQL 8 触发器脚本和安装/检查管理命令；MySQL CI 在测试套件结束后安装并核验触发器定义。

## 12.2 正式权限矩阵

| 权限 | 正式角色 |
|---|---|
| `billing.refund_request` | 前台、财务 |
| `billing.refund_approve` | 值班经理、店长 |
| `billing.refund_execute` | 财务 |
| `billing.cashier_shift_manage` | 前台、财务、值班经理、店长 |
| `billing.reverse_entry` | 财务 | 红字冲销申请与已批准冲销执行 |
| `billing.reversal_approve` | 值班经理、店长 | 红字冲销审批；审批人与执行人分离 |
| `billing.cash_movement_manage` | 财务 |
| `inventory.maintenance_manage` | 值班经理、店长 |

权限迁移必须保留历史授权审计，不得将旧权限码改名后视为完成；必须验证真实角色组授权集合、页面按钮、API 与领域服务一致。

## 12.3 数据库验收要求补充

- MySQL 8 CI：全量迁移 → 完整测试 → 安装六条 append-only 触发器 → 校验触发器表/事件/时机/阻断语句。
- SQLite 仅用于快速回归，不替代 MySQL 的锁、约束、索引、并发和触发器验收。
- 迁移遇到历史负数班次现金或无法证明三方分离的退款时，必须明确报错或标记历史只读，不得静默改金额、审批人、执行人或账本凭证。
- 本地 MySQL 连接错误 `Access denied for user 'wu'lei'@'localhost'` 仍需在目标开发环境修复账号/凭据；CI 使用临时 root 账号成功不能代替本地目标数据库验收。
- 生产 MySQL 需由 DBA 执行触发器部署，并确认应用运行账号对审计、账本、班次现金只追加表无 UPDATE/DELETE 权限。

## 12.4 冻结状态

**基线 2.0 仍未冻结。** 只有在最新 main 的 SQLite/Django CI 与 MySQL 8 CI 全绿、目标 MySQL 迁移及触发器验收通过、真实浏览器财务和九角色 E2E 通过、备份恢复演练完成后，才可冻结 PMS2 并启动 PMS3.0 排期。

详细实现、迁移、测试名称、MySQL 触发器部署和剩余验收项见：

- 《PMS2财务权限数据库与业务闭环整改实施记录.md》
- 《PMS2 MySQL财务与审计不可变事实部署说明.md》


## 12.5 2026-11-06 红字冲销审批闭环补充

此前 `billing.reversal` 仍列在 `ApprovalActionRegistry.PENDING_ACTIONS`，账务服务允许财务仅凭原因直接冲销，与既定“财务执行 + 管理者审批”职责分离不一致。本次按既有决策补齐，不新增业务页面：

1. `billing.reverse_entry`：仅财务可发起冲销申请并执行已批准冲销。
2. `billing.reversal_approve`：仅值班经理、店长持有审批权；管理岗位不因此获得财务执行权。
3. `ApprovalActionRegistry` 注册 `billing.reversal`，目标为 `LedgerEntry`；审批申请、审批列表按 action/entity 过滤，财务可刷新本人申请状态。
4. `POST /api/v1/billing/reversals` 必须携带已批准的 `approval_id`；执行服务再次校验申请人权限、审批人权限、执行人权限及审批人与执行人不相同。
5. 新 `LedgerEntry` 通过 `approval_record` 外键关联审批事实；`legacy_workflow` 显式标记迁移前的旧流程红字凭证。
6. `ck_ledger_reversal_approval_link` 数据库约束禁止新红字凭证缺少审批关联；历史记录只标记旧流程，不伪造审批记录。
7. 财务工作台改为“提交审批 → 管理者审批 → 财务刷新状态 → 执行冲销”两阶段操作。

本项代码已提交到 main；最终验收仍须等待最新 Django CI、MySQL 8 测试与触发器检查全部通过，并完成人工浏览器角色验收。


---

## 12. 2026-11-06 第七批：财务状态事实、MySQL 锁顺序与 CI 兼容性修正

> 本节记录本轮直接提交到 `main` 的代码修复。提交后 CI 仍须以最新 SHA 重新完成；任何旧 SHA 的成功/失败均不得外推到新 SHA。

### 12.1 本轮提交记录

| 提交 | 修改范围 | 目的 |
|---|---|---|
| `c3ea524` | `apps/api/test_billing_center.py` | 测试工号改用数据库生成的短 ID，修复 MySQL 8 下 `employee_no` 超过 32 字符导致的测试失败；不放宽正式字段长度 |
| `71b1fb7` | `apps/billing/models.py` | 冻结已执行/已驳回退款的所有审批与执行字段，包含空参考号、空执行班次等空值事实 |
| `dbfaf9b` | `apps/billing/services_v2.py` | 初步调整现金退款与关班锁定顺序 |
| `2ad7a28` | `apps/billing/models.py` | 增加退款处理时间和执行参考号状态约束 |
| `59ade32` | `apps/billing/migrations/0021_refund_execution_fact_freeze.py` | 新增 MySQL/SQLite 可执行的退款事实约束迁移；历史异常记录仅标记 `legacy_workflow` |
| `458199e` | `apps/api/test_billing_center.py` | 增加现金退款执行成功后空参考号不可回填的回归测试 |
| `1e25fe7` | `apps/billing/services_v2.py` | 统一退款/关班锁顺序为现金班次 → Payment → Refund；同支付退款金额聚合采用 MySQL 当前读 |

### 12.2 退款三分离的补充保护

- 申请、审批、执行仍为三个不同账号；只有执行成功才追加 REFUND LedgerEntry。
- 已执行或已驳回退款进入终态后，申请、审批、执行字段全部冻结；现金退款的空渠道参考号也属于不可变事实，不能事后补写。
- 新增数据库约束：
  - `PENDING/APPROVED` 不得预写 `processed_at`；
  - `REJECTED/SUCCEEDED` 必须有 `processed_at`；
  - 非 `SUCCEEDED` 退款不得预写执行参考号。
- 迁移发现无法证明上述事实的历史记录时，只设置 `legacy_workflow=True`，不回填执行人、不伪造时间、不修改金额/凭证。
- 现金退款执行与关班共享班次锁；退款操作锁顺序统一，避免 MySQL InnoDB 死锁。退款金额上限聚合使用 `SELECT ... FOR UPDATE` 当前读，避免 REPEATABLE READ 旧快照漏算并发退款。

### 12.3 MySQL CI 失败根因与修复状态

- `596f359` 的 MySQL 8 测试 workflow 已成功执行完整迁移，但 848 项测试中有 1 项失败：测试账号工号拼接用户名超过 `PropertyStaffMembership.employee_no.max_length=32`。
- 正式工号字段长度和数据库 schema 不应为测试夹具错误而放宽；已改用创建后的 User 主键生成测试工号。
- `2ad7a28` 的 Django CI 曾因模型约束已修改、迁移尚未提交而在 migration drift check 失败；后续新增 `billing.0021` 迁移予以同步。
- 最新代码提交 `1e25fe7` 对应的 `tests` 与 `Django CI` 必须全部完成成功后，才可标记代码 CI 通过。运行中或旧 SHA 成功都不算本轮通过。

### 12.4 仍需独立验收，不得混为“已完成”

| 验收项 | 当前状态 | 关闭条件 |
|---|---|---|
| MySQL 8 CI | 最新 SHA 运行中 | 最新 SHA 的 migrations、全量测试、append-only guard 检查均成功 |
| 本地 MySQL | 待处理 | 修复本地账号/Host 授权后，真实目标库执行迁移与只追加触发器检查 |
| 浏览器 E2E | 待处理 | Playwright/真实浏览器完成财务收款、退款三方、冲销、班次开闭、权限 403/200 |
| 九角色授权 | 代码迁移已有；实际账号回归待处理 | 九角色登录验证页面可见性、API 权限、服务端拒绝和对象物业隔离 |
| 财务报表指标 | DEC-16 未验收 | 负责人签署收入、退款、冲账、营业日和自然日统计口径后再冻结 |
| 生产备份恢复 | 待处理 | 在独立 MySQL 环境完成备份、恢复和数据核对演练 |

**当前结论：** 财务三分离、现金班次、维修专项权限和通知个人已读已具备对应代码/迁移；本轮继续修复了退款事实不可变和 MySQL 锁顺序问题。仍不得宣称 PMS2 财务/数据库全部验收完成，也不得冻结基线 2.0 或启动 PMS3.0 功能排期。


### 12.5 第八批补充：退款审批到期、通用审批分流与支付状态机

本次继续白盒复核发现：退款申请创建时会自动生成 ApprovalRecord，但通用 `/api/v1/approvals/<id>/approve|reject` 也能单独推进 ApprovalRecord，而不更新 Refund/Payment。这样会产生“审批记录已通过/驳回，退款仍待审批”的悬空状态。另一个闭环缺口是 ApprovalRecord 到期后，`approve_refund` 原先抛错回滚，退款一直保持 PENDING 并持续占用原支付可退余额。

已提交修复：

1. **退款审批唯一写路径**
   - 通用 ApprovalRecord 列表仍可展示退款审批。
   - 通用审批通过/驳回接口遇到 `action_code=billing.refund` 时返回 409，必须走 `/api/v1/billing/refunds/{id}/approve|reject` 专用接口。
   - 专用服务同事务更新 ApprovalRecord、Refund、Payment 和 AuditEvent，避免只更新审批记录。

2. **退款审批到期闭环**
   - Refund 新增 `EXPIRED` 终态；审批到期时同步记录 `processed_at`，不生成 REFUND LedgerEntry。
   - 若同一 Payment 没有其他待审批/已批准退款，按已执行退款事实恢复 Payment 状态（SUCCEEDED / PARTIALLY_REFUNDED / REFUNDED），释放到期申请占用的可退额度。
   - 新迁移 `billing.0022_refund_approval_expiry` 识别历史上“ApprovalRecord 已 EXPIRED、Refund 仍 PENDING”的记录；只有存在真实 `ApprovalRecord.approved_at` 时才同步终态。缺失时间戳的记录标记为 legacy，不伪造处理时间。
   - 增加审批过期回归测试，验证退款过期、支付状态释放且不产生退款凭证。

3. **支付事实和状态机**
   - `Payment.provider` 加入不可变原始交易字段，禁止支付渠道标识被 ORM 覆盖。
   - 移除 `REFUND_PENDING → FAILED` 非法状态迁移；已有待审批/已批准退款时，不允许支付回退为 FAILED、SUCCEEDED、PARTIALLY_REFUNDED 或 REFUNDED。
   - 增加支付渠道不可变和退款处理中不得失败的回归测试。

4. **班次现金事实**
   - `CashierShiftMovement.save()` 增加 ORM 层保护：班次必须存在且仍为 OPEN，登记人必须是该班次收银员。
   - 已增加直接 ORM 写入关闭班次的拒绝测试；服务层仍以班次行锁防止并发关班/现金变动。

5. **MySQL 一致性**
   - 退款/关班统一锁顺序：现金班次（现金退款）→ Payment → Refund。
   - 同一 Payment 下退款额度、驳回后 Payment 状态汇总使用锁定当前读，避免 MySQL REPEATABLE READ 旧快照漏算并发退款。

**本批最新代码 SHA：** `52a182e8b6f61693bdeaa34e8e31bd3637b0536c`。该 SHA 的 Django CI 与 MySQL tests 当时仍在运行；最终结论必须以更新后最新 SHA 的完整工作流为准。


### 12.6 第九批补充：退款审批超时自动闭环与 MySQL 班次插入保护

1. 新增 `BillingService.expire_due_refunds()` 和管理命令 `python manage.py expire_pending_refunds --limit 100`：
   - 每笔退款独立事务，锁顺序 Payment → Refund → ApprovalRecord；
   - 到期后同步 ApprovalRecord.EXPIRED、Refund.EXPIRED、Payment 状态；
   - 没有其他待处理退款时，按已成功退款累计金额恢复 Payment 的 SUCCEEDED / PARTIALLY_REFUNDED / REFUNDED；
   - 只写系统审计事件，不写 REFUND 账本凭证；
   - 重复执行幂等，单次 limit 1–500，可选 `--property-id`。
2. 生产部署建议通过操作系统任务计划程序每 5 分钟运行一次；本命令不会自行安装定时器。详细命令和权限边界见《PMS2 MySQL财务与审计不可变事实部署说明.md》。
3. `CashierShiftMovement.save()` 已拒绝向 CLOSED 班次或非班次本人写入现金变动；MySQL 部署命令增加第 7 条 BEFORE INSERT 触发器，直接 SQL 也必须验证班次 OPEN 且登记人等于班次收银员。
4. MySQL guard 从原 6 条扩展为 7 条；安装与检查命令、SQL 脚本和部署说明同步更新。

**代码提交：** `1bc9aa6`（到期批处理服务）、`8950d8e`（管理命令）、`645e9aa`（批处理回归测试）、`4b7a27a` / `a8301e5` / `f4f3da7`（MySQL 班次插入保护）。

**验收边界：** 命令代码与测试提交不等于服务器已配置定时器；上线前需在实际部署主机安装计划任务，并检查任务运行日志、到期退款状态、Payment 余额释放和 AuditEvent。MySQL 触发器必须在实际目标库安装并通过检查命令。


### 12.7 第九批验收结果：MySQL 8 与 Django CI 已有完整成功样本

截至 2026-11-06，本轮代码 SHA `9b982b0355afa7fc9154f02ce9dda75bf5aa19b6` 的自动化结果：

| 工作流 | 结果 | 证据 |
|---|---|---|
| Django CI | <badge label="通过" color="green"/> | <Link url="https://github.com/wulei0214/django-hotel2/actions/runs/42231517693" title="Django CI · 9b982b0"/> |
| MySQL 8 tests | <badge label="通过" color="green"/> | <Link url="https://github.com/wulei0214/django-hotel2/actions/runs/42231517610" title="MySQL tests · 9b982b0"/> |
| MySQL migrations | <badge label="通过" color="green"/> | billing.0021、billing.0022 及其余迁移全部成功 |
| 全量测试 | <badge label="通过" color="green"/> | 852 tests，148.570s，OK |
| MySQL 触发器 | <badge label="通过" color="green"/> | 安装 7 条触发器并通过 `check_mysql_append_only_guards` |

之后仅对 MySQL 触发器检查器增加了定义内容断言（必须包含 `FOR UPDATE`、`NEW.shift_id`、`NEW.created_by_id` 和 OPEN 状态），当前 `main` SHA `c7f12247b527c3c1f41446f7c51ddad3defbc24d` 的两条工作流正在重新执行，需等待该 SHA 最终结论。

**范围说明：** 以上是 GitHub MySQL 8 CI 环境，不等于用户本地或生产 MySQL 已部署。此前用户本地 MySQL 账号授权错误仍需在本地修复；生产数据库还要由 DBA 安装 7 条触发器，主机还需配置 `expire_pending_refunds` 定时任务。
