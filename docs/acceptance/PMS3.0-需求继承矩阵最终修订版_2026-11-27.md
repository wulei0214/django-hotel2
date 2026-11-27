# PMS3.0 需求继承矩阵最终修订版（2026-11-27）

> 文档编号：PMS2-PMS3-INHERIT-002 ｜ 类型：PLANNING ｜ 状态：ACTIVE ｜ 版本域：PMS3.0 ｜ 业务模块：版本继承
> 任务编号：TASK-22 ｜ 交付物：DEL-18
> 执行日期：2026-11-27 ｜ 代码基线：main HEAD `0fa7086`（`git rev-parse HEAD` 现取；`git rev-list --left-right --count origin/main...HEAD` = `0 0`）
> 上版基线：`docs/acceptance/PMS3.0需求继承及冻结矩阵_2026-11-26.md`（PMS2-PMS3-INHERIT-001，绑 `0ca1ce4`）
> 上游权威矩阵：`docs/PMS3.0_后续规划/01_需求拆分与开发批次/PMS3需求继承与新增开发矩阵_2026-11-22.md`（PMS3-REQ-001，89 条 / 8 态处置码）
> 任务依据：《PMS 2.0 最终验收补证、遗留事项整改与 PMS 3.0 启动准备任务书》PMS2-FINAL-ACCEPTANCE-REMEDIATION-003 V1.0 §TASK-22（任务书原文第 834–856 行）
> 执行方式：全仓只读取证（Read / `grep` 实跑 / `git rev-parse` / `git log` / `Test-Path`）+ 本轮 11 份已入库交付物结论复用。**未改动任何 `.py`、模板、配置、测试，未改动任何历史文档。**
> **纪律**：本矩阵只做「PMS2.0 现状 → PMS3.0 处置」的映射与状态修订，不新增业务规则、不改写已批准裁定、不把未批准事项写成已批、不把「移交 PMS3」计入 PMS2 已实现功能。

---

## 一、结论摘要

