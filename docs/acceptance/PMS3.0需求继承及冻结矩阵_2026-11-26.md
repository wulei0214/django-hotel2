# PMS3.0 需求继承及冻结矩阵（DEL-11，2026-11-26）

> 文档编号：PMS2-PMS3-INHERIT-001 ｜ 类型：PLANNING ｜ 状态：ACTIVE ｜ 版本域：PMS3.0 ｜ 业务模块：版本继承
> 任务依据：《PMS 2.0 剩余事项裁定执行、G14 最终验收与 PMS 3.0 启动准备任务书》PMS2-FINAL-DECISION-EXEC-002 §六（DEL-11）
> 上游权威文档：`docs/PMS3.0_后续规划/01_需求拆分与开发批次/PMS3需求继承与新增开发矩阵_2026-11-22.md`（PMS3-REQ-001，89 条 8 态矩阵）
> 基线：`main` `0ca1ce4`
> **纪律**：本矩阵只做「PMS2.0 现状 → PMS3.0 处置」的映射，不新增业务规则、不改写已批准裁定、不把未批准事项写成已批。

---

## 一、必须继承的 14 项（任务书 §六）

| # | 必须继承项 | PMS2.0 落点（可复核） | PMS3.0 处置 | 冻结级别 |
|---|---|---|---|---|
| 1 | **`ARCHITECTURE.md`** | 仓库根 `ARCHITECTURE.md`（架构宪法，修改等级 L1/L2/L3 分级） | 原样继承；任何 L3 变更须负责人批准 | L3 闸门 |
| 2 | **Django 模块化单体架构** | `apps/`（core/identity/inventory/reservations/billing/guests/api）+ `hotelapp/`（遗留）+ `hotel/`（配置与路由） | 原样继承；模块边界与依赖方向由 `scripts/arch_gate.py`（L1/L2）纳入 CI | L2 门禁 |
| 3 | **Django Template + 原生 JS** | `templates/`（37 个）+ `static/js/`（`permission_center.js`、`frontdesk.js`、`pms-ux.js` 等）；无前端构建、无框架 | 原样继承；**不引入 SPA/Node 构建链** | 硬约束 |
| 4 | **Django API / JsonResponse** | `apps/api/`（统一 `api_contract` 封套：`{data, meta}` / `{error:{code,message}}`） | 原样继承；不得自造第二套封套 | 硬约束 |
| 5 | **OpenAPI 3.1** | `apps/api/openapi_schema.py` + 两份逐字节相等快照（`docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json`、`docs/接口文档/openapi.json`） | 原样继承；**新增路由须四处同步**（契约 / TAGS / 计数 / 两份快照），缺一即红 | 硬约束 |
| 6 | **Session 认证** | Django Session + `CSRF_USE_SESSIONS=True`；平台域 `platform_sessionid` / Django Admin `django_admin_sessionid` 双 Cookie 隔离 | 原样继承；平台身份与 PMS 员工身份**分域**不可合并 | 硬约束 |
| 7 | **MySQL 作为生产事实库** | `DJANGO_DB_ENGINE=mysql`；本机 MySQL 8.0.33；SQLite 仅作 CI/轻量口径 | 原样继承；MySQL 为权威（SQLite 全绿不替代 MySQL） | 硬约束 |
| 8 | **九岗位 RBAC** | `apps/identity/registry.py`（22 个专项权限码）+ `AuthorizationService` + `StaffRoleAssignment` + 九岗位矩阵 | 原样继承；**权限码逐项审批，不批量扩展**（F-08） | 硬约束 |
| 9 | **PII 加密与 HMAC 索引** | `apps/guests/services.py`（姓名/证件 Fernet 密文 + **带密钥 HMAC** 盲索引）、`GuestDocumentIndex` 物业级唯一仲裁 | 原样继承；**禁止引入第二套 PII 加密体系** | 硬约束 |
| 10 | **正式 ApprovalService 审批链** | `apps/identity/approval_registry.py` + `ApprovalService`（申请/审批/执行三方分离 + SoD） | 原样继承；不得用普通日志或直写替代 | 硬约束 |
| 11 | **审计及账务不可篡改约束** | `AuditEvent`（append-only，`entity_type` 只用新模型）+ `ImmutableFinancialFactQuerySet` + MySQL append-only 触发器（CI 以 root 安装） | 原样继承；**审计只追加，不得为补哈希重写历史**（F-10） | 硬约束 |
| 12 | **Outbox + Django Worker 方向** | `apps/core/outbox.py` + `outbox_worker.py` + 事件风险登记表 + 入队/消费双端 HIGH_RISK 硬拒绝 | 原样继承；不得新建第二套事件总线 | 硬约束 |
| 13 | **RPO 15 分钟 / RTO 4 小时目标** | `docs/PMS2.0_当前版本/07_部署运维与数据治理/` 运维文档；PROD-03 / DEP-PROD-C | 原样继承（**目标**）；**达成证据待生产演练**（PROD-03 未验收） | 目标继承，验收待生产 |
| 14 | **生产安全配置不合格时拒绝启动** | `apps/core/production_settings.py` / `_validate_production_settings`（DEBUG 关闭、密钥独立、HTTPS、Cookie 安全、HSTS 等） | 原样继承；生产启动前的配置门禁不得放宽 | 硬约束 |

