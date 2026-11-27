# PMS3.0 本地开发准入报告（2026-11-27）

> 文档编号：PMS2-PMS3-ACCESS-001 ｜ 类型：ACCEPTANCE ｜ 状态：ACTIVE ｜ 版本域：PMS3.0 ｜ 业务模块：版本准入
> 任务编号：TASK-23 ｜ 交付物：DEL-19
> 执行日期：2026-11-27 ｜ 代码基线：main HEAD `0fa7086`（`git rev-parse HEAD` 现取；`git rev-list --left-right --count origin/main...HEAD` = `0 0`）
> 上游矩阵：`docs/acceptance/PMS3.0-需求继承矩阵最终修订版_2026-11-27.md`（PMS2-PMS3-INHERIT-002，DEL-18）
> 上版准入依据：`docs/acceptance/PMS3.0需求继承及冻结矩阵_2026-11-26.md`（PMS2-PMS3-INHERIT-001，绑 `0ca1ce4`）＋ `docs/acceptance/PMS2.0最终收口与PMS3.0启动准备报告_2026-11-26.md`（PMS2-FINAL-CLOSEOUT-001，绑 `0ca1ce4`）
> 任务依据：《PMS 2.0 最终验收补证、遗留事项整改与 PMS 3.0 启动准备任务书》PMS2-FINAL-ACCEPTANCE-REMEDIATION-003 V1.0 §TASK-23（任务书原文第 858–885 行）＋ §第十五章 C「PMS3.0 本地开发准入」（任务书原文第 1053–1064 行）
> 执行方式：全仓只读取证（Read / `grep` 实跑 / `git rev-parse` / `Test-Path`）+ 本轮 11 份已入库交付物结论复用。**未改动任何 `.py`、模板、配置、测试，未改动任何历史文档。**
> **执行人纪律**：本报告**不代签** G14、**不代替**负责人裁定任何 `WAITING_OWNER` 事项、**不把**生产事项改标为已完成、**不把**「移交 PMS3」计入 PMS2 已实现功能。

---

## 一、结论摘要

| # | 结论 | 状态 |
|---|---|---|
| 1 | **PMS3.0 本地开发准入通过**，附 6 条保留项；**生产环境尚未就绪不阻塞本地开发**，正式生产上线必须另行验收 | `PASS_WITH_RESERVATIONS` |
| 2 | **PMS2 已完成且可继承**：46 条需求最终状态 `DONE`（+ 8 条 `PASS_WITH_RESERVATIONS` 的已达成部分），覆盖员工/RBAC、Guest 360、PII、库存、会员、预订、财务、夜审、维修、报表、通知、平台 Outbox 等 12 个模块 | `DONE` |
| 3 | **PMS2 有条件完成**：8 条 `PASS_WITH_RESERVATIONS`（G2-03/04/05/06/08、MB-03、RES-07、RPT-06）——本地能力达成，保留项分别为阶段 2 切换、期限裁定、人工复核流程、候选治理 UI、生产归档层、旧入口两项待确认 | `PASS_WITH_RESERVATIONS` |
| 4 | **PMS2 仍有遗留但不阻塞本地开发**：19 条 `NOT_STARTED` + 7 条 `DEFERRED_TO_PMS3`；全部由 PMS3 承接或待裁定，**不进入 PMS3.0 启动门禁** | `NOT_STARTED` |
| 5 | **PMS2 未完成且会阻塞 PMS3 的模块：无**（P0 = 0、P1 = 0）；仅存在 3 项**受限批次约束**（归档实施、多物业、姓名揭示），见 §二.4 | `PASS` |
| 6 | **生产依赖事项**：PROD-01～PROD-08 **8/8 保留 `PRODUCTION_PENDING` 类标记**，聚合门禁 GATE-PROD 未开启且不得部分通过；M5-4 八项前置 **0/8 满足** | `PRODUCTION_PENDING` |
| 7 | **负责人未裁定事项**：8 项 `WAITING_OWNER`（G14 签署、ARCH-04 C11/C12/C14、ARCH-11 三项裁定、Q-03 薪酬权限码、ARCH-05 核查点 7、OWNER-05、OWNER-07）；**均不导致 PMS3.0 核心架构返工** | `WAITING_OWNER` |
| 8 | **PMS3 新增开发范围**：PMS3-REQ-001 §12.1 第一批 8 项边界 + 3 条新增需求（**均缺负责人批准记录，一律 `NOT_STARTED`**） | `NOT_STARTED` |
| 9 | **PMS3 不允许重复开发的能力**：12 条禁止重复建设清单 + 4 个唯一权威事实源 + 2 条硬边界 | `PASS` |
| 10 | **9 项技术约束逐条核对全部满足**（模块化单体 / Django 后端 / Django Template + 原生 JS / MySQL 生产事实库 / MySQL Outbox + Django Worker / Session 身份体系 / 既有 RBAC 与权限专项码治理 / 不引入已冻结技术栈 / 多物业切换未获批准前继续冻结） | `PASS` |
| 11 | **两条硬边界已写入并生效**：**姓名揭示不得恢复**；**多物业切换未获批准前继续冻结** | `PASS` |
| 12 | **基线口径差异已声明**：总览记基线 `6d7d9ce` / 全量 2177（绑 `5cad6b7`）；本轮现取 HEAD `0fa7086`，双引擎全量 **2229/2229**（绑 `23fdd00`）。以现取为准，总览待主控会话统一更新 | `PASS` |

---

## 二、正文

### 2.1 PMS2 已完成且可继承的模块（`DONE`，46 条）