| # | 结论 | 状态 |
|---|---|---|
| 1 | **89 条需求全部逐条给出最终状态**，Req ID 沿用 PMS3-REQ-001，**未新增、未删除、未改号**；分布 = `DONE` 46 / `PASS_WITH_RESERVATIONS` 8 / `NOT_STARTED` 19 / `DEFERRED_TO_PMS3` 7 / `WAITING_OWNER` 5 / `WAITING_ENVIRONMENT` 4 / `PASS` 0 / `FAIL` 0 = **89** | `PASS` |
| 2 | **处置码改判 9 条**：`CARRY_OVER → INHERIT` 6 条（EMP-07 / G2-04 / RES-08 / RPT-04 / PLT-01 / PLT-05）、`WAITING_EXTERNAL → INHERIT` 1 条（RES-09）、`WAITING_OWNER → INHERIT` 1 条（PLT-06）、`EXTEND → CARRY_OVER` 1 条（EMP-05）。改判后处置码分布 = INHERIT 47 / CARRY_OVER 24 / DEFERRED 6 / WAITING_OWNER 5 / WAITING_EXTERNAL 4 / NEW 3 / EXTEND 0 / DEPRECATED 0 = **89** | `PASS` |
| 3 | **TASK-01～TASK-13 完成情况已逐项同步**（§2.4），其中 TASK-01～TASK-06 为 `PASS_WITH_RESERVATIONS`（G14 保留 6 条、签署页留空），TASK-07 / TASK-08 / TASK-11 为 `DONE`，TASK-09 / TASK-10 / TASK-13 为 `PASS_WITH_RESERVATIONS`，TASK-12 为 `WAITING_ENVIRONMENT` | `PASS` |
| 4 | **T02-3 方案 B 完成状态已同步**：`APPROVED_WITH_CONDITIONS → DONE`（代码/测试/文档/CI 14 项全闭环，基线 `80fa7c5`）；生产真实批量清理维持 `PRODUCTION_PENDING`，期限与法律责任人仍 `WAITING_OWNER` | `PASS` |
| 5 | **NS-E-05 具体权限裁定已同步**：5 个权限码逐项裁定并落地（FIN-06 `billing.payment_collect`、`MB-11 `guests.membership_status_manage` 为 `APPROVED`；Retention 三码 `guests.retention_request/approve/execute` 为 `APPROVED_WITH_CONDITIONS`）；专项权限码总数 22 → **27**（`apps/identity/registry.py:492-758` 现取） | `DONE` |
| 6 | **ARCH-02～ARCH-05 当前状态已同步**：ARCH-02 `DONE`（KEEP 20 / REDIRECT 15 / 410 32）、ARCH-03 `DONE`（KEEP 29 / MIGRATE 6 / DELETE 2，模板总数 37 → 29）、ARCH-04 `PASS_WITH_RESERVATIONS`（C11/C12/C14 `WAITING_OWNER`）、ARCH-05 `PASS_WITH_RESERVATIONS`（7 核查点 6 `PASS` + 1 `WAITING_OWNER`） | `PASS` |
| 7 | **OWNER-05、OWNER-07 等待负责人事项保留为 `WAITING_OWNER`**，未代拍板、未实施任何受其约束的业务变更 | `WAITING_OWNER` |
| 8 | **PROD-01～PROD-08 全部保留 `PRODUCTION_PENDING` 类标记，8/8 未改标**，未以本地测试冒充生产验收 | `PRODUCTION_PENDING` |
| 9 | **M5-4 生产归档与本地代码归档已分别登记**（§2.10）：本地代码归档层 `PASS_WITH_RESERVATIONS`，生产真实数据归档层 `PRODUCTION_PENDING` + `WAITING_OWNER`（八项前置 **0/8 满足**） | `PASS_WITH_RESERVATIONS` |
| 10 | **已移交 PMS3 的旧页面与功能未重复登记为 PMS2 未完成开发**（§2.11 共 6 条 `DEFERRED_TO_PMS3`），登记为 PMS3 承接范围 | `DEFERRED_TO_PMS3` |
| 11 | **PMS3 新增需求 3 条全部缺负责人批准记录** → 一律标 `NOT_STARTED` 并注明缺失项（§2.12） | `NOT_STARTED` |
| 12 | **基线口径差异已声明**：`docs/共享规范/PMS当前状态总览.md:5,10` 记当前基线 `6d7d9ce`、权威全量 `2177/2177`（绑 `5cad6b7`）；本轮现取 HEAD 为 `0fa7086`，本轮 G14 双引擎全量为 **2229/2229**（绑 `23fdd00`）。**以现取为准，总览数字待主控会话统一更新。** | `PASS` |

---

## 二、正文

### 2.1 本版口径：状态词与处置码分列（两轴正交，不可混列）

上一版矩阵声明「不另立状态枚举，只引用 PMS3-REQ-001 的 8 态处置码」。本轮 OWNER-07 裁定材料已量化该做法的代价：同一批 89 条需求在台账挂 9 态、在 PMS3 挂 8 态处置码，两套词表并行作用于同一条目，且台账内部已出现 10 条实证冲突（`docs/acceptance/OWNER-07-需求状态体系统一裁定材料_2026-11-27.md` 结论 2/4/6）。故本版按该材料结论 7 的建议**分列两轴**：

| 轴 | 取值 | 回答的问题 | 本版取值来源 |
|---|---|---|---|
| **最终状态**（结论词） | `DONE` / `PASS` / `PASS_WITH_RESERVATIONS` / `WAITING_OWNER` / `WAITING_ENVIRONMENT` / `DEFERRED_TO_PMS3` / `NOT_STARTED` / `FAIL` | **现在是什么** | 共同作业说明 §4.10 规定的 8 态词表；本版全部条目只用这 8 个词 |
| **PMS3 处置码** | `INHERIT` / `EXTEND` / `CARRY_OVER` / `NEW` / `WAITING_OWNER` / `WAITING_EXTERNAL` / `DEFERRED` / `DEPRECATED` | **下一版做什么** | PMS3-REQ-001 §0 的 8 态处置码（2026-11-22 版） |

> **生产事项例外声明**：PROD-01～PROD-08 按共同作业说明 §4.5 与任务书 TASK-22 第 6 项保留 `PRODUCTION_PENDING` 类标记。该标记**只用于生产依赖登记表（§2.9）**，不出现在 89 条需求矩阵的「最终状态」列内。

> **映射规则（可机械复核）**：`INHERIT`→`DONE`；`EXTEND`→`DONE`（PMS2 侧已完成，增强在 PMS3）；`CARRY_OVER`→ 按 PMS2 实际完成度取 `NOT_STARTED` / `PASS_WITH_RESERVATIONS` / `DONE`；`NEW`→`NOT_STARTED`（本版 3 条新增均缺批准记录）；`WAITING_OWNER`→`WAITING_OWNER`；`WAITING_EXTERNAL`→`WAITING_ENVIRONMENT`；`DEFERRED`→`DEFERRED_TO_PMS3`。

### 2.2 基线与口径差异声明（现取现核）

| 项 | 总览记载 | 本轮现取 | 处理 |
|---|---|---|---|
| 当前 main HEAD | `6d7d9ce`（`docs/共享规范/PMS当前状态总览.md:10`） | **`0fa7086`**（`git rev-parse HEAD`，`origin/main` 同点） | 以现取为准 |
| 权威双引擎全量 | 2177/2177，绑 `5cad6b7`（总览 `:37,43`） | **2229/2229**，绑 `23fdd00`（`docs/acceptance/G14同基线最终补测报告_2026-11-27.md:16-17`） | 以现取为准；阶梯只增不减 |
| OpenAPI 契约 | 175 paths / 177 ops，SHA-256 `EA1C97BD…B6E197`，1,339,672 字节（总览 `:27`） | **175 paths / 177 ops，OpenAPI 3.1.0**（现取 `docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json`） | 路径数一致，字节数未现取，本版不引用字节数 |
| 专项权限码总数 | 22 个（2026-11-26 版矩阵 `:22`） | **27 个**（`apps/identity/registry.py:492-758`，NS-E-05 新增 5 个） | 以现取为准 |
| 模板总数 | 37 个（2026-11-26 版矩阵 `:17`） | **29 个**（现取 `templates/` 递归计数；ARCH-03 退役 8 个） | 以现取为准 |
| Django / MySQL | Django 4.1 + MySQL 8.0.33 | Django **4.1**（`django.get_version()`）、MySQL 8.0.33（G14 报告 `:5`） | 一致 |

### 2.3 完整需求矩阵（89 条，逐条最终状态）

> 计数口径：12 张分节表逐行清点「最终状态」列，合计须 = 89；Req ID 唯一性沿用 PMS3-REQ-001 §16 复核方式。

#### 2.3.1 员工与 RBAC（apps.identity，9 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-EMP-01 | 员工生命周期全链（建档/开通/邀请/改密/授权/停用/离职/再入职） | `INHERIT` | `DONE` | 无变化；离职终止授权与「不得直接恢复旧任职」守卫在位 | `docs/共享规范/PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md:108`；`apps/identity/services.py:522-577` |
| P3-EMP-02 | 九岗位权限矩阵 + `AuthorizationService` | `INHERIT` | `DONE` | 无变化；九岗位 90 格 failures=0 | `docs/acceptance/G14同基线最终补测报告_2026-11-27.md:20` |
| P3-EMP-03 | 平台身份与 PMS 员工身份分域 | `INHERIT` | `DONE` | 无变化；双 Path Cookie 隔离 | PMS3-REQ-001 `:67`；`hotel/settings.py:222`（`CSRF_USE_SESSIONS = True`） |
| P3-EMP-04 | 跨物业调岗（MC11-ST-03） | `CARRY_OVER` | `DEFERRED_TO_PMS3` | 单物业边界冻结（EMP-02 `REJECTED` 当前版本），多物业属 L3 架构变更，未获批准前继续冻结 | 台账 `:109`；F-11（PMS3-REQ-001 `:237`） |
| P3-EMP-05 | 离职员工 WorkItem 批量转交（MC11-ST-04） | `EXTEND` → `CARRY_OVER` | `NOT_STARTED` | **处置码改判**：`EXTEND` 的前提「PMS2 已完成」不成立——现取 `apps/identity/services.py` 内零 WorkItem 转交命中，离职联动批量重派未实现，仅有 dedupe_key 销项 | `apps/identity/services.py:528-577`（现取零命中 WorkItem）；台账 `:110` |
| P3-EMP-06 | 联系电话长度口径统一（MC11-ST-05：20/32/11） | `CARRY_OVER` | `NOT_STARTED` | 无变化；口径仍不一致：`models.py:40` 为 `max_length=20`，`:89`/`:94` 为 `max_length=32` | `apps/identity/models.py:40,89,94`；台账 `:111` |
| P3-EMP-07 | 旧员工批量迁移映射（MC11-ST-07） | `CARRY_OVER` → `INHERIT` | `DONE` | **改判**：映射台账 `LegacyStaffMapping` 已落地（两侧一对一 + 旧主键原值 + 映射状态 + 核验人/核验时间），旧 `StaffProfile.legacy_staff` 外键由迁移 `0016` 回填 | `apps/identity/models.py:594-697`；`docs/acceptance/ARCH-04技术整改与负责人裁定材料_2026-11-27.md` 结论 1 |
| P3-EMP-08 | 员工链浏览器证据补录（MC11-ST-06） | `INHERIT` | `DONE` | 无变化 | 台账 `:112` |
| P3-EMP-09 | 多物业组织架构 | `WAITING_OWNER` | `WAITING_OWNER` | 无变化；仍待负责人裁定（默认范围外） | PMS3-REQ-001 `:73` |

#### 2.3.2 Guest 360、PII 与数据生命周期（apps.guests，9 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-G2-01 | 手机号密文 + HMAC 索引 + 受控揭示 | `INHERIT` | `DONE` | 无变化；禁止第二套 PII 加密体系 | `apps/guests/services.py`；台账 `:120` |
| P3-G2-02 | 证件密文 + `doc_no_hash` 物业级唯一 | `INHERIT` | `DONE` | 无变化 | `apps/guests/models.py:413`；台账 `:121` |
| P3-G2-03 | Guest 姓名加密 + 盲索引 | `CARRY_OVER` | `PASS_WITH_RESERVATIONS` | **状态升级**：阶段 1（Fernet 密文 + HMAC 盲索引 + 双写）已落地；**保留项**：明文列 `full_name` 仍保留（阶段 2 切换与迁移未做），且 `reveal_guest_name` 已按 OWNER-06 治理整体移除，**姓名揭示不得恢复** | `apps/guests/name_pii.py:1-15,73-74`；`apps/guests/services.py:377-378`；台账 `:122` |
| P3-G2-04 | 数据保留 / Legal Hold / 清理执行记录 | `CARRY_OVER` → `INHERIT` | `PASS_WITH_RESERVATIONS` | **处置码改判 + 状态升级**：本地代码层已落地 `RetentionPolicy` / `LegalHold` / `RetentionExecution` + `apps/guests/retention.py`，T02-3 方案 B 14 项全闭环；**保留项**：期限值与法律责任人仍 `WAITING_OWNER`、生产真实清理 `PRODUCTION_PENDING`（分别登记于 §2.8/§2.9） | `apps/guests/models.py:487,579,691`；`apps/guests/retention.py`；`docs/acceptance/T02-3最终核验报告_2026-11-25.md` |
| P3-G2-05 | 客人时间线完整聚合（MC11-G360-05） | `CARRY_OVER` | `PASS_WITH_RESERVATIONS` | 有实现、缺独立动态验收（台账 `IMPLEMENTED_UNVERIFIED`） | 台账 `:124`；`apps/api/guest_center.py:428` |
| P3-G2-06 | 重复档案候选治理 API/页面（MC11-G360-06） | `CARRY_OVER` | `PASS_WITH_RESERVATIONS` | 受控合并端点已落地（reason + 审计 + 软归档）；**保留项**：候选列表 / 影响面预览 / 双人复核与撤销机制未落地；**禁止仅凭姓名或手机号自动合并**（F-16） | `apps/api/guest_center.py:912-954`；台账 `:125`；F-16（PMS3-REQ-001 `:242`） |
| P3-G2-07 | 合并软归档 | `INHERIT` | `DONE` | 无变化 | 台账 `:126` |
| P3-G2-08 | Client/Vip 历史归档（映射 + dry-run + 对账） | `CARRY_OVER` | `PASS_WITH_RESERVATIONS` | **本地代码归档层已落地**（七表 spec + 密文 transform + 迁移 0037/0039 + 回归）；**保留项**：LegalHold 归档批次级未落地、查询等价性未验；生产归档层另行登记（§2.10） | `apps/billing/legacy_archive.py:74-109,199-225`；`apps/billing/models.py:2599-2904`；`docs/acceptance/M5-4-归档实施与生产边界修订报告_2026-11-27.md` 结论 1/3 |
| P3-G2-09 | 积分/权益体系 | `WAITING_OWNER` | `WAITING_OWNER` | 无变化；未经 DEC-15 批准不得建模 | PMS3-REQ-001 `:87` |

#### 2.3.3 房态、库存与排房（apps.inventory，6 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-INV-01 | 房态三维分离 + 可售派生 | `INHERIT` | `DONE` | 无变化 | 台账 `:133` |
| P3-INV-02 | `RoomAssignmentConflict` 唯一键仲裁 + 并发 | `INHERIT` | `DONE` | 无变化；MySQL 并发仲裁 55 例 OK | `docs/acceptance/G14同基线最终补测报告_2026-11-27.md:18` |
| P3-INV-03 | 差异工作流五步 + 冲突只读端点 | `INHERIT` | `DONE` | 无变化 | 台账 `:135` |
| P3-INV-04 | 差异扫描异步化（MC11-INV-06） | `DEFERRED` | `DEFERRED_TO_PMS3` | 无变化；**已批准移交 PMS3，不得重复登记为 PMS2 未完成开发** | PMS3-REQ-001 `:96`；台账 `:137` |
| P3-INV-05 | 扫描中断恢复（批次 ID + 游标 + 续跑） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | 台账 `:136` |
| P3-INV-06 | `rate_adjust` 归属口径（MC11-INV-07） | `INHERIT` | `DONE` | 无变化 | 台账 `:138` |

#### 2.3.4 会员（apps.guests，7 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-MB-01 | 会员创建/等级/暂停/恢复 + `expires_at` 全链 | `INHERIT` | `DONE` | 无变化 | 台账 `:144` |
| P3-MB-02 | 等级目录受控（MC11-MB-05） | `CARRY_OVER` | `NOT_STARTED` | 无变化；等级目录定义属数据规范，待裁定 | 台账 `:146` |
| P3-MB-03 | `member_no` 冲突人工复核（MC11-MB-09） | `CARRY_OVER` | `PASS_WITH_RESERVATIONS` | **状态升级**：物业级唯一约束与写入前拒绝已落地（唯一键 + 重复即拒）；**保留项**：「人工复核流程」未落地 | `apps/guests/models.py:413`；`apps/guests/services.py:973-974`；台账 `:147` |
| P3-MB-04 | TERMINATED/PENDING 生命周期入口（MC11-MB-04） | `WAITING_OWNER` | `WAITING_OWNER` | 无变化 | 台账 `:145` |
| P3-MB-05 | 会员来源 `source` 透出 | `INHERIT` | `DONE` | 无变化 | PMS3-REQ-001 `:108`（`27f70cd`） |
| P3-MB-06 | Vip→会员迁移映射（MC11-MB-06） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | PMS3-REQ-001 `:109` |
| P3-MB-07 | 表单可用性（MC11-MB-10） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | 台账 `:148` |

#### 2.3.5 预订与入住（apps.reservations，10 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-RES-01 | 公开预订 + PENDING Hold + 过期释放 | `INHERIT` | `DONE` | 无变化 | 台账 `:157` |
| P3-RES-02 | 排房/入住/退房/续住/换房/取消 | `INHERIT` | `DONE` | 无变化；单预订全链路闭环测试在位 | PMS3-REQ-001 `:117` |
| P3-RES-03 | Day-use 建单/政策价/转过夜 | `INHERIT` | `DONE` | 无变化 | 台账 `:156` |
| P3-RES-04 | 提前离店罚金（DEC-01） | `INHERIT` | `DONE` | 无变化；不得擅改 DEC-01 | PMS3-REQ-001 `:119` |
| P3-RES-05 | 押金 30%（DEC-03） | `INHERIT` | `DONE` | 无变化；不得擅改 DEC-03 | PMS3-REQ-001 `:120` |
| P3-RES-06 | 负余额退款流程（DEC-02） | `INHERIT` | `DONE` | 无变化；不得擅改 DEC-02 | PMS3-REQ-001 `:121` |
| P3-RES-07 | 模块③旧事件链/状态机/旧读路径收口 | `CARRY_OVER` | `PASS_WITH_RESERVATIONS` | **状态升级**：旧事件无消费者已核验（TASK-06 验收 `PASS`），旧路由/旧模板面已由 ARCH-02/03 收口并加「零写库」断言；**保留项**：`folio/` 仍恒返回 404（改 410 待裁定）、`booking/` 302 是否升级永久重定向待业务确认 | `docs/acceptance/TASK06-09验收复核报告_2026-11-24.md` 结论表 TASK-06；`docs/acceptance/ARCH-02与03退役实施报告_2026-11-27.md:52`；`docs/acceptance/G14同基线最终补测报告_2026-11-27.md:157-158` |
| P3-RES-08 | Walk-in 旧入口退役（`/walk-ins`→410） | `CARRY_OVER` → `INHERIT` | `DONE` | **改判**：旧 `/walk-ins` 端点已不在路由表，现取仅存标准入口 `walkin_create/`；410 流量统计脚本保留生产观察口径 | `hotel/urls.py:145`；`scripts/walkin_410_stats.py`（`Test-Path` = True）；台账 `:163` |
| P3-RES-09 | `/checkin/legacy/` 旧页可达（MC11-RES-07） | `WAITING_EXTERNAL` → `INHERIT` | `DONE` | **改判**：`checkin/legacy/` 已改为 `_legacy_center_redirect("/checkin/", …)` → 302 到新中心，且已补「只读不写库」断言；生产流量观察不作为 PMS3 阻塞项（转 DEP-PROD-A 跟踪） | `hotel/urls.py:137`；`docs/acceptance/ARCH-02与03退役实施报告_2026-11-27.md:40` |
| P3-RES-10 | 特殊预订状态扩展 | `NEW` | `NOT_STARTED` | 无变化；**缺负责人批准记录**（见 §2.12） | PMS3-REQ-001 `:125` |

#### 2.3.6 财务与 Folio（apps.billing，10 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-FIN-01 | 统一账本写入口 + 不可变凭证 | `INHERIT` | `DONE` | 无变化；禁止第二套账本 | PMS3-REQ-001 `:131` |
| P3-FIN-02 | 退款申请/审批/执行三方分离 | `INHERIT` | `DONE` | 无变化 | PMS3-REQ-001 `:132`；台账 `:174`（FIN-04 退款三分离） |
| P3-FIN-03 | 收银班次现金管理 | `INHERIT` | `DONE` | 无变化 | PMS3-REQ-001 `:133` |
| P3-FIN-04 | 财务旧数据迁移逐笔对账 + 只读收口 | `CARRY_OVER` | `NOT_STARTED` | 无变化 | 台账 `:178` |
| P3-FIN-05 | 银行资金对账（MC11-FIN-01） | `WAITING_EXTERNAL` | `WAITING_ENVIRONMENT` | 无变化；等待银行资料与生产环境 | PMS3-REQ-001 `:135` |
| P3-FIN-06 | 真实支付渠道（MC11-FIN-02） | `WAITING_EXTERNAL` | `WAITING_ENVIRONMENT` | 无变化；仓库内支付为模拟/沙箱路径，**不得伪造渠道证据** | `docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:36` |
| P3-FIN-07 | 退款失败/重试系统表达（MC11-FIN-03） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | PMS3-REQ-001 `:137` |
| P3-FIN-08 | `Folio.VOIDED` 生产写路径（MC11-FIN-07） | `WAITING_OWNER` | `WAITING_OWNER` | 无变化 | 台账 `:176` |
| P3-FIN-09 | 双轴口径页面区分（MC11-FIN-08） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | 台账 `:177` |
| P3-FIN-10 | 支付/退款/账本状态机扩展 | `NEW` | `NOT_STARTED` | 无变化；**缺负责人批准记录**（见 §2.12） | PMS3-REQ-001 `:140` |

#### 2.3.7 营业日与夜审（apps.core + apps.billing，7 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-NA-01 | 营业日 + 夜审预览/执行/关账 | `INHERIT` | `DONE` | 无变化 | 台账 `:185` |
| P3-NA-02 | 重开双人审批 + Break-glass | `INHERIT` | `DONE` | 无变化 | PMS3-REQ-001 `:147`；台账 `:185,191` |
| P3-NA-03 | `SchedulerRun` 调度运行记录 | `DEFERRED` | `DEFERRED_TO_PMS3` | 无变化；NS-E-04 `APPROVED_PMS3_HANDOFF`，**不得计为 PMS2 已实现** | PMS3-REQ-001 `:148`；台账 `:188` |
| P3-NA-04 | 调度停摆探测 + 失败告警 | `DEFERRED` | `DEFERRED_TO_PMS3` | 无变化；同上 | PMS3-REQ-001 `:149`；台账 `:189` |
| P3-NA-05 | 自动夜审 + 忘记夜审提醒（MC11-NA-11） | `DEFERRED` | `DEFERRED_TO_PMS3` | 无变化；现取全仓 `auto_night_audit` / `自动夜审` 零命中，能力未在 PMS2 落地 | PMS3-REQ-001 `:150`；台账 `:190` |
| P3-NA-06 | 复核 REJECTED 端到端（MC11-NA-07） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | 台账 `:186` |
| P3-NA-07 | 单一经理资格边界用例（MC11-NA-08） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | 台账 `:187` |

#### 2.3.8 客房与维修（apps.inventory，5 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-HK-01 | 清洁任务状态机 + 房态同事务 | `INHERIT` | `DONE` | 无变化 | 台账 `:207` |
| P3-HK-02 | 维修全链 + SoD | `INHERIT` | `DONE` | 无变化；SoD 不可放宽 | PMS3-REQ-001 `:159` |
| P3-HK-03 | 工单取消不自动恢复可售（NS-E-01） | `INHERIT` | `DONE` | 无变化 | 台账 `:209` |
| P3-HK-04 | `RESTORED` 死状态清理（MC11-HK-08） | `CARRY_OVER` | `NOT_STARTED` | 无变化；枚举值 `RESTORED` 仍在位，模板标签未清理 | `apps/inventory/models.py:443`；台账 `:210` |
| P3-HK-05 | 报修表单字段暴露（MC11-HK-09） | `CARRY_OVER` | `NOT_STARTED` | 无变化；现取 `severity` / `category` / `from_date` 在报修表单与接口零命中 | `templates/pms/housekeeping_center.html`；台账 `:211` |

#### 2.3.9 审计与合规（apps.core，6 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-AUD-01 | 追加式审计 + 三层防护 | `INHERIT` | `DONE` | 无变化 | 台账 `:197` |
| P3-AUD-02 | 受控导出 + 凭证 + 脱敏 | `INHERIT` | `DONE` | 无变化 | 台账 `:197` |
| P3-AUD-03 | 链式哈希 + 外部锚定（NS-E-03） | `DEFERRED` | `DEFERRED_TO_PMS3` | 无变化；**不得做伪链式哈希、不得为补哈希重写历史审计** | PMS3-REQ-001 `:170` |
| P3-AUD-04 | 导出截断标记（MC11-AUD-06） | `CARRY_OVER` | `NOT_STARTED` | 审计导出侧只设上限 `MAX_EXPORT_ROWS = 10000`，**未见截断标记**（报表侧已实现，见 P3-RPT-04） | `apps/api/audit_governance.py:34,67`；台账 `:200` |
| P3-AUD-05 | 到期销毁独立审批 + 只读备份（MC11-AUD-07） | `WAITING_OWNER` | `WAITING_OWNER` | 无变化；与 P3-G2-04 联动，期限与法律 owner 未定 | 台账 `:201` |
| P3-AUD-06 | MySQL 触发器安装 + 备份登记（MC11-AUD-05） | `WAITING_EXTERNAL` | `WAITING_ENVIRONMENT` | 本机 MySQL 账号无 SUPER 且开 binary log → 建触发器报 1419，行为级验证必须在具备权限的独立 MySQL 上做 | `AGENTS.md:43`；`docs/acceptance/G14同基线最终补测报告_2026-11-27.md:160` |

#### 2.3.10 报表（apps.api，7 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-RPT-01 | 7 类报表 + 口径 metadata + CSRF 导出 | `INHERIT` | `DONE` | 无变化 | 台账 `:217` |
| P3-RPT-02 | 源数据独立复算工具 | `INHERIT` | `DONE` | 无变化；禁止只比较两个同源函数 | 台账 `:217` |
| P3-RPT-03 | 跨报表勾稽说明（MC11-RPT-03） | `CARRY_OVER` | `NOT_STARTED` | 无变化；现取全仓 `勾稽` 在 `apps/`、`templates/` 零命中 | 台账 `:218` |
| P3-RPT-04 | 导出截断标记（MC11-RPT-04） | `CARRY_OVER` → `INHERIT` | `DONE` | **改判**：导出上限与截断标记均已落地（CSV 首行写 `# truncated: 仅导出前 5000 行`） | `apps/api/report_export.py:35,78-79`；台账 `:219` |
| P3-RPT-05 | housekeeping 报表依赖审计留存（MC11-RPT-05） | `INHERIT` | `DONE` | 无变化 | 台账 `:220` |
| P3-RPT-06 | 三种日期口径首屏预告（MC11-RPT-06） | `CARRY_OVER` | `PASS_WITH_RESERVATIONS` | 口径已登记于报表 metadata；**保留项**：首屏预告文案未落地 | 台账 `:221`；PMS3-REQ-001 `:184` |
| P3-RPT-07 | 经营报表扩展 | `DEFERRED` | `DEFERRED_TO_PMS3` | 无变化；指标定义前置，DEC-16 PMS3 扩展口径待决 | PMS3-REQ-001 `:185` |

