# Phase 0 缺口登记册（工作流 H）

> 审计基线：main @ `100a93a1382ca310aae588bc3a1734e4d67e94ba`
> 测试基线：`manage.py test --noinput` — SQLite 181 项全过（2026-11-04）；MySQL 8 同基线 181 项全过
> 报告索引：10-routes / 11-permissions / 12-roles / 13-identity-sessions / 14-dataflows / 15-audit-coverage
> 处置原则：审计任务书 §2.3——P0 风险登记阻塞；紧急安全修复须负责人批准并单独变更记录

## 1. P0（阻塞 Phase 1 启动，须紧急安全修复或明确接受）

| 编号 | 缺口 | 证据 | 影响 | 建议处置 |
|---|---|---|---|---|
| F-01 | BookingAccessToken 在创建事务提交后才签发 | 14-dataflows G1；apps/api/views.py:195-202 | 签发失败→已扣库存但顾客拿不到 token 的孤儿订单，无法查询/支付/取消 | 移入创建事务内签发（小改动，建议批准紧急修复） |
| F-02 | User `test` 无员工档案却持值班经理组 + 组外直授 user_permissions | 13-identity-sessions §账号分类 | 绕过角色组与授权审计的异常授权账号 | 登记待处置（D10 分类流程），不由审计自行改库 |
| F-03 | 前台核心动作（排房/入住/退房/续住/换房/No-Show）零 DB 审计 | 15-audit-coverage；front_desk_services.py:112/196/340 | 仅 hotel.audit 文件日志，轮转即失去房态/账务变更追溯 | Phase 1 首批：front_desk 全动作补 AuditEvent |
| F-04 | 权限矩阵保存仅 logger 无 DB 审计 | permission_admin.py:151 | 授权变更不可追溯（与退款/夜审已落库形成反差） | Phase 3 权限中心重构时一并解决（§8.3 流程） |

## 2. P1（Phase 1-2 处置）

| 编号 | 缺口 | 证据 | 处置归属 |
|---|---|---|---|
| F-05 | OpenAPI 漂移：daily-rates adjust 文档写 change_room，实际执行 inventory.manage_rates | 11-permissions；openapi_schema.py:175 | Phase 4 契约核验 |
| F-06 | 敏感导出审批硬编码角色名（_has_role），绕开权限矩阵 | hotelapp/views.py:320,1466-1470 | Phase 2/3 权限执行点迁移 |
| F-07 | `guests.document_reveal` 孤儿权限点（仅定义，代码用 hotelapp 码） | guests/migrations/0003 | Phase 2 清理（登记后删除或映射） |
| F-08 | reverse_checkout / room_release_ready 权限悬空（唯一检查点在 410 死代码，PMS2 无对应 API） | 11-permissions | Phase 4：front-desk 补 API 或登记退役 |
| F-09 | /permissions/ 矩阵只管辖 hotelapp 域 41 点；billing.refund_*、identity.staff_manage、inventory.manage_rates、core.breakglass_review 矩阵外 | 12-roles 漂移分析 | Phase 2 跨 App RBAC 注册表（核心动因） |
| F-10 | PropertyContextMiddleware 不豁免平台路径：property_context_denied 已积累 38 条、100% 来自超管访问平台面（审计噪声） | 13-identity-sessions | Phase 1 平台域（§6.1.8 明确要求跳过） |
| F-11 | /admin/login/ 无登录限流（PMS /login/ 有 5/5min fail-closed） | 13-identity-sessions | Phase 1 统一登录保护 |

## 3. P2（登记备查）

- 16 个旧 CRUD 权限检查点位于 410 死代码；6 个 delete_* 叠加"无组持有"双失效（Phase 5 旧入口退役时清理）
- User 盘点 management command（任务书 §12.2 / 方案 §12）尚未实现（Phase 1 AUTH-22）
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