| 模块 | 可继承能力 | 证据 | 继承方式 |
|---|---|---|---|
| 员工与 RBAC | 员工生命周期全链；九岗位权限矩阵 + `AuthorizationService`；平台身份与员工身份分域；旧员工批量迁移映射台账（`LegacyStaffMapping` + 迁移 `0016`）；员工链浏览器证据 | `apps/identity/models.py:594-697`；`apps/identity/services.py:522-577`；`docs/acceptance/G14同基线最终补测报告_2026-11-27.md:20` | 原样继承，**禁止重建** |
| Guest 360 / PII | 手机号密文 + HMAC 索引 + 受控揭示；证件密文 + `doc_no_hash` 物业级唯一；合并软归档 | `apps/guests/services.py`；`apps/guests/models.py:413` | 原样继承，**禁止第二套 PII 加密体系** |
| 房态 / 库存 / 排房 | 房态三维分离 + 可售派生；`RoomAssignmentConflict` 唯一键仲裁 + 并发；差异工作流五步 + 冲突只读端点；`rate_adjust` 归属口径 | MySQL 并发仲裁 55 例 OK（`docs/acceptance/G14同基线最终补测报告_2026-11-27.md:18`） | 原样继承，**禁止重复开发原始修复** |
| 会员 | 会员创建/等级/暂停/恢复 + `expires_at` 全链；会员来源 `source` 透出 | 台账 `docs/共享规范/PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md:144` | 原样继承 |
| 预订与入住 | 公开预订 + PENDING Hold + 过期释放；排房/入住/退房/续住/换房/取消；Day-use 建单/政策价/转过夜；提前离店罚金（DEC-01）；押金 30%（DEC-03）；负余额退款（DEC-02）；Walk-in 旧入口已收敛；`/checkin/legacy/` 已 302 退役 | `hotel/urls.py:137,145`；`apps/api/report_export.py:35` | 原样继承；**DEC-01/02/03 不得擅改** |
| 财务与 Folio | 统一账本写入口 + 不可变凭证；退款申请/审批/执行三方分离；收银班次现金管理 | PMS3-REQ-001 `:131-133` | 原样继承，**禁止第二套账本** |
| 营业日与夜审 | 营业日 + 夜审预览/执行/关账；重开双人审批 + Break-glass | 台账 `:185` | 原样继承 |
| 客房与维修 | 清洁任务状态机 + 房态同事务；维修全链 + SoD；工单取消不自动恢复可售（NS-E-01） | PMS3-REQ-001 `:158-160` | 原样继承，**SoD 不可放宽** |
| 审计与合规 | 追加式审计 + 三层防护；受控导出 + 凭证 + 脱敏 | 台账 `:197` | 原样继承，**审计只追加** |
| 报表 | 7 类报表 + 口径 metadata + CSRF 导出；源数据独立复算工具；导出截断标记（5000 行 + `# truncated:` 首行）；housekeeping 报表依赖审计留存 | `apps/api/report_export.py:35,78-79` | 原样继承 |
| 通知与待办 | Notification / WorkItem 分离；WorkItem 仅待办提示（NS-E-02）；通知链 E2E；无独立「开始处理」动作 | PMS3-REQ-001 `:191-195` | 原样继承 |
| 平台 / Outbox / 工程治理 | `OutboxEvent` + `apps/core/outbox.py` + `outbox_worker` 管理命令（同事务入队 / `skip_locked` 批量租约 / 退避 / 死信 / 崩溃恢复 / HIGH_RISK 双端硬拒绝）；OpenAPI 双快照逐字节守卫（现取 175 paths / 177 ops）；凭据扫描门禁 + CI pipefail；双引擎测试纪律（现取 2229/2229）；结构化 JSON 日志 + RequestID + 脱敏；NS-E-05 五个专项权限码逐项裁定落地（专项码总数 27） | `apps/core/models.py:954`；`apps/core/management/commands/outbox_worker.py`；`apps/identity/registry.py:492-758` | 原样继承，**不得新建第二套事件总线 / 不得第三份 OpenAPI 快照 / 权限码不得批量扩展** |

### 2.2 PMS2 有条件完成的模块（`PASS_WITH_RESERVATIONS`，8 条）

| 条目 | 已达成部分 | 保留项（未完成） | 证据 |
|---|---|---|---|
| P3-G2-03 Guest 姓名加密 | 阶段 1 双写（Fernet 密文 + HMAC 盲索引）已落地 | 阶段 2 明文列退役与分批迁移未做；`reveal_guest_name` 已按 OWNER-06 治理整体移除 | `apps/guests/name_pii.py:1-15,73-74` |
| P3-G2-04 数据保留 / Legal Hold | 三模型 + `retention.py` + 真实申请→审批→执行链路 + T02-3 方案 B 14 项全闭环 | 期限值与法律责任人待裁定；生产真实清理冻结 | `apps/guests/models.py:487,579,691`；T02-3 最终核验报告 |
| P3-G2-05 客人时间线聚合 | 时间线聚合接口已在位 | 缺独立动态验收 | 台账 `:124` |
| P3-G2-06 重复档案候选治理 | 受控合并端点（reason + 审计 + 软归档）已落地 | 候选列表 / 影响面预览 / 双人复核与撤销未落地；**禁止自动合并** | `apps/api/guest_center.py:912-954` |
| P3-G2-08 Client/Vip 归档 | **本地代码归档层**七表 spec + 密文 transform + 迁移 0037/0039 + 校验和 + 不可变 + 回归 | LegalHold 归档批次级未落地；查询等价性未验；财务抽凭待生产；**生产归档层另立登记** | `apps/billing/legacy_archive.py:74-109,199-225` |
| P3-MB-03 `member_no` 冲突 | 物业级唯一约束 + 写入前重复拒绝 | 「人工复核流程」未落地 | `apps/guests/models.py:413`；`apps/guests/services.py:973-974` |
| P3-RES-07 模块③旧事件链/状态机收口 | 旧事件无消费者已核验（TASK-06 `PASS`）；旧路由/旧模板面已收口并加「零写库」断言 | `folio/` 仍恒 404（改 410 待裁定）；`booking/` 302 升级永久重定向待业务确认 | `docs/acceptance/TASK06-09验收复核报告_2026-11-24.md`；`docs/acceptance/ARCH-02与03退役实施报告_2026-11-27.md:52` |
| P3-RPT-06 三种日期口径首屏预告 | 口径已登记于报表 metadata | 首屏预告文案未落地 | 台账 `:221` |