#### 2.3.11 通知与待办（apps.core，5 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-NTF-01 | Notification（提示）与 WorkItem（待办）分离 | `INHERIT` | `DONE` | 无变化 | 台账 `:228` |
| P3-NTF-02 | WorkItem 仅待办提示（NS-E-02） | `INHERIT` | `DONE` | 无变化；禁止「标记完成即改预订状态」 | PMS3-REQ-001 `:192` |
| P3-NTF-03 | 待办来源扩展（MC11-NTF-06 的 5 项未派生） | `CARRY_OVER` | `NOT_STARTED` | 无变化 | 台账 `:230` |
| P3-NTF-04 | 通知链 E2E（MC11-NTF-07） | `INHERIT` | `DONE` | 无变化 | 台账 `:231` |
| P3-NTF-05 | 无独立「开始处理」动作（MC11-NTF-04） | `INHERIT` | `DONE` | 无变化 | PMS3-REQ-001 `:195` |

#### 2.3.12 平台、Outbox 与工程治理（apps.core + 跨模块，8 条）

| Req ID | 需求 | 上版处置码 | **最终状态** | 本版修订依据 | 证据 |
|---|---|---|---|---|---|
| P3-PLT-01 | 通用 Outbox + Django Worker | `CARRY_OVER` → `INHERIT` | `DONE` | **改判**：`OutboxEvent` 模型 + `apps/core/outbox.py` + `outbox_worker` 管理命令已落地（同事务入队 / `skip_locked` 批量租约 / 退避 / 死信 / 崩溃恢复 / HIGH_RISK 双端硬拒绝）；**保留项**：生产多 Worker 部署为 `PRODUCTION_PENDING`（PROD-04） | `apps/core/models.py:954`；`apps/core/outbox.py`；`apps/core/management/commands/outbox_worker.py`；台账 `:238,253` |
| P3-PLT-02 | OpenAPI 双快照逐字节守卫 | `INHERIT` | `DONE` | 无变化；现取 175 paths / 177 ops；**禁止第三份快照** | 现取 `docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json`；台账 `:241` |
| P3-PLT-03 | 凭据扫描门禁 + CI pipefail | `INHERIT` | `DONE` | 无变化 | 台账 `:244` |
| P3-PLT-04 | 双引擎测试纪律（SQLite + MySQL 权威） | `INHERIT` | `DONE` | 无变化；本轮双引擎 2229/2229 绑 `23fdd00` | `docs/acceptance/G14同基线最终补测报告_2026-11-27.md:16-17` |
| P3-PLT-05 | 结构化日志与异常处理统一 | `CARRY_OVER` → `INHERIT` | `DONE` | **改判**：JSON formatter + RequestID + 脱敏 + `hotel.api/core/billing` handler 已落地；**保留项**：生产监控平台未部署（PROD-07） | 台账 `:242`；`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:38` |
| P3-PLT-06 | 权限码逐项扩展（NS-E-05） | `WAITING_OWNER` → `INHERIT` | `DONE` | **改判**：5 个权限码已逐项裁定并落地（详见 §2.6）；**后续新增权限码仍须逐项审批（F-08），不得批量扩展** | `apps/identity/registry.py:492-758`（现取 27 码）；`docs/acceptance/NS-E-05逐项裁定落档与准入核验_2026-11-24.md` §1 |
| P3-PLT-07 | Redis 缓存边界 + 多 worker | `WAITING_EXTERNAL` | `WAITING_ENVIRONMENT` | 无变化；等待生产 Redis（PROD-04） | PMS3-REQ-001 `:207` |
| P3-PLT-08 | 性能与可观测性（指标/追踪） | `NEW` | `NOT_STARTED` | 无变化；**缺负责人批准记录**（见 §2.12） | PMS3-REQ-001 `:208` |