## 二、必须冻结的 7 项（任务书 §六）

| # | 冻结内容 | 现状与依据 | 违反后果 | 冻结边界 |
|---|---|---|---|---|
| 1 | **姓名揭示权限继续关闭** | OWNER-06 裁定**不批准**姓名揭示权限；`apps/identity/registry.py` 无 `guest_name_reveal`；既有专权 `guest_document_reveal`/`guest_phone_reveal` 保留且需专门授权 + 审计 | 新增/恢复同名或等价绕过入口 → 违反 OWNER-06 | 冻结（不得新增） |
| 2 | **OWNER-05 未确认前不得执行生产数据清理** | OWNER-05「暂缓最终批准」；`RetentionPolicy.retention_days` 数据驱动 + `RetentionBasis` 三枚举 + LegalHold 在位；生产真实清理/批量匿名化/到期销毁冻结 | 以「默认值伪装已批准的法律期限」或直接清理生产数据 → 违反 OWNER-05 与 M5-4-P3（L4 审批） | 冻结（`WAITING_OWNER`） |
| 3 | **多物业切换不得擅自提前启用** | F-11「当前单物业运营优先，不启用未经验收的切换能力」；跨物业调岗为 `P3-EMP-04 CARRY_OVER`（需负责人批准） | 提前启用多物业切换 → 属架构变更（L3） | 冻结（L3 闸门） |
| 4 | **已裁定的押金、退款、提前离店等业务规则不得擅自修改** | DEC-01 提前离店按实际住宿晚数 × 房价计划罚金；DEC-02 负余额按既定退款流程；DEC-03 押金 = `Reservation.total_amount` × 30%；另有 MC11-HK-02 维修完成 ≠ 可售 | 擅改 → 账务口径漂移，已裁定项需负责人改判 | 冻结（`INHERIT`） |
| 5 | **不引入未经批准的新技术栈** | 现栈：Django 4.1 + 原生 JS + MySQL 8 + 浏览器原生；仓库另有 `docs/PMS3.0_后续规划/99_历史版本/` 归档 | 引入第二套 ORM/前端框架/消息中间件 → 属重复建设 + L3 | 冻结（硬约束） |
| 6 | **不重复建设已有 Service、Model、审批和审计能力** | PMS3-REQ-001 §14 禁止重复建设清单 **12 条**（唯一 Guest / 唯一预订 / 唯一账本 / 唯一库存占用 / 唯一员工身份授权 / 唯一维修状态机 / 唯一 PII 加密 / AuditEvent 唯一 / WorkItem 非业务事实源 / 不得新增第三份 OpenAPI 快照 / 不得把「移交 PMS3」计为 PMS2 已实现 / 不得把 PROD-01~08 改标已完成） | 重复建设 → 技术债 + 与 PMS2.0 双写不一致 | 冻结（12 条） |
| 7 | **PROD-01～08 不得伪装成已完成** | 8/8 保持 `PRODUCTION_PENDING`（`WAITING_FOR_SERVER`/`WAITING_FOR_DBA`/`WAITING_FOR_PROVIDER`/`PRODUCTION_PENDING`）；M5-4 三项前置未满足（实际被 8 项共同阻断） | 以本地 SQLite/MySQL 测试冒充生产演练、以 mock 支付冒充真实渠道、以本地备份冒充生产备份恢复验收 | 冻结（禁止改标） |