### 2.3 PMS2 仍有遗留但不阻塞本地开发的模块（`NOT_STARTED` 19 条 + `DEFERRED_TO_PMS3` 7 条）

**（a）PMS3 承接的未完成开发（19 条 `NOT_STARTED`）**

| 模块 | 条目 | 说明 |
|---|---|---|
| 员工 | P3-EMP-05 离职 WorkItem 批量转交；P3-EMP-06 电话长度口径统一 | 能力未实现，处置码 `CARRY_OVER` |
| 库存 | P3-INV-05 扫描中断恢复 | 批次 ID + 游标 + 续跑未实现 |
| 会员 | P3-MB-02 等级目录受控；P3-MB-06 Vip→会员迁移映射；P3-MB-07 表单可用性 | 依赖数据规范/生产数据决策 |
| 预订 | P3-RES-10 特殊预订状态扩展（新增，缺批准记录） | 见 §2.7 |
| 财务 | P3-FIN-04 旧数据逐笔对账收口；P3-FIN-07 退款失败/重试状态表达；P3-FIN-09 双轴口径页面区分；P3-FIN-10 状态机扩展（新增，缺批准记录） | 见 §2.7 |
| 夜审 | P3-NA-06 REJECTED 端到端；P3-NA-07 单一经理资格边界用例 | 用例/端到端补齐 |
| 客房维修 | P3-HK-04 `RESTORED` 死状态清理；P3-HK-05 报修表单字段暴露 | 模板标签与表单字段 |
| 报表 | P3-RPT-03 跨报表勾稽说明 | 现取「勾稽」在 `apps/`、`templates/` 零命中 |
| 通知 | P3-NTF-03 待办来源扩展（5 项未派生） | 派生清单待决策 |
| 平台 | P3-PLT-08 性能与可观测性（新增，缺批准记录） | 见 §2.7 |

**（b）已批准移交 PMS3、不得在 PMS2 重复建设（7 条 `DEFERRED_TO_PMS3`）**

P3-NA-03 / P3-NA-04 / P3-NA-05（NS-E-04 `APPROVED_PMS3_HANDOFF`）、P3-AUD-03（NS-E-03）、P3-INV-04、P3-RPT-07、P3-EMP-04（F-11 多物业冻结）。
> **约束**：移交 PMS3 **不计入** PMS2.0 已实现功能；在 PMS3 内承接时**不得做伪链式哈希、不得为补哈希重写历史审计**；夜审调度监控与经营报表扩展的承接载体为 PMS3 专项，**不得重复派发**。

**（c）明确不构成阻塞的理由**：以上条目均不触及 PMS3.0 的模块边界、API 封套、Outbox 契约、Session 身份体系与权限码治理五条主干；P3-REQ-001 §12.1 第一批 8 项边界中，仅第 8 项「历史数据只读与归档体系」依赖上述条目，且该项在 PMS3 内亦不得绕过 M5-4 八项前置（见 §2.4 受限批次约束 ①）。

### 2.4 PMS2 未完成且会阻塞 PMS3 的模块

**结论：无阻塞项。**

| 判据 | 现取事实 | 证据 |
|---|---|---|
| P0 缺口 | **P0 = 0、P1 = 0**（OWNER-02 裁定口径②，91 项计分缺口 / 46 关闭 / 45 开放 / 实际需处置 44） | `docs/共享规范/PMS当前状态总览.md:52` |
| 安全阻塞 | 改密二次认证已实现且有回归（R5）；冷却窗口绕过口子已修复并「先红后绿」实测（`ce549b7` + `15cec54`） | `docs/acceptance/G14同基线最终补测报告_2026-11-27.md:39-55` |
| 账务阻塞 | 统一账本写入口 + 不可变凭证 + 三层防护在位；DEC-01/02/03 口径未漂移 | PMS3-REQ-001 `:131-133`、`:119-121` |
| 权限阻塞 | NS-E-05 五码逐项裁定并落地；Q-03 现状为「财务岗位当前**无任何路径能读到薪酬**」 | `apps/identity/registry.py:492-758`；`docs/acceptance/Q-03财务薪酬只读权限裁定材料_2026-11-27.md` 结论 3 |

**受限批次约束（不构成全局阻塞，但约束具体批次）**

| # | 约束 | 适用范围 | 解除条件 | 证据 |
|---|---|---|---|---|
| ① | **归档实施一律不得启动** | PMS3 第一批第 8 项「历史数据只读与归档体系」 | M5-4 八项前置全部满足 + L4 数据删除审批 + 负责人对八项前置的最终确认 | `docs/acceptance/M5-4-八项前置条件完整清单_2026-11-27.md` 结论 5/6（**0/8 满足**） |
| ② | **多物业切换继续冻结** | P3-EMP-04 / P3-EMP-09 及一切跨物业上下文改造 | 负责人批准（L3 架构变更）+ `PropertyContextMiddleware` 与全部 `property_scoped` 校验改造 + 跨物业调岗验收 | F-11（PMS3-REQ-001 `:237`）；台账 `:109` |
| ③ | **姓名揭示不得恢复** | 任何 P3 新增/重构的客人中心与 PII 读路径 | **无解除条件**（OWNER-06 终局 `REJECTED`）；确需受控揭示时只能走既有 `guest_document_reveal` / `guest_phone_reveal` 专项码（需专门授权 + 审计） | 2026-11-26 版矩阵 §二 第 1 项；`apps/guests/name_pii.py:11-15` |