### 2.4 TASK-01～TASK-13 完成情况同步（任务书第 91–522 行）

| TASK | 任务书点名事项 | 交付物 | 本轮最终状态 | 证据 / 保留项 |
|---|---|---|---|---|
| **TASK-01** | 冻结当前验收基线 | DEL-01 基线冻结（由 G14 同基线报告承载） | `PASS_WITH_RESERVATIONS` | 被测树 `23fdd00`，工作区未提交项均为未跟踪证据目录；保留：G14 签署页留空未代签 |
| **TASK-02** | 双引擎全量回归 | DEL-02 | `PASS_WITH_RESERVATIONS` | SQLite 2229 OK（skipped=32）/ MySQL 2229 OK（skipped=5），exit 0，绑 `23fdd00`；保留 6 条 |
| **TASK-03** | E2E 56 项重新验收 | DEL-03 | `PASS_WITH_RESERVATIONS` | E2E 56 项失败 0，日志内记 `repo_head: 23fdd00`；保留：同 G14 6 条 |
| **TASK-04** | 九岗位权限矩阵重新验收 | DEL-04 | `PASS_WITH_RESERVATIONS` | 90 格（84 实测 + 6 按既有口径跳过）failures=0；保留：同 G14 6 条 |
| **TASK-05** | MySQL 并发仲裁重新验收 | DEL-05 | `PASS_WITH_RESERVATIONS` | 55 例 OK（历史 45 例口径未变，纯增量 10 例）；保留：同 G14 6 条 |
| **TASK-06** | G14 独立技术复核 | DEL-06 | `PASS_WITH_RESERVATIONS` | 独立技术复核结论 `PASS_WITH_RESERVATIONS`，保留 6 条（R1～R7 中的 7 条登记） |
| **TASK-07** | ARCH-02 旧路由治理收口 | DEL-07 + 实施报告 | `DONE` | 67 条路由处置矩阵（KEEP 20 / REDIRECT 15 / 410 32）；10 条 302 已实施 + 只读零写库断言；保留：R2 `folio/` 404→410 待裁定、R3 `booking/` 302 升级待业务确认 |
| **TASK-08** | ARCH-03 旧模板治理收口 | DEL-08 + 实施报告 | `DONE` | 37 个模板（KEEP 29 / MIGRATE 6 / DELETE 2）；链接迁移终态守卫已加；保留：R4 餐饮/停车/订单 3 模块 KEEP，PMS3.0 立项前需负责人确认 |
| **TASK-09** | ARCH-04 LegacyStaff 技术整改 | DEL-09 | `PASS_WITH_RESERVATIONS` | B1/H1/H2/H3/C13/C15/C16 已落地；C11（未映射旧员工数量）/C12（月薪工龄是否迁移）/C14（旧表保留策略）三项 `WAITING_OWNER`；MySQL 反向迁移本轮未本地复跑 → `WAITING_ENVIRONMENT` |
| **TASK-10** | ARCH-05 改密安全收口 | DEL-10 | `PASS_WITH_RESERVATIONS` | 7 核查点 6 `PASS` + 1 `WAITING_OWNER`（新旧密码相同是否禁止）；保留 8 条 |
| **TASK-11** | ARCH-11 审批历史与附件能力评估 | DEL-11（2 份） | `DONE`（材料） | 评估 7 项结论已出；裁定项一/二/三（审批事件表 / 附件能力 / 审批导出）**全部 `WAITING_OWNER`**，本轮未实施 |
| **TASK-12** | MySQL Append-only 触发器行为验证 | 环境限制报告 | `WAITING_ENVIRONMENT` | 本机 MySQL 无 SUPER + 开 binary log → 建触发器 1419；E2E F5 降级为 API 面断言；**本轮专项环境限制报告由并行子任务产出，尚未入库 → 未核实** |
| **TASK-13** | OWNER-05 数据保留期限与 Legal Hold | DEL-12 | `PASS_WITH_RESERVATIONS` | 分类与期限建议口径 `PASS`；Legal Hold 阻断 `PASS_WITH_RESERVATIONS`（4 项实现缺口）；生产真实清理 `PRODUCTION_PENDING`；期限与法律责任人 `WAITING_OWNER` |