> 与 PMS3-REQ-001 §13 的关系：任务书 §六 的 7 项冻结，与 PMS3-REQ-001 §13.1 架构/产品边界 F-11～F-16、§13.2 已批准业务裁定 F-01～F-10、§14 禁止重复建设 12 条**方向一致、互为补充**，无冲突。F-08（不批量扩展权限码）与 F-10（审计只追加）直接支撑本表第 1、11 项。

## 三、PMS3.0 启动前必须形成的 9 项材料（任务书 §六）

| # | 材料 | 当前状态 | 载体 |
|---|---|---|---|
| 1 | **PMS2.0 最终基线说明** | ✅ 已具备 | `docs/acceptance/G14同基线最终补测报告_2026-11-26.md`（DEL-01，基线 `0ca1ce4`）+ `docs/acceptance/PMS2.0最终收口与PMS3.0启动准备报告_2026-11-26.md`（DEL-12） |
| 2 | **PMS3.0 需求继承矩阵** | ✅ 已具备 | `docs/PMS3.0_后续规划/01_需求拆分与开发批次/PMS3需求继承与新增开发矩阵_2026-11-22.md`（PMS3-REQ-001，89 条 / 8 态）+ 本矩阵 §一 |
| 3 | **已批准业务决策清单** | ✅ 已具备 | DEC-01/02/03/04/06/16/17/19/20/26/27 + NS-E-01～05 + MC11-HK-02 + OWNER-02/05/06/08/09（均以 GOV-REQ-001 与 PMS3-REQ-001 §13.2 为准） |
| 4 | **未决 Owner 事项清单** | ✅ 已具备 | GOV-REQ-001（`WAITING_OWNER` 2 项）+ PMS3-REQ-001（`WAITING_OWNER` 6 项）+ 本轮新增：ARCH-05 改密二次认证、ARCH-04 薪酬/工龄归属、ARCH-04 保留策略审批（见 DEL-02 §四 R5、R6） |
| 5 | **旧路由及模板处置矩阵** | ✅ 已具备 | `docs/acceptance/ARCH-02旧路由处置矩阵_2026-11-26.md`（67 条）+ `ARCH-03旧模板处置矩阵_2026-11-26.md`（37 个） |
| 6 | **LegacyStaff 迁移边界** | ✅ 已具备 | `docs/acceptance/ARCH-04LegacyStaff迁移方案_2026-11-26.md`（引用盘点 + 映射规则 + 迁移/回滚/核对 + 16 项退役条件） |
| 7 | **Outbox 事件风险策略** | ✅ 已具备 | 事件类型与风险级别登记表 + 入队/消费双端 HIGH_RISK 硬拒绝（提交 `2604b19`、`18ab698`），36 项 outbox 用例在 MySQL 全绿 |
| 8 | **生产依赖清单** | ✅ 已具备 | `docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md`（DEL-10，8 项 + 3 项前置 + 聚合门禁 GATE-PROD） |
| 9 | **PMS3.0 第一阶段开发范围及验收标准** | ✅ 已具备 | PMS3-REQ-001 §12.1「第一批开发边界 8 项」+ §0 处置码/§15 字段规范（工作量、依赖、验收标准、是否需负责人批准） |

**结论：9 项材料全部就位**，PMS3.0 启动的**材料门槛**已满足；剩余闸门是 G14 负责人签署（DEL-02 §六）与生产事项（不影响本地开发）。

## 四、与 PMS3-REQ-001（89 条 8 态矩阵）的关系

| 项 | PMS3-REQ-001 | 本矩阵 | 关系 |
|---|---|---|---|
| 覆盖对象 | 89 条需求级条目（12 模块） | 14 继承项 + 7 冻结项 + 9 材料 | **不同粒度**：前者是需求条目，本矩阵是任务书点名的架构/流程约束 |
| 状态枚举 | 8 态（INHERIT/EXTEND/CARRY_OVER/NEW/WAITING_OWNER/WAITING_EXTERNAL/DEFERRED/DEPRECATED） | 冻结级别（L3 闸门 / 硬约束 / 目标继承 / 冻结） | 本矩阵**不另立状态枚举**，只标注冻结强度 |
| 条目数 | 89（INHERIT 39 / EXTEND 1 / CARRY_OVER 29 / NEW 3 / WAITING_OWNER 6 / WAITING_EXTERNAL 5 / DEFERRED 6 / DEPRECATED 0） | 30（14+7+9） | 本矩阵不新增需求条目，**不改变 89 条的处置码** |
| 冻结边界 | §13.1 F-11～F-16、§13.2 F-01～F-10、§14 十二条 | §二 7 项 | **一致且互补**：任务书 §六 的 7 项为上位要求，PMS3-REQ-001 §13 是其在 PMS2.0 裁定层的细化 |