### 2.5 生产依赖事项

| 编号 | 事项 | 状态 | 是否阻塞 PMS3.0 本地开发 |
|---|---|---|---|
| PROD-01 | 生产服务器与部署 | `WAITING_FOR_SERVER` | 否 |
| PROD-02 | 生产环境密钥托管与轮换（含 PII 独立密钥） | `PRODUCTION_PENDING` | 否 |
| PROD-03 | 真实生产备份恢复演练 | `WAITING_FOR_DBA` | 否 |
| PROD-04 | 生产 Redis 多 worker | `PRODUCTION_PENDING` | 否 |
| PROD-05 | 真实支付渠道/银行/供应商联调 | `WAITING_FOR_PROVIDER` | 否 |
| PROD-06 | 生产数据库特权操作（V13-01 孪生账户取证） | `WAITING_FOR_DBA` | 否 |
| PROD-07 | 生产监控告警与灾备演练 | `PRODUCTION_PENDING` | 否 |
| PROD-08 | MySQL 生产连接容量与池化验证 | `PRODUCTION_PENDING` | 否 |
| M5-4-B | 生产真实数据归档层（八项前置 0/8） | `PRODUCTION_PENDING` + `WAITING_OWNER` | 否（但约束归档批次，见 §2.4 ①） |
| ARCH-11 触发器行为级验证 | 需具备 SUPER 权限的独立 MySQL（本机 1419） | `WAITING_ENVIRONMENT` | 否 |

> 依据：任务书 §五末句「生产事项不阻塞 PMS3.0 本地开发」；`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:19,86`。
> **GATE-PROD 未开启且不得部分通过**；生产上线必须另行验收（任务书 §十五 C 末句）。

### 2.6 负责人未裁定事项

| # | 事项 | 状态 | 对 PMS3.0 核心架构的影响 | 证据 |
|---|---|---|---|---|
| 1 | G14 最终签署 | `WAITING_OWNER` | 无（仅验收动作） | `docs/PMS2.0_当前版本/06_验收与发布/96-g14-signoff_2026-11-10.md`（留空未代签） |
| 2 | ARCH-04 C11 未映射旧员工（生产数量口径） | `WAITING_OWNER` | 无（数据口径） | ARCH-04 裁定材料 结论 9 |
| 3 | ARCH-04 C12 月薪/工龄是否迁移 | `WAITING_OWNER` | 无（字段归属，不改模块边界） | ARCH-04 裁定材料 结论 10 |
| 4 | ARCH-04 C14 旧 Staff 表保留策略 | `WAITING_OWNER` | 无（材料**不建议删除旧表**） | ARCH-04 裁定材料 结论 11 |
| 5 | ARCH-11 裁定项一：是否新增审批历史（事件流）表 | `WAITING_OWNER` | 无（`apps.billing` 内新增只追加表，状态权威仍留 `ApprovalRecord`） | ARCH-11 负责人裁定材料 §〇 |
| 6 | ARCH-11 裁定项二：是否建设审批附件能力 | `WAITING_OWNER` | 无（PMS2 中附件能力**完全不存在**，新建不改既有边界） | ARCH-11 评估 结论 3 |
| 7 | ARCH-11 裁定项三：是否建设审批历史导出 | `WAITING_OWNER` | 无（可基于现状 `ApprovalRecord` 实现） | ARCH-11 负责人裁定材料 §〇 |
| 8 | Q-03 是否独立建立薪酬只读权限码 | `WAITING_OWNER` | 无（建议独立 `identity.staff_compensation_view`，**不复用** `identity.view_staffprofile`） | Q-03 裁定材料 结论 7 |
| 9 | ARCH-05 核查点 7：新旧密码是否禁止相同 | `WAITING_OWNER` | 无（已有可回退开关 `PASSWORD_POLICY_FORBID_REUSE`，默认 True） | ARCH-05 报告 §一 |
| 10 | OWNER-05 保留期限值与法律责任人（含审计留存口径冲突） | `WAITING_OWNER` | 无（期限数据驱动、代码零硬编码） | OWNER-05 裁定材料 结论 C-1/C-2/C-8 |
| 11 | OWNER-07 需求状态口径最终选择 | `WAITING_OWNER` | 无（文档层口径） | OWNER-07 裁定材料 结论 7 |
| 12 | M5-4 八项前置的最终确认与责任归属 | `WAITING_OWNER` | 无（生产批次） | M5-4 清单 结论 6 |
| 13 | PMS3 新增 3 条需求的批准记录 | `NOT_STARTED` | 无（未立项即不产生架构影响） | DEL-18 §2.12 |

> **反向约束**：上述任一项在 PMS3 立项前获得裁定时，**只能按其裁定结果实施**，不得在 PMS3 内以「默认口径」绕过；OWNER-05 在裁定前，**不得**把默认值伪装为已批准的法律期限。
> **审计留存口径冲突提示**：仓库内存在两处互相冲突的留存口径（审计留存 10 年已裁定 vs 「待确认、不设固定年限」），见 OWNER-05 裁定材料 结论 C-8，PMS3 内不得擅自择一实现。

### 2.7 PMS3 新增开发范围

**（a）第一批开发边界 8 项**（PMS3-REQ-001 §12.1，**不新增、不改判**，仅补执行顺序）