### 2.5 T02-3 方案 B 完成状态同步

| 项 | 内容 | 状态 | 证据 |
|---|---|---|---|
| 裁定路径 | OWNER 2026-11-24「方案 B」逐项裁定 → 三项保留期专项权限码注册 → 真实申请→审批→执行链路 → 14 项交付验收 | `DONE` | `docs/acceptance/T02-3最终核验报告_2026-11-25.md`（基线 `80fa7c5`，双引擎 2018/2018 OK，CI 双绿） |
| 权限码 | `guests.retention_request` / `guests.retention_approve` / `guests.retention_execute` 已注册于 `SPECIAL_PERMISSIONS` 并与模块 `special` 元组双向一致 | `DONE` | `apps/identity/registry.py:119-124,512-532` |
| 审批动作 | `privacy.retention_execute` 审批动作已登记（request→`guests.retention_request`、approve→`guests.retention_approve`） | `DONE` | `apps/billing/approval_registry.py`；T02-3 最终核验报告 第 4 项 |
| 总体状态 | `APPROVED_WITH_CONDITIONS → DONE`（代码/测试/文档/CI 全闭环） | `DONE` | `docs/acceptance/T02-3最终核验报告_2026-11-25.md` 末段 |
| 生产真实批量清理 | 维持冻结 | `PRODUCTION_PENDING` | 同上 第 14 项；`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:85` |
| 期限与法律责任人 | 仍待负责人确认 | `WAITING_OWNER` | `docs/acceptance/OWNER-05-数据保留期限与LegalHold负责人裁定材料_2026-11-27.md` 结论 C-1 |

### 2.6 NS-E-05 具体权限裁定同步

| 权限码 | 裁定结果（2026-11-24） | 落地状态 | 证据 |
|---|---|---|---|
| `billing.payment_collect`（FIN-06） | `APPROVED` | 已注册并纳入 61 例权限回归矩阵 | `apps/identity/registry.py:245,733-734`；`docs/共享规范/PMS当前状态总览.md:54` |
| `guests.membership_status_manage`（MB-11） | `APPROVED` | 已注册 | `apps/identity/registry.py:184,188,748-749` |
| `guests.retention_request` | `APPROVED_WITH_CONDITIONS` | 已注册（T02-3 方案 B 落地，条件 1～4 满足） | `apps/identity/registry.py:122,513-514` |
| `guests.retention_approve` | `APPROVED_WITH_CONDITIONS` | 同上 | `apps/identity/registry.py:123,522-523` |
| `guests.retention_execute` | `APPROVED_WITH_CONDITIONS` | 同上 | `apps/identity/registry.py:124,531-532` |
| 后续扩展 | **不批量扩展，逐项审批（F-08）**；专项权限码总数 22 → **27** | `DONE`（NS-E-05 五码）/ 持续约束 | `apps/identity/registry.py:492-758`（现取 27 条 `codename`） |

> **口径澄清（随裁定落档）**：5 个 Django 权限码 ≠ 1 个审批动作码（`privacy.retention_execute` 不计入权限码数量）；方案批准 / 代码准入 / 生产启用是三个独立状态，不得混用。

### 2.7 ARCH-02～ARCH-05 当前状态同步

