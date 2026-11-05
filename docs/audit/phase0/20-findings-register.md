# Phase 0 缺口登记册（工作流 H）

> 审计基线：main @ `100a93a1382ca310aae588bc3a1734e4d67e94ba`
> 测试基线：`manage.py test --noinput` — SQLite 181 项全过（2026-11-04）；MySQL 8 同基线 181 项全过
> 报告索引：10-routes / 11-permissions / 12-roles / 13-identity-sessions / 14-dataflows / 15-audit-coverage
> 处置原则：审计任务书 §2.3——P0 风险登记阻塞；紧急安全修复须负责人批准并单独变更记录

## 1. P0（阻塞 Phase 1 启动，须紧急安全修复或明确接受）

| 编号 | 缺口 | 证据 | 影响 | 建议处置 |
|---|---|---|---|---|
| F-01 | BookingAccessToken 在创建事务提交后才签发 | 14-dataflows G1（审计时点）；后续修复见 Phase 5/更新日志 | 签发失败可能造成孤儿订单 | **已修复（2026-11-04）**：BookingAccessToken 移入创建事务；仍需在当前 main 的 CI/业务链路回归中持续验证 |
| F-02 | User 直授组残留及历史测试/孤儿账号治理 | AUTH-22 `classify_users` 盘点命令；本地数据库实时状态需重新盘点 | **负责人已批准本地/测试环境清理方向（2026-11-05）**：先备份、重新盘点和预览；清除冗余 User.groups，保留正式 StaffRoleAssignment；孤儿账号默认禁用而非物理删除；九角色测试账号必须重建并通过登录/权限验收。尚未执行数据库清理。 |
| F-03 | 前台核心动作（排房/入住/退房/续住/换房/No-Show）零 DB 审计 | 15-audit-coverage（审计时点）；后续修复见更新日志与 front_desk_services.py | 仅文件日志会降低关键动作追溯性 | **已补核心动作 DB 审计（2026-11-04）**：排房/入住/退房/续住/换房/No-Show 增加 AuditEvent；需持续核对事件字段脱敏和失败路径 |
| F-04 | 权限矩阵保存仅 logger 无 DB 审计 | permission_admin.py:151 | 授权变更不可追溯（与退款/夜审已落库形成反差） | Phase 3 权限中心重构时一并解决（§8.3 流程） |

## 2. P1（Phase 1-2 处置）

| 编号 | 缺口 | 证据 | 处置归属 |
|---|---|---|---|
| F-05 | OpenAPI 漂移：daily-rates adjust 文档写 change_room，实际执行 inventory.manage_rates | 11-permissions；openapi_schema.py:175 | Phase 4 契约核验 |
| F-06 | 敏感导出审批硬编码角色名（_has_role），绕开权限矩阵 | hotelapp/views.py:320,1466-1470 | **已处置（Phase 5，2026-11-05）**：新增 `hotelapp.export_approve` 权限码（migration hotelapp.0019，语义不变授予值班经理/店长），approve_staff_export 判定改走 AuthorizationService；registry SPECIAL_PERMISSIONS 与 staff 模块 special 元组同步登记 |
| F-07 | `guests.document_reveal` 孤儿权限点（仅定义，代码用 hotelapp 码） | guests/migrations/0003 | Phase 2 清理（登记后删除或映射） |
| F-08 | reverse_checkout / room_release_ready 权限悬空（唯一检查点在 410 死代码，PMS2 无对应 API） | 11-permissions | Phase 4：front-desk 补 API 或登记退役 |
| F-09 | /permissions/ 矩阵只管辖 hotelapp 域 41 点；billing.refund_*、identity.staff_manage、inventory.manage_rates、core.breakglass_review 矩阵外 | 12-roles 漂移分析 | Phase 2 跨 App RBAC 注册表（核心动因） |
| F-10 | PropertyContextMiddleware 不豁免平台路径：property_context_denied 已积累 38 条、100% 来自超管访问平台面（审计时点） | 13-identity-sessions；后续平台身份域实施记录 | **已处置（Phase 1）**：平台路径豁免物业上下文中间件，旧审计噪声保留为历史记录，不回写或删除历史事件 |
| F-11 | /admin/login/ 无登录限流（PMS /login/ 有 5/5min fail-closed） | 13-identity-sessions | Phase 1 统一登录保护 |

## 3. P2（登记备查）

- 16 个旧 CRUD 权限检查点位于 410 死代码；6 个 delete_* 叠加"无组持有"双失效（Phase 5 旧入口退役时清理）
- AUTH-22 `classify_users` 盘点 management command 已实现只读盘点；当前分类按有效任职/生效授权判定。数据库账号处置仍未实现、未执行，不能将盘点命令等同于清理工具。
- auth.logout / security.permission_denied / identity.invitation_* / Refund 拒绝终态审计缺失（随对应功能阶段补齐）

## 4. 与治理方案的映射

| 缺口 | 对应 Phase | 方案章节 |
|---|---|---|
| F-01 | Phase 0→紧急止血 / Phase 4 链路验证 | §9.2 |
| F-03/F-04 | Phase 1/3 | §6、§8.3 |
| F-05/F-06/F-08 | Phase 2/3/4 | §7.2、§9.8 |
| F-09 | Phase 2（注册表核心动因） | §7.2/7.3 |
| F-10/F-11 | Phase 1 平台域 | §6.1.8、§7.1 |
| F-02 | Phase 1 AUTH-22 历史账号盘点 | 身份方案 2.0 §12 |


## 5. 2026-11-05 后续处置状态校准

本登记册的原始审计基线仍为 2026-11-04 的 `100a93a`，上表状态根据后续 main 提交补充，不覆盖原始证据：

- F-01：已修复，待当前主干持续回归。
- F-02：只读盘点命令已具备；负责人批准本地/测试库治理方向，但数据库清理和九角色本地验收尚未执行。
- F-03：六类前台关键动作已补 AuditEvent；不代表所有审计字段和拒绝路径已完整验收。
- F-06：已迁移至 `hotelapp.export_approve` 权限码（Phase 5）。
- F-10：平台路径物业上下文豁免已实施。
- 其余缺口按原状态保留；未有新证据的项目不擅自改为完成。