| 顺序 | 边界项 | 对应条目 | 前置 | 备注 |
|---|---|---|---|---|
| 1 | 通知与待办权威关系 | P3-NTF-01/02/05（`INHERIT`） | 无，可立即开工 | 零迁移成本；禁止「标记完成即改预订状态」 |
| 2 | Outbox 与事件可靠性 | P3-PLT-01（`INHERIT`） | 事件目录与契约 DEC-27 已批准 | **不得新建第二套事件总线** |
| 3 | 历史数据只读与归档体系 | P3-G2-08 / P3-RES-07/08 / P3-FIN-04 | 依赖 ARCH-02/03 处置矩阵 + ARCH-04 退役条件 | **归档实施受 §2.4 ① 约束，不得绕过 M5-4 八项前置** |
| 4 | Guest 重复档案治理受控流程 | P3-G2-06/07 | 合并规则需负责人批准（DEC-03/06） | **禁止自动合并** |
| 5 | 会员历史关系与权益治理 | P3-MB-02/03、P3-G2-09 | 等级目录与权益体系需负责人决策 | **权益/积分未经 DEC-15 批准不得建模** |
| 6 | 夜审调度监控与失败提示 | P3-NA-03/04 | 已批准移交（`APPROVED_PMS3_HANDOFF`） | 承接载体为 PMS3 专项，**不得重复派发** |
| 7 | 审计完整性增强 | P3-AUD-03 | 已批准移交 | **不得做伪链式哈希、不得重写历史** |
| 8 | 经营报表扩展 | P3-RPT-07/03 | 指标定义前置；口径扩展需负责人确认（DEC-16） | — |

**（b）PMS3 新增需求 3 条（含批准记录登记）**

| Req ID | 需求 | 来源 | 业务目的 | 优先级 | 批准记录 | 状态 |
|---|---|---|---|---|---|---|
| P3-RES-10 | 特殊预订状态扩展 | V2.3 §5.5 当前缺口 | 业务灵活：支持非标准预订状态 | L | **缺失** | `NOT_STARTED` |
| P3-FIN-10 | 支付/退款/账本状态机扩展 | V2.3 §24.3 目标状态机 | 异常健壮：覆盖退款失败与补偿路径 | L | **缺失** | `NOT_STARTED` |
| P3-PLT-08 | 性能与可观测性（指标/追踪） | V2.3 §15.3 INF-04/05、§25.5、DEC-21 | 运营质量：关键路径可观测 | L | **缺失** | `NOT_STARTED` |

> 3 条新增需求**均缺负责人批准记录**，按任务书 TASK-22 第 9 项一律标 `NOT_STARTED` 并注明缺批准记录；**不得**表述为「已立项」或「已批准」。

**（c）明确不得纳入 PMS3.0 第一阶段的事项**：PROD-01～08 相关实现（生产资源未到位）；G14 签署动作（仅负责人）；任何绕过 OWNER 裁定的功能实现；多物业切换；姓名揭示；M5-4 生产归档实施。

### 2.8 PMS3 不允许重复开发的能力

| # | 禁止重复项 | 唯一权威 | 证据 |
|---|---|---|---|
| 1 | 不得新建第二套客人主表 | `apps.guests` | PMS3-REQ-001 `:261` |
| 2 | 不得再造预订系统 | `apps.reservations` | 同上 `:262` |
| 3 | 不得再造账本 | `apps.billing`（禁止覆盖历史凭证） | 同上 `:263` |
| 4 | 不得再造库存占用系统 | `apps.inventory` | 同上 `:264` |
| 5 | 不得新建第二套员工身份/授权服务 | `apps.identity`（禁止 `User.groups` 作业务权限权威） | 同上 `:265` |
| 6 | 不得新建第二套清洁/维修任务状态机 | `apps.inventory` 维修状态机 | 同上 `:266` |
| 7 | 不得引入第二套 PII 加密体系 | `apps/guests/services.py` Fernet + HMAC | 同上 `:267` |
| 8 | 不得用普通日志替代审计 | `AuditEvent` 唯一 | 同上 `:268` |
| 9 | 不得让 WorkItem 成为第二套业务状态系统 | WorkItem 仅待办提示（NS-E-02） | 同上 `:269` |
| 10 | 不得新增第三份 OpenAPI 快照 | 两份快照逐字节一致（现取 175 paths / 177 ops） | 同上 `:270`；`apps/api/test_openapi_schema.py` |
| 11 | 不得把「移交 PMS3」计入 PMS2 已实现功能 | 7 条 `DEFERRED_TO_PMS3` 独立登记 | 同上 `:271`；DEL-18 §2.11 |
| 12 | 不得把生产待办（PROD-01~08）改标为已完成 | 8/8 保留 `PRODUCTION_PENDING` | 同上 `:272`；`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:83-88` |
| 13 | **不得恢复姓名揭示**（硬边界） | `guest_document_reveal` / `guest_phone_reveal` 专项码 + 审计；无 `guest_name_reveal` | `apps/guests/name_pii.py:11-15`；`apps/identity/registry.py:494-509` |
| 14 | **不得擅自启用多物业切换**（硬边界） | 单物业运营优先；跨物业属 L3 架构变更 | F-11（PMS3-REQ-001 `:237`） |
| 15 | 不得引入第二套事件总线 | `apps/core/outbox.py` + `outbox_worker` | PMS3-REQ-001 `:201`；`apps/core/management/commands/outbox_worker.py` |
| 16 | 不得批量扩展权限码 | 逐项审批（F-08）；现取 27 个专项码 | PMS3-REQ-001 `:255`；`apps/identity/registry.py:492-758` |