| 专项 | 当前状态 | 关键结论 | 证据 |
|---|---|---|---|
| **ARCH-02** 旧路由治理 | `DONE` | 67 条：KEEP 20 / REDIRECT 15 / 410 32（原汇总 13/39 已按逐行表订正）；10 条 302 已实施并补「零写库」断言；31 条已退役逐条 AST 复核属实，唯一例外 `folio/` 仍恒 404 | `docs/acceptance/ARCH-02与03退役实施报告_2026-11-27.md:76-78,82-87` |
| **ARCH-03** 旧模板治理 | `DONE` | 37 个：KEEP 29 / MIGRATE 6 / DELETE 2（原汇总 10/25 已订正）；6 个 MIGRATE 模板「链接已迁 → 路由已 302 → 模板已退役删除」三提交闭环；模板总数 37 → **29** | `docs/acceptance/ARCH-02与03退役实施报告_2026-11-27.md:93-94,62` |
| **ARCH-04** LegacyStaff | `PASS_WITH_RESERVATIONS` | B1/H1/H2/H3/C13/C15/C16 达成；C11、C12、C14 为 `WAITING_OWNER`；MySQL 反向迁移本轮未本地复跑 → `WAITING_ENVIRONMENT` | `docs/acceptance/ARCH-04技术整改与负责人裁定材料_2026-11-27.md` 结论 1–12 |
| **ARCH-05** 改密安全 | `PASS_WITH_RESERVATIONS` | 7 核查点：6 `PASS` + 1 `WAITING_OWNER`；另 8 条保留意见（`WAITING_OWNER` 2 / `WAITING_ENVIRONMENT` 1 / `PRODUCTION_PENDING` 1 / `DEFERRED_TO_PMS3` 2 / `NOT_STARTED` 2），**全部不阻塞 PMS3.0 本地开发** | `docs/acceptance/ARCH-05密码修改安全最终验收报告_2026-11-27.md` §一 |
| **ARCH-11** 审批历史与附件 | `WAITING_OWNER` | 评估结论：附件与文件存储能力在 PMS2 中**完全不存在**；三项裁定（审批事件表 / 附件能力 / 审批导出）均 `WAITING_OWNER` | `docs/acceptance/ARCH-11审批历史附件能力评估_2026-11-27.md` 结论 3/5/6；`docs/acceptance/ARCH-11负责人裁定材料_2026-11-27.md` §〇 |

### 2.8 OWNER-05、OWNER-07 等待负责人事项（保留为 `WAITING_OWNER`）

| 编号 | 事项 | 状态 | 已备好的证据 | 实施边界 |
|---|---|---|---|---|
| **OWNER-05** | 数据保留期限值与法律责任人（六类数据 × 建议期限）；另含审计留存口径冲突（审计 10 年 vs 「待确认、不设固定年限」） | `WAITING_OWNER` | 六类数据真实表名/字段/行号已现取；期限完全数据驱动、代码零硬编码；Legal Hold 在到期清理路径上确实能阻断；全仓不存在「到期即自动清理」触发器 | **未写任何期限值、未执行任何清理**；生产真实批量匿名化与物理删除继续冻结（`PRODUCTION_PENDING`）。证据：`docs/acceptance/OWNER-05-数据保留期限与LegalHold负责人裁定材料_2026-11-27.md` 结论 C-1～C-10 |
| **OWNER-07** | 需求状态口径最终选择（任务书 12 态 vs 台账 9 态 vs 现行 8 词建议） | `WAITING_OWNER` | 已量化 8 组并存状态词集合、并集 34 个状态串；同名仅 4 个；实证冲突 10 条；已给出 4 套 → 8 词的完整可追溯映射，历史状态一条不删；已出 16 份待统一修改文档清单 | **未实际修改任何既有文档**，只输出清单与逐文件改动点。证据：`docs/acceptance/OWNER-07-需求状态体系统一裁定材料_2026-11-27.md` 结论 1～10 |

### 2.9 生产依赖事项登记（PROD-01～PROD-08，8/8 保留 `PRODUCTION_PENDING`）

| 编号 | 事项 | 状态 | 解除条件 | 证据 |
|---|---|---|---|---|
| PROD-01 | 生产服务器与部署 | `WAITING_FOR_SERVER` | 服务器交付并就绪，生产环境可启动 | `docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:32` |
| PROD-02 | 生产环境密钥托管与轮换（含 PII 独立密钥） | `PRODUCTION_PENDING` | 密钥平台到位 + 轮换演练通过 | 同上 `:33` |
| PROD-03 | 真实生产备份恢复演练 | `WAITING_FOR_DBA` | 满足 RPO 15min / RTO 4h，可恢复到独立实例且核对一致 | 同上 `:34` |
| PROD-04 | 生产 Redis 多 worker | `PRODUCTION_PENDING` | 多 worker 下锁/缓存/限流行为正确，无跨进程重复消费 | 同上 `:35` |
| PROD-05 | 真实支付渠道/银行/供应商联调 | `WAITING_FOR_PROVIDER` | 真实小额交易成功；回调签名验证通过；退款可执行；对账一致 | 同上 `:36` |
| PROD-06 | 生产数据库特权操作（V13-01 孪生账户取证） | `WAITING_FOR_DBA` | 孪生账户清单可核实；`hotelhotel@localhost` 治理完成 | 同上 `:37` |
| PROD-07 | 生产监控告警与灾备演练 | `PRODUCTION_PENDING` | 关键路径有指标与告警；灾备切换演练通过 | 同上 `:38` |
| PROD-08 | MySQL 生产连接容量与池化验证（V13-04） | `PRODUCTION_PENDING` | `CONN_MAX_AGE`/`CONN_HEALTH_CHECKS` 正确；并发连接不超上限 | 同上 `:39` |

> **状态保护规则**（不得违反）：① 不得改标为「已完成 / PASS」；② 本地 MySQL ≠ 生产数据库验收、本地备份 ≠ 生产备份恢复演练、mock 支付 ≠ 真实渠道、单进程本地 Redis ≠ 生产多 worker；③ 无法律依据与批准记录不得删除真实数据；④ 8 项**不进入** PMS2.0/PMS3.0 本地开发的完成率与门禁判定。聚合门禁 **GATE-PROD 未开启**且**不得部分通过**（`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:47,83-88`）。

### 2.10 M5-4 生产归档与本地代码归档（分别登记）

| 登记项 | 范围 | 状态 | 事实与证据 |
|---|---|---|---|
| **M5-4-A 本地代码归档层** | client/vip 七表 spec + 密文/盲索引 transform + 迁移 0037/0039 + 校验和 + 不可变 + 回归测试 | `PASS_WITH_RESERVATIONS` | 七表 spec 与 `legacy_archive.py`、迁移 0037 在列数/列序/Fernet+带密钥 HMAC/明文不入归档四点上完全一致；保留：LegalHold 归档批次级未落地、查询等价性未验、财务抽凭待生产；**旧文档「归档代码尚未实现」结论自此失效**。证据：`docs/acceptance/M5-4-归档实施与生产边界修订报告_2026-11-27.md` 结论 1/3/7 |
| **M5-4-B 生产真实数据归档层** | 生产环境存量盘点 → 备份恢复演练 → L4 删除审批 → 财务 D5 门禁归档 → D6 读路径切换 → 物理删除 | `PRODUCTION_PENDING` + `WAITING_OWNER` | **八项前置 0/8 满足**；负责人对八项前置的最终确认与责任归属属裁定事项；**不恢复姓名揭示、不重新提交相同权限方案**（与 OWNER-06 `REJECTED` 直接冲突的 5 处已登记）。证据：`docs/acceptance/M5-4-八项前置条件完整清单_2026-11-27.md` 结论 5/6；`docs/acceptance/M5-4-归档实施与生产边界修订报告_2026-11-27.md` 结论 5 |

> **八项前置的唯一权威出处** = GOV-026《架构治理模块五批次 M5-4 归档 spec 设计与退役移交_2026-11-19.md》§2.6，行 109–118，编号 1–8 连续无缺号。下游文档另有 9 行（`1,2,3,3b,4,5,6,7,8`）、`M5-4-P1..P7`、压缩 6 项三种自造编号，均为**旁证口径错误**，以 GOV-026 为准。

### 2.11 已移交 PMS3 的旧页面与功能（不得重复登记为 PMS2 未完成开发）

| 承接项 | 原 PMS2 编号 | 裁定依据 | 本版登记 |
|---|---|---|---|
| `SchedulerRun` 调度运行记录 | P3-NA-03 | NS-E-04 `APPROVED_PMS3_HANDOFF`（`6b6519e` 落档） | `DEFERRED_TO_PMS3` |
| 调度停摆探测 + 失败告警 | P3-NA-04 | 同上 | `DEFERRED_TO_PMS3` |
| 自动夜审 + 忘记夜审提醒 | P3-NA-05 | 同上 | `DEFERRED_TO_PMS3` |
| 链式哈希 + 外部锚定 | P3-AUD-03 | NS-E-03 `APPROVED_PMS3_HANDOFF` | `DEFERRED_TO_PMS3` |
| 差异扫描异步化 | P3-INV-04 | 已批准移交评估 | `DEFERRED_TO_PMS3` |
| 经营报表扩展 | P3-RPT-07 | 已批准移交 PMS3 批次 | `DEFERRED_TO_PMS3` |
| 跨物业调岗 | P3-EMP-04 | F-11 多物业冻结 | `DEFERRED_TO_PMS3` |

