# PMS2 Phase 0 现状审计报告包

> 审计基线：main @ `100a93a1382ca310aae588bc3a1734e4d67e94ba`（工作区 clean）
> 执行方式：基线冻结（工作流 A，主线程）+ 三路并行只读审计（工作流 B/C/D/E/F/G，子智能体）+ 汇总登记（工作流 H）
> 执行日期：2026-11-04
> 依据：《PMS2第一阶段现状审计与权限数据流基线任务书》（只读约束 §2.2 全程遵守：零代码修改、零数据写入）

## 报告索引

| 文件 | 工作流 | 内容 |
|---|---|---|
| [00-baseline_2026-11-10.md](00-baseline_2026-11-10.md) | A | 基线冻结（SHA/环境/版本） |
| [10-routes_2026-11-10.md](10-routes_2026-11-10.md) | B | 全量路由清单：337 条（Admin 191 / API 60 / PMS 页面 28 / 公开面 13 / 旧只读 15 / 410 退役 35） |
| [11-permissions_2026-11-10.md](11-permissions_2026-11-10.md) | C | 全量权限点：263 个（自定义 15 / hotelapp CRUD 44 / PMS2 CRUD 180 / 内建 24），逐点定义/授予/执行位置 |
| [12-roles_2026-11-10.md](12-roles_2026-11-10.md) | D | 九角色授权快照与四层漂移分析（DB 权限零漂移；矩阵页只覆盖 hotelapp 域 41 点） |
| [13-identity-sessions_2026-11-10.md](13-identity-sessions_2026-11-10.md) | F | 账号七分类（26 个）、超管三项不变量核查（0 违规）、双会话边界 |
| [14-dataflows_2026-11-10.md](14-dataflows_2026-11-10.md) | E | 11 条业务数据流追踪（事务边界/写表顺序/审计点/缺口） |
| [15-audit-coverage_2026-11-10.md](15-audit-coverage_2026-11-10.md) | G | 审计事件覆盖：50 个已埋点 action vs §16.1 要求（7 覆盖/6 部分/5 缺失） |
| [20-findings-register_2026-11-10.md](20-findings-register_2026-11-10.md) | H | 缺口登记册：P0×4 / P1×7 / P2×3，含处置归属映射 |

## 关键结论

1. **授权快照健康**：数据库 9 组 96 项权限与迁移声明链零漂移；超管三项不变量零违规。
2. **矩阵页不是完整事实源**：只管辖 hotelapp 域 41 点，5 类跨 App 权限矩阵外——Phase 2 跨 App RBAC 注册表的核心动因成立。
3. **最大审计盲区**：前台核心动作（排房/入住/退房/续住/换房/No-Show）零 DB 审计，仅文件日志。
4. **高危事务边界**：BookingAccessToken 事务外签发（孤儿订单风险）——P0-01。
5. **异常授权账号**：User `test` 无档案持值班经理组 + 直授 user_permissions——待 D10 盘点处置。

## 测试基线

| 引擎 | 命令 | 结果 |
|---|---|---|
| SQLite | `DJANGO_DB_ENGINE=sqlite manage.py test --noinput` | 181 项全过 |
| MySQL 8 | `manage.py test --noinput`（.env） | 181 项全过 |

## Phase 1 准入

按治理方案 §4：Phase 0 证据齐备后进入 Phase 1（平台身份域与 /system/ 拆分）。缺口登记册中 F-01（token 事务边界）建议以紧急安全修复先行处置（须负责人批准）。