### 2.9 PMS3.0 继续遵守的技术约束核对

| # | 约束 | 现状（现取） | 是否满足 | 核对方法 |
|---|---|---|---|---|
| 1 | **模块化单体** | `apps/` 下 6 个业务应用：`api`、`billing`、`core`、`guests`、`identity`、`inventory`、`reservations`；依赖方向由 `scripts/arch_gate.py`（L1/L2）纳入 CI，违规即非零退出、读取失败 fail-closed | ✅ 满足 | 现取 `apps/` 目录清单；台账 `:262`（ARCH-10） |
| 2 | **Django 后端** | Django **4.1**（`django.get_version()`）；`DJANGO_DB_ENGINE` 仅支持 mysql/postgresql/sqlite，生产强制 mysql | ✅ 满足 | `python -c "import django;print(django.get_version())"`；`hotel/settings.py:124,132,424` |
| 3 | **Django Template + 原生 JS** | `templates/` 现存 **29** 个模板（ARCH-03 退役 8 个后）；`static/js/` 仅原生脚本（`frontdesk.js`、`permission_center.js`、`pms-ux.js` 等），无前端构建、无框架、无 Node 依赖链 | ✅ 满足 | 现取模板与脚本计数；`docs/acceptance/PMS3.0需求继承及冻结矩阵_2026-11-26.md:17` |
| 4 | **MySQL 生产事实库** | `DJANGO_DB_ENGINE=mysql` 为默认；生产启动校验强制 mysql（`hotel/settings.py:424`）；本机 MySQL 8.0.33；SQLite 仅 CI/轻量口径，**SQLite 全绿不替代 MySQL 权威** | ✅ 满足 | `hotel/settings.py:124,424`；`docs/acceptance/G14同基线最终补测报告_2026-11-27.md:5` |
| 5 | **MySQL Outbox + Django Worker** | `OutboxEvent`（`apps/core/models.py:954`）+ `apps/core/outbox.py`（同事务入队 / `skip_locked` 批量租约 / 退避 / 死信 / 崩溃恢复）+ `apps/core/management/commands/outbox_worker.py`；入队与消费双端 HIGH_RISK 硬拒绝 | ✅ 满足（生产多 Worker 部署仍属 PROD-04） | 现取代码；台账 `:238,253` |
| 6 | **Session 身份体系** | `CSRF_USE_SESSIONS = True`（`hotel/settings.py:222`）；平台域 `platform_sessionid` 与 Django Admin `django_admin_sessionid` 双 Cookie 隔离；**平台身份与 PMS 员工身份分域不可合并** | ✅ 满足 | `hotel/settings.py:222`；PMS3-REQ-001 `:67` |
| 7 | **既有 RBAC 与权限专项码治理** | `apps/identity/registry.py` = 模块注册表 + `SPECIAL_PERMISSIONS`（现取 **27** 个专项码，含 `requires_reason` / `requires_confirm` / `risk`）；`AuthorizationService` + `StaffRoleAssignment` + 九岗位矩阵；专项码与模块 `special` 元组**双向一致**（有测试锁定）；**权限码逐项审批，不批量扩展** | ✅ 满足 | `apps/identity/registry.py:492-758,116-125`；`docs/acceptance/NS-E-05逐项裁定落档与准入核验_2026-11-24.md` |
| 8 | **不引入已冻结技术栈** | 现栈 = Django 4.1 + 原生 JS + MySQL 8 + 浏览器原生；无第二套 ORM / 前端框架 / 消息中间件；统一 API 封套 `{data, meta}` / `{error:{code,message}}`；两份 OpenAPI 快照逐字节守卫 | ✅ 满足 | 现取依赖与目录；PMS3-REQ-001 `:18-19` |
| 9 | **多物业切换未获批准前继续冻结** | F-11 冻结在位；多物业组织架构 `P3-EMP-09` 仍 `WAITING_OWNER`；跨物业调岗 `P3-EMP-04` `DEFERRED_TO_PMS3`；物业隔离由 `apps/identity/authorization.py` 的 `filter_queryset_for_property` 强制 | ✅ 满足（**继续冻结**） | PMS3-REQ-001 `:73,237`；现取 `apps/identity/authorization.py` |

### 2.10 两条硬边界（写入即生效，不可由 PMS3 自行解除）

> **硬边界一：姓名揭示不得恢复。**
> OWNER-06 终局 `REJECTED`，`apps/identity/registry.py` **无** `guest_name_reveal`，`reveal_guest_name` 依赖未获批权限码并已整体移除。PMS3 内**不得**新增同名或等价绕过入口；确需受控揭示时，只能走既有 `hotelapp.guest_document_reveal` / `hotelapp.guest_phone_reveal` 专项码（需专门授权 + 理由必填 + 落审计）。
> 依据：`docs/acceptance/PMS3.0需求继承及冻结矩阵_2026-11-26.md:34`；`apps/guests/name_pii.py:11-15`；`apps/identity/registry.py:494-509`。

> **硬边界二：多物业切换未获批准前继续冻结。**
> 「当前单物业运营优先，不启用未经验收的切换能力」。启用属 L3 架构变更，须负责人批准，并须完成 `PropertyContextMiddleware` 与全部 `property_scoped` 校验改造及跨物业调岗验收。
> 依据：F-11（PMS3-REQ-001 `:237`）；`docs/acceptance/PMS3.0需求继承及冻结矩阵_2026-11-26.md:36`；台账 `:109`。

> **硬边界三（附带）：生产真实数据清理与物理删除在 OWNER-05 裁定前继续冻结**，不得以「默认值伪装已批准的法律期限」或直接清理生产数据的方式绕过。
> 依据：`docs/acceptance/PMS3.0需求继承及冻结矩阵_2026-11-26.md:35`；`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:85`。