> **硬边界（不得因移交而放松）**：① 移交 PMS3 **不计入** PMS2.0 已实现功能；② P3-NA-03/04、P3-AUD-03 在 PMS3 内承接时**不得做伪链式哈希、不得为补哈希重写历史审计**；③ P3-EMP-04 / P3-EMP-09 在负责人批准前**多物业切换继续冻结**。
> **已在本矩阵中按「PMS2 未完成开发」重复登记的复核结果**：无。ARCH-02/03 退役的 31 条 410 路由、15 条 302 路由、8 个模板属 **PMS2.0 内已完成的收口**，已按 `P3-RES-07` / `P3-RES-08` / `P3-RES-09` 的实际落地状态登记，**未**另列为 PMS2 未完成开发。

### 2.12 PMS3 新增需求登记（来源 / 业务目的 / 优先级 / 批准记录）

| Req ID | 需求 | 来源 | 业务目的 | 优先级 | 批准记录 | 最终状态 |
|---|---|---|---|---|---|---|
| P3-RES-10 | 特殊预订状态扩展 | V2.3 §5.5 当前缺口（特殊预订状态扩展 P2） | 业务灵活：支持非标准预订状态 | L（跨模型/状态机） | **缺失**——矩阵「需负责人批准 = 是」，未检索到负责人书面批准记录 | `NOT_STARTED` |
| P3-FIN-10 | 支付/退款/账本状态机扩展 | V2.3 §24.3 目标状态机（PaymentIntent/ProviderTransaction 边界约束仍有效） | 异常健壮：覆盖退款失败/补偿等异常路径 | L（状态机变更） | **缺失**——同上 | `NOT_STARTED` |
| P3-PLT-08 | 性能与可观测性（指标/追踪） | V2.3 §15.3 INF-04/05、§25.5 监控指标、DEC-21 | 运营质量：关键路径可观测 | L（依赖部署架构） | **缺失**——同上 | `NOT_STARTED` |

> **规则执行说明**：3 条新增需求均具备来源、业务目的与优先级，**但均无负责人批准记录**；按任务书 TASK-22 第 9 项，一律标 `NOT_STARTED` 并注明缺批准记录，**不得**在 PMS3 立项材料中表述为「已立项」或「已批准」。
> **复核方法**：在 `docs/PMS2.0_当前版本/00_项目基线与决策/` 的负责人裁定记录与 `docs/acceptance/OWNER核对结果报告_2026-11-23.md` 中按需求名与 V2.3 章节号检索，均未命中对这三条的书面批准。

### 2.13 状态分布统计（逐行清点，可机械复核）

**最终状态（8 态词表）**

| 状态 | 条目数 |
|---|---:|
| `DONE` | 46 |
| `PASS_WITH_RESERVATIONS` | 8 |
| `NOT_STARTED` | 19 |
| `DEFERRED_TO_PMS3` | 7 |
| `WAITING_OWNER` | 5 |
| `WAITING_ENVIRONMENT` | 4 |
| `PASS` | 0 |
| `FAIL` | 0 |
| **合计** | **89** |

**PMS3 处置码（8 态）**

| 处置码 | 上版（2026-11-22） | 本版 | 差异 |
|---|---:|---:|---|
| `INHERIT` | 39 | **47** | +8 |
| `CARRY_OVER` | 29 | **24** | −5 |
| `DEFERRED` | 6 | **6** | 0 |
| `WAITING_OWNER` | 6 | **5** | −1 |
| `WAITING_EXTERNAL` | 5 | **4** | −1 |
| `NEW` | 3 | **3** | 0 |
| `EXTEND` | 1 | **0** | −1 |
| `DEPRECATED` | 0 | **0** | 0 |
| **合计** | **89** | **89** | — |

**复核方法**：对本文件 §2.3.1～§2.3.12 共 12 张表的「最终状态」列与「上版处置码」列逐行清点，合计须 = 89；Req ID 唯一性沿用 PMS3-REQ-001 §16 复核方式。

---

## 三、变更摘要（相对 2026-11-26 版改了哪些条目、为什么）

### 3.1 处置码改判 9 条

| # | Req ID | 上版 → 本版 | 原因 | 证据 |
|---|---|---|---|---|
| 1 | P3-EMP-07 | `CARRY_OVER` → `INHERIT` | `LegacyStaffMapping` 持久映射台账 + 迁移 `0016` 回填已落地，映射能力可在 PMS3 原样继承 | `apps/identity/models.py:594-697`；ARCH-04 裁定材料 结论 1 |
| 2 | P3-G2-04 | `CARRY_OVER` → `INHERIT` | `RetentionPolicy`/`LegalHold`/`RetentionExecution` + `retention.py` + T02-3 方案 B 14 项全闭环，本地代码层已具备；生产清理与期限另立登记（§2.8/§2.9/§2.10） | `apps/guests/models.py:487,579,691`；T02-3 最终核验报告 |
| 3 | P3-RES-08 | `CARRY_OVER` → `INHERIT` | 旧 `/walk-ins` 端点已不在路由表，双端点并存事实消失 | `hotel/urls.py:145` |
| 4 | P3-RES-09 | `WAITING_EXTERNAL` → `INHERIT` | `checkin/legacy/` 已 302 到 `/checkin/` 并补只读断言；生产流量观察转 DEP-PROD-A | `hotel/urls.py:137`；ARCH-02/03 实施报告 `:40` |
| 5 | P3-RPT-04 | `CARRY_OVER` → `INHERIT` | 导出上限 5000 + 截断标记已落地 | `apps/api/report_export.py:35,78-79` |
| 6 | P3-PLT-01 | `CARRY_OVER` → `INHERIT` | `OutboxEvent` + `outbox.py` + `outbox_worker` 已落地；生产多 Worker 转 PROD-04 | `apps/core/models.py:954`；`apps/core/management/commands/outbox_worker.py` |
| 7 | P3-PLT-05 | `CARRY_OVER` → `INHERIT` | 结构化 JSON 日志 + RequestID + 脱敏已落地；监控平台转 PROD-07 | 台账 `:242` |
| 8 | P3-PLT-06 | `WAITING_OWNER` → `INHERIT` | NS-E-05 五个权限码已逐项裁定并落地（2 `APPROVED` + 3 `APPROVED_WITH_CONDITIONS`） | `apps/identity/registry.py:492-758`；NS-E-05 逐项裁定落档报告 |
| 9 | P3-EMP-05 | `EXTEND` → `CARRY_OVER` | `EXTEND` 以「PMS2 已完成」为前提；现取离职链路零 WorkItem 转交命中，能力未实现，应按未完成继承 | `apps/identity/services.py:528-577`（现取零命中） |

### 3.2 状态升级 / 降级（不改处置码，只改最终状态词）

| # | Req ID | 上版 PMS2 状态 → 本版最终状态 | 原因 |
|---|---|---|---|
| 1 | P3-G2-03 | `NOT_IMPLEMENTED` → `PASS_WITH_RESERVATIONS` | 阶段 1 双写（Fernet + HMAC）已落地；阶段 2 未做 |
| 2 | P3-G2-05 | `PARTIAL` → `PASS_WITH_RESERVATIONS` | 有实现、缺独立动态验收 |
| 3 | P3-G2-06 | `DESIGN_ONLY` → `PASS_WITH_RESERVATIONS` | 受控合并端点已落地；候选治理 UI 与双人复核未落地 |
| 4 | P3-G2-08 | `NOT_IMPLEMENTED` → `PASS_WITH_RESERVATIONS` | 本地代码归档层已落地；生产归档层另立 |
| 5 | P3-MB-03 | `NOT_IMPLEMENTED` → `PASS_WITH_RESERVATIONS` | 唯一约束 + 写入前拒绝已落地；人工复核流程未落地 |
| 6 | P3-RES-07 | `TODO` → `PASS_WITH_RESERVATIONS` | 旧事件无消费者已核验 + 旧路由/模板面收口；`folio/` 与 `booking/` 两项待确认 |
| 7 | P3-RPT-06 | `PARTIAL` → `PASS_WITH_RESERVATIONS` | 口径已登记；首屏预告未落地 |
| 8 | P3-AUD-04 | `NOT_IMPLEMENTED` → `NOT_STARTED` | 审计导出只设上限 10000，无截断标记（保持未开始，附证据） |

### 3.3 未改动条目说明