**本轮（PMS2-FINAL-DECISION-EXEC-002）对继承矩阵的增量**（不改判既有条目，仅登记本轮新增的交付与保留）：

| 增量 | 内容 | 对 PMS3 的影响 |
|---|---|---|
| 前端体验增强库 `static/js/pms-ux.js` + `pms-ux.css` + `_ux_assets.html`（27 页 head 注入） | 全局交互增强，零依赖、渐进增强、`data-ux-*` 声明式 opt-in | 可继承；新增页面按需 opt-in，**不引入前端框架**（呼应 §二 第 5 项） |
| ARCH-02 67 条路由处置矩阵 | KEEP 15 / REDIRECT 13 / 410 39 / MIGRATE 0 / DELETE 0 | 路由退役节奏是 PMS3 的 P3-RES-07/08 依赖项，处置结论可直接引用 |
| ARCH-03 37 个模板处置矩阵 | KEEP 25 / MIGRATE 10 / DELETE 2（零引用证据） | 模板处置须在 PMS3 内按「先迁链接→再重定向→最后删模板」顺序执行 |
| ARCH-04 LegacyStaff 迁移方案 + 16 项退役条件 | 旧 Staff 表零写路径已达（C3/C5 已满足），10 项待关闭 | 纳入 PMS3 的 `P3-EMP-07 CARRY_OVER` |
| ARCH-05 改密二次认证缺口 | 安全增强未实施，需负责人裁定 | 新增待裁定项，见 §三 第 4 项 |

## 五、PMS3.0 第一阶段建议范围

以 PMS3-REQ-001 §12.1 指定的**第一批 8 项**为准（不新增、不改判），仅补充执行顺序建议：

| 顺序 | 第一批边界项 | 建议前置 | 备注 |
|---|---|---|---|
| 1 | 通知与待办权威关系（NTF-05，WorkItem 仅待办提示） | 无（`INHERIT`，可立即开工） | 零迁移成本 |
| 2 | Outbox 与事件可靠性（P3-PLT-01） | 事件目录与契约（DEC-27）已批准 | 不得新建第二套事件总线 |
| 3 | 历史数据只读与归档体系（P3-G2-08 / P3-RES-07/08 / P3-FIN-04） | 依赖 ARCH-02/03 处置矩阵 + ARCH-04 退役条件 | 归档实施仍被 M5-4 八项前置阻断，**PMS3 不得绕过** |
| 4 | Guest 重复档案治理受控流程（P3-G2-06/07） | 合并规则需负责人批准（DEC-03/06） | 禁止自动合并 |
| 5 | 会员历史关系与权益治理（P3-MB-02/03、P3-G2-09） | 等级目录与权益体系需负责人决策 | 权益/积分未经 DEC-15 批准不得建模 |
| 6 | 夜审调度监控与失败提示（P3-NA-03/04） | 已批准移交（`APPROVED_PMS3_HANDOFF`） | 承接载体为 PMS3 专项，不得重复派发 |
| 7 | 审计完整性增强（P3-AUD-03） | 已批准移交 | **不得做伪链式哈希、不得重写历史**（F-06/F-10） |
| 8 | 经营报表扩展（P3-RPT-07/03） | 指标定义前置；口径扩展需负责人确认（DEC-16） | — |

**不得纳入 PMS3.0 第一阶段的事项**：PROD-01～08 相关实现（生产资源未到位）、G14 签署动作（仅负责人）、任何绕过 OWNER 裁定的功能实现。

## 六、启动门槛判定

| 门槛 | 状态 |
|---|---|
| 材料门槛（§六 9 项） | ✅ 全部就位 |
| 技术验收门槛（DEL-01/DEL-02） | ✅ `PASS_WITH_RESERVATIONS`（保留 6 条已列明） |
| G14 负责人签署 | ❌ **未完成**（仅负责人本人可执行） |
| 生产就绪 | ❌ 不适用（PMS3.0 本地开发不依赖生产事项，任务书 §五末句明确） |

**判定：PMS3.0 可在满足 G14 签署后启动本地开发**；本矩阵不替代 G14 签署，也不代替 MC11 独立验收。