---

## 三、通过条件逐条对照（任务书 §十五 C，第 1055–1064 行）

| # | 通过条件 | 逐条对照 | 判定 |
|---|---|---|---|
| 1 | **PMS2.0 已完成能力明确继承** | 46 条 `DONE` + 8 条 `PASS_WITH_RESERVATIONS` 已达成部分，覆盖 12 个模块（§2.1、§2.2）；14 项必须继承的技术约束逐条复核仍成立，其中 2 处数字按现取更新（专项权限码 22 → **27**，模板 37 → **29**） | ✅ 满足 |
| 2 | **PMS2.0 未完成事项明确归属** | 19 条 `NOT_STARTED` + 7 条 `DEFERRED_TO_PMS3` + 5 条 `WAITING_OWNER` + 4 条 `WAITING_ENVIRONMENT` 逐条归属：PMS3 承接 / 负责人裁定 / 生产资源 / 并行子任务，全部有责任方与复核方法（DEL-18 §四） | ✅ 满足 |
| 3 | **不存在未处理的 P0 安全、账务或权限阻塞** | P0 = 0、P1 = 0（OWNER-02 口径②）；改密二次认证已实现且有回归；冷却窗口绕过口子已「先红后绿」实测修复；NS-E-05 五码逐项裁定落地；薪酬只读现状为「财务岗位无任何路径可读到」 | ✅ 满足（**保留 1 条未来泄露口**：`legacy_staff_archive` 薪酬明文列一旦接入归档查询通道或 Admin 即成泄露口，见 Q-03 裁定材料 结论 9） |
| 4 | **多物业、姓名揭示、生产清理等冻结边界没有被擅自打开** | 多物业：`P3-EMP-04` `DEFERRED_TO_PMS3`、`P3-EMP-09` `WAITING_OWNER`；姓名揭示：无 `guest_name_reveal`，`reveal_guest_name` 已移除；生产清理：PROD-01～08 8/8 未改标、M5-4 八项前置 0/8 未启动 | ✅ 满足 |
| 5 | **PMS3.0 需求矩阵和开发批次明确** | 矩阵 = DEL-18（89 条逐条最终状态）+ PMS3-REQ-001（12 模块 / 8 态处置码）；开发批次 = PMS3-REQ-001 §12.1 第一批 8 项 + 执行顺序建议（§2.7）；3 条新增需求缺批准记录已明确标注 | ✅ 满足 |
| 6 | **Owner 未决事项不会导致 PMS3.0 核心架构返工** | 13 项 `WAITING_OWNER` / `NOT_STARTED` 全部落在**数据治理、字段归属、审批元数据、附件能力、导出能力、权限码、期限值、文档口径、生产批次**范围，**无一触及** `apps` 模块边界、API 封套、Outbox 契约、Session 身份体系、权限码治理五条主干 | ✅ 满足 |

> 任务书 §十五 C 末句：「生产环境尚未就绪不阻塞 PMS3.0 本地开发，但正式生产上线必须另行验收。」→ 本报告据此**不作生产上线许可**。

### 3.1 证据索引（文件路径 + 行号 / 命令 + 结果）

| 证据项 | 路径 / 命令 | 关键结果 |
|---|---|---|
| 代码基线 | `git -C F:\ZCode\pms2-final-docs rev-parse HEAD` | `0fa7086ea3ecc514dab4a521e860102b533b9a82` |
| 远端同点核对 | `git rev-list --left-right --count origin/main...HEAD` | `0 0` |
| 需求矩阵基线 | `docs/acceptance/PMS3.0-需求继承矩阵最终修订版_2026-11-27.md`（DEL-18） | 89 条逐条最终状态，分布 DONE 46 / PWR 8 / NS 19 / D2P3 7 / WO 5 / WE 4 |
| 双引擎全量 | `docs/acceptance/evidence/g14-final-20261127/{sqlite,mysql}-23fdd00.log` | 双侧 2229 OK，绑 `23fdd00` |
| MC11 缺口统计 | `docs/共享规范/PMS当前状态总览.md:52` | 91 计分缺口 / 46 关闭 / 45 开放 / 实际需处置 44，**P0=0、P1=0** |
| 模块化单体 | 现取 `apps/` 目录清单 | `api` / `billing` / `core` / `guests` / `identity` / `inventory` / `reservations` |
| Django 后端 | `python -c "import django;print(django.get_version())"`；`hotel/settings.py:124,132,424` | Django `4.1`；`DJANGO_DB_ENGINE` 仅 mysql/postgresql/sqlite；生产强制 mysql |
| Template + 原生 JS | 现取 `templates/`（29 个 `.html`）、`static/js/` | 无前端构建、无框架、无 Node 依赖链 |
| Outbox + Worker | `apps/core/models.py:954`；`apps/core/outbox.py`；`apps/core/management/commands/outbox_worker.py` | 同事务入队 / `skip_locked` 租约 / 退避 / 死信 / 崩溃恢复 / HIGH_RISK 双端硬拒绝 |
| Session 身份体系 | `hotel/settings.py:222` | `CSRF_USE_SESSIONS = True`；平台域与 Admin 域双 Cookie 隔离 |
| RBAC 与专项码治理 | `apps/identity/registry.py:492-758,116-125` | 27 个专项码；与模块 `special` 元组双向一致（有测试锁定） |
| 生产依赖 | `docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:19,32-39,47,83-88` | 8/8 保留 `PRODUCTION_PENDING`；GATE-PROD 未开启 |
| M5-4 八项前置 | `docs/acceptance/M5-4-八项前置条件完整清单_2026-11-27.md` 结论 5/6 | **0/8 满足**；最终确认与责任归属 `WAITING_OWNER` |
| 两条硬边界 | `docs/acceptance/PMS3.0需求继承及冻结矩阵_2026-11-26.md:34,36`；`apps/guests/name_pii.py:11-15`；`apps/identity/registry.py:494-509` | 姓名揭示无 `guest_name_reveal` 且已整体移除；多物业切换继续冻结 |
| 引用路径复验 | 脚本复验本文件内全部 `路径:行号` 引用 | **缺失/越界 = 0** |
| 文档门禁 | `python scripts/docs_link_check.py check` | `新增违规数=0`，exit 0 |