其余 **72 条**的处置码与最终状态均**保持上版结论**，包括：6 条 `DEFERRED_TO_PMS3`（§2.11）、3 条 `NOT_STARTED` 新增需求（§2.12）、5 条 `WAITING_OWNER`、4 条 `WAITING_ENVIRONMENT`、19 条 `NOT_STARTED`（含 P3-EMP-06、P3-INV-05、P3-MB-02/06/07、P3-FIN-04/07/09、P3-NA-06/07、P3-HK-04/05、P3-RPT-03、P3-NTF-03）。

### 3.4 「14 项继承 + 7 项冻结」约束的同步

2026-11-26 版矩阵 §一（14 项继承）与 §二（7 项冻结）在本版**逐条复核仍然成立**，其中两处数字随本轮现取更新：

| 项 | 上版表述 | 本版现取 | 出处 |
|---|---|---|---|
| 第 8 项 九岗位 RBAC（22 个专项权限码） | 22 个 | **27 个**（NS-E-05 新增 5 个） | `apps/identity/registry.py:492-758` |
| 第 3 项 Django Template + 原生 JS（`templates/` 37 个） | 37 个 | **29 个**（ARCH-03 退役 8 个） | 现取 `templates/` 递归计数；ARCH-02/03 实施报告 `:62` |

### 3.5 证据索引（文件路径 + 行号 / 命令 + 结果）

| 证据项 | 路径 / 命令 | 关键结果 |
|---|---|---|
| 代码基线 | `git -C F:\ZCode\pms2-final-docs rev-parse HEAD` | `0fa7086ea3ecc514dab4a521e860102b533b9a82` |
| 远端同点核对 | `git rev-list --left-right --count origin/main...HEAD` | `0 0` |
| 双引擎全量 | `docs/acceptance/evidence/g14-final-20261127/{sqlite,mysql}-23fdd00.log` | SQLite 2229 OK（skipped=32，502.029s）／MySQL 2229 OK（skipped=5，601.681s），绑 `23fdd00` |
| MySQL 并发仲裁 | `docs/acceptance/evidence/g14-final-20261127/mysql-concurrency-23fdd00.log` | 55 例 OK（12.320s） |
| E2E 全链路 | `docs/acceptance/evidence/g14-final-20261127/e2e-56-23fdd00.log` | 56 项失败 0，日志内记 `repo_head: 23fdd00` |
| 九岗位矩阵 | `docs/acceptance/evidence/task06-nine-role-20261127-23fdd00/report.json` | 90 格，`passed=true` |
| OpenAPI 契约 | 现取 `docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json` | OpenAPI 3.1.0，**175 paths / 177 operations** |
| 专项权限码 | 现取 `apps/identity/registry.py:492-758` | `SPECIAL_PERMISSIONS` **27** 条 `codename`（上版 22，NS-E-05 新增 5） |
| 模板总数 | 现取 `templates/` 递归计数 | **29** 个 `.html`（上版 37，ARCH-03 退役 8） |
| Django 版本 | `python -c "import django;print(django.get_version())"` | `4.1` |
| 需求条目清点 | 逐行清点本文件 §2.3.1～§2.3.12「最终状态」列 | **89 行 / 89 唯一 Req ID**；分布 DONE 46 / PWR 8 / NS 19 / D2P3 7 / WO 5 / WE 4 |
| 引用路径复验 | 脚本复验两份文件内全部 `路径:行号` 引用 | **缺失/越界 = 0** |
| 文档门禁 | `python scripts/docs_link_check.py check` | `新增违规数=0`，exit 0 |

---

## 四、未完成与待裁定事项

| # | 事项 | 状态 | 责任归属 | 复核方法 |
|---|---|---|---|---|
| 1 | G14 最终签署（签署页留空未代签） | `WAITING_OWNER` | 负责人本人 | `docs/PMS2.0_当前版本/06_验收与发布/96-g14-signoff_2026-11-10.md` |
| 2 | G14 保留意见 6 条（R1～R7，含 `folio/` 404→410、餐饮/停车/订单 3 模块归属、`requirements.txt` 缺 `playwright`/`requests`、隔离库 `test_hotel` 与 Django 测试库撞名） | `PASS_WITH_RESERVATIONS` | 负责人 + 运维（DBA 授权） | `docs/acceptance/G14同基线最终补测报告_2026-11-27.md:152-164` |
| 3 | ARCH-04 C11 / C12 / C14 三项 | `WAITING_OWNER` | 负责人 | `docs/acceptance/ARCH-04技术整改与负责人裁定材料_2026-11-27.md` 结论 9/10/11 |
| 4 | ARCH-11 三项裁定（审批事件表 / 附件能力 / 审批导出） | `WAITING_OWNER` | 负责人 | `docs/acceptance/ARCH-11负责人裁定材料_2026-11-27.md` §〇 |
| 5 | Q-03 薪酬只读权限码是否独立建立（建议独立，不复用 `identity.view_staffprofile`） | `WAITING_OWNER` | 负责人 | `docs/acceptance/Q-03财务薪酬只读权限裁定材料_2026-11-27.md` 结论 7 |
| 6 | ARCH-05 核查点 7（新旧密码是否禁止相同） | `WAITING_OWNER` | 负责人 | `docs/acceptance/ARCH-05密码修改安全最终验收报告_2026-11-27.md` §一 |
| 7 | OWNER-05 保留期限值与法律责任人；审计留存口径冲突 | `WAITING_OWNER` | 负责人 + 法律 | `docs/acceptance/OWNER-05-数据保留期限与LegalHold负责人裁定材料_2026-11-27.md` 结论 C-1/C-8 |
| 8 | OWNER-07 状态口径最终选择（8 词建议 vs 12 态 vs 9 态） | `WAITING_OWNER` | 负责人 | `docs/acceptance/OWNER-07-需求状态体系统一裁定材料_2026-11-27.md` 结论 7 |
| 9 | PROD-01～PROD-08 全部 8 项 | `PRODUCTION_PENDING` | 负责人（采购）+ 运维 + DBA + 商务 | `docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md` §1 |
| 10 | M5-4 八项前置（0/8 满足）+ 八项前置的最终确认与责任归属 | `PRODUCTION_PENDING` + `WAITING_OWNER` | 负责人 + DBA | `docs/acceptance/M5-4-八项前置条件完整清单_2026-11-27.md` 结论 5/6 |
| 11 | TASK-12 MySQL append-only 触发器行为级验证 | `WAITING_ENVIRONMENT` | DBA（需 SUPER 权限独立 MySQL） | `AGENTS.md:43`；G14 报告 R5 |
| 12 | PMS3 新增 3 条需求的负责人批准记录 | `NOT_STARTED` | 负责人 | §2.12 |
| 13 | 本矩阵 19 条 `NOT_STARTED` 中不阻塞本地开发的部分 | `NOT_STARTED` | PMS3 承接（P3-EMP-05/06/07、INV-05、MB-02/06/07、FIN-04/07/09、NA-06/07、HK-04/05、RPT-03、NTF-03） | §2.3 |

---

## 五、下一步责任归属

| 项 | 责任方 | 动作 | 前置 |
|---|---|---|---|
| 本矩阵交接 PMS3.0 开发团队 | PMS3 开发团队 | 以 §2.3 的「最终状态」列作为 PMS3 需求基线；以「PMS3 处置码」列作为批次与范围划分依据 | 本报告入库 |
| G14 最终签署 | 负责人本人 | 完成签署 | 保留意见处置决策 |
| 5 项 `WAITING_OWNER` + 3 项裁定 | 负责人 | 逐项批复 | 裁定材料已备齐（§2.6/§2.7/§2.8/§四） |
| 8 项 `PRODUCTION_PENDING` | 运维 / DBA / 商务 | 资源到位后按「解除条件」逐项验收 | 生产环境（PROD-01） |
| M5-4 生产归档 | 负责人 + DBA | 八项前置逐项确认；**归档实施一律不得启动**，**不得恢复姓名揭示** | 八项前置全部满足 + L4 审批 |
| 状态口径统一 | 负责人 | 裁定 8 词 / 12 态 / 9 态后，按 OWNER-07 材料 §2.8 的 16 份文档清单执行统一修改 | OWNER-07 裁定 |
| 文档基线一致性 | 主控会话 | 更新 `docs/共享规范/PMS当前状态总览.md` 的基线、测试数、接口数、权限码数、模板数（本报告 §2.2 已列差异） | 主控会话统一处理 |

---

> **本报告不构成生产上线许可，不替代 G14 负责人签署与 MC11 独立验收；也不代替负责人对 §四 任何 `WAITING_OWNER` 事项的裁定。**