---

## 四、未完成与待裁定事项

| # | 事项 | 状态 | 责任归属 | 复核方法 |
|---|---|---|---|---|
| 1 | G14 最终签署（签署页留空未代签） | `WAITING_OWNER` | 负责人本人 | `docs/PMS2.0_当前版本/06_验收与发布/96-g14-signoff_2026-11-10.md` |
| 2 | G14 保留意见 6 条（R1～R7） | `PASS_WITH_RESERVATIONS` | 负责人 + 运维（DBA 授权） | `docs/acceptance/G14同基线最终补测报告_2026-11-27.md:152-164` |
| 3 | ARCH-04 C11 / C12 / C14 | `WAITING_OWNER` | 负责人 | ARCH-04 技术整改与负责人裁定材料 结论 9/10/11 |
| 4 | ARCH-11 三项裁定 | `WAITING_OWNER` | 负责人 | ARCH-11 负责人裁定材料 §〇 |
| 5 | Q-03 薪酬只读权限码是否独立建立 | `WAITING_OWNER` | 负责人 | Q-03 财务薪酬只读权限裁定材料 结论 7 |
| 6 | ARCH-05 核查点 7 | `WAITING_OWNER` | 负责人 | ARCH-05 密码修改安全最终验收报告 §一 |
| 7 | OWNER-05 期限值、法律责任人、审计留存口径冲突 | `WAITING_OWNER` | 负责人 + 法律 | OWNER-05 裁定材料 结论 C-1/C-2/C-8 |
| 8 | OWNER-07 状态口径最终选择 | `WAITING_OWNER` | 负责人 | OWNER-07 裁定材料 结论 7 |
| 9 | M5-4 八项前置最终确认与责任归属 | `WAITING_OWNER` + `PRODUCTION_PENDING` | 负责人 + DBA | M5-4 八项前置条件完整清单 结论 5/6 |
| 10 | PROD-01～08 全部 8 项 | `PRODUCTION_PENDING` | 运维 / DBA / 商务 | PROD-01至08与M5-4依赖状态表 §1 |
| 11 | TASK-12 MySQL append-only 触发器行为级验证 | `WAITING_ENVIRONMENT` | DBA（需 SUPER 权限独立 MySQL） | `AGENTS.md:43`；G14 报告 R5；**本轮专项环境限制报告由并行子任务产出，尚未入库 → 未核实** |
| 12 | PMS3 新增 3 条需求的负责人批准记录 | `NOT_STARTED` | 负责人 | DEL-18 §2.12 |
| 13 | 19 条 `NOT_STARTED` 中不阻塞本地开发的部分 | `NOT_STARTED` | PMS3 承接 | DEL-18 §2.3 |
| 14 | `docs/共享规范/PMS当前状态总览.md` 当前值过期（基线、测试数、权限码数、模板数） | `PASS_WITH_RESERVATIONS` | 主控会话 | DEL-18 §2.2 |
| 15 | `requirements.txt` 未声明 `playwright` / `requests` | `NOT_STARTED` | 开发 | G14 报告 R6 |

---

## 五、下一步责任归属

| 阶段 | 责任方 | 动作 | 前置 |
|---|---|---|---|
| **准入生效** | PMS3 开发团队 | 以本报告 §2.1～§2.10 与 DEL-18 §2.3 作为 PMS3.0 本地开发的启动依据；按 §2.7 第一批 8 项开工 | 本报告与 DEL-18 入库 |
| **并行推进（不阻塞开发）** | 运维 / DBA / 商务 | 按「解除条件」逐项推进 PROD-01～08 | 生产环境（PROD-01） |
| **并行推进（不阻塞开发）** | PMS3 开发团队 | 承接 19 条 `NOT_STARTED` 与 7 条 `DEFERRED_TO_PMS3`；**不得**重复建设 §2.8 的 16 项 | — |
| **待裁定（不阻塞开发）** | 负责人 | 逐项批复 13 项 `WAITING_OWNER` / `NOT_STARTED` | 裁定材料已备齐 |
| **严禁事项** | 全体 | 不得恢复姓名揭示；不得擅自启用多物业切换；不得在 OWNER-05 裁定前执行生产清理；不得启动 M5-4 生产归档；不得批量扩展权限码；不得新建第二套事实源 | §2.10、§2.8 |
| **生产上线** | 负责人 + 运维 | 另行组织生产验收（GATE-PROD 8/8） | 生产环境就绪 |

---

> **本报告是 PMS3.0 本地开发准入材料，不是生产上线许可，不替代 G14 负责人签署与 MC11 独立验收，也不代替负责人对任何 `WAITING_OWNER` 事项的裁定。**
> **基线口径差异声明**：`docs/共享规范/PMS当前状态总览.md:5,10` 记当前基线 `6d7d9ce`、权威全量 `2177/2177`（绑 `5cad6b7`）；本轮现取 HEAD 为 `0fa7086`，本轮 G14 双引擎全量为 **2229/2229**（绑 `23fdd00`）。本报告一切结论以现取为准；总览的「当前值」载体由主控会话统一更新，本报告不修改。
