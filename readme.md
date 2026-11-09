# 酒店管理系统（PMS）

> 架构：Django 模块化单体；MySQL 8 为目标数据库，SQLite 用于 CI/轻量测试。
> 当前业务代码审查基线：main@8e310b5a8491f31dd91b5461160fd6dfc1f22e06（2026-11-09）。
> 当前文档审查报告：[PMS2 最新代码单元测试与白盒全链路审查报告](docs/PMS2最新代码单元测试与白盒全链路审查报告_2026-11-09.md)。

## 1. 当前状态

PMS2 已建立员工身份与任职授权、预订库存、前台履约、财务账单、夜审、清洁维修、审计和报表等主要业务能力。当前应以代码、自动化测试和验收证据分别判断状态，不把“代码存在”写成“用户验收通过”。

| 范围 | 当前代码事实 | 验收边界 |
|---|---|---|
| 认证与授权 | User / StaffProfile / PropertyStaffMembership / StaffRoleAssignment；AuthorizationService；九角色目录 | 九角色真实账号登录和全部权限边界仍需本地验收 |
| 预订与库存 | 公开预订、PENDING Hold、前台排房/入住/退房 | 未预付订单可由前台成功收款/收押金后自动确认 |
| 财务 | Folio、Payment、Refund、LedgerEntry、CashierShift、审批与冲销服务 | 真实银行/支付渠道不是本地人工登记或 Mock 测试 |
| 夜审 | BusinessDate、NightAuditRun、NightAuditService | 营业日、夜审失败恢复和生产定时任务仍按部署验收 |
| 清洁维修 | DIRTY→CLEANING→INSPECTING→CLEAN；维修专项权限 | 客房服务员不得执行净房放行 |
| API | OpenAPI 3.1 静态快照 113 paths / 114 operations；全部 verified | partial 接口仍需逐项真实执行验证 |
| 测试 | 最新 SQLite 与 MySQL 8 CI 各 887→919 项通过（含 DEC-02 新增 13 项 + OpenAPI 同步调整） | 不等于真实浏览器 E2E、用户本地库或生产验收 |
| 全链路串测 | scripts/e2e_full_chain.py 已提交 | 当前脚本存在导入缺失、空断言、凭据硬编码和清库护栏不足；36/36 尚不作为有效验收结论 |
| DEC-02-A1 账务调整 | `BillingAdjustment` 模型+服务+API+财务页面（财务发起→值班经理审批→执行生凭证；净额重建） | 开发完成，13/13 测试通过 |
| DEC-02-B2 异常放行 | `CheckoutExceptionRelease` 模型+服务+API+前台引导+财务核查面板（申请→审批→放行不建负债→受控转负债/关闭） | 开发完成，13/13 测试通过 |
| PAY-P0 支付意图 | `PaymentIntent` 模型+`payment_intents.py` 服务（创建/窗口延长/确认成功/过期/事件记录） | 开发完成，10/10 测试通过 |
| PAY-P0 渠道交易 | `ProviderTransaction` 模型（渠道交易号/事件 ID 唯一约束+验签标志） | 开发完成 |
| PAY-P0 补偿队列 | `PaymentCompensationTask` 模型+服务（租约抢占/指数退避/死信/人工重放） | 开发完成，10/10 测试通过 |
| D2 渠道窗口 | 服务端 `CHANNEL_WINDOW_CONFIG`（客户端不可提交/修改窗口或延长上限） | 开发完成 |

## 2. 最新自动化测试证据

- [Django CI / SQLite（run #900）](https://github.com/wulei0214/django-hotel2/actions/runs/42500994999)：887 tests，OK；System Check 和 migration drift check 成功。
- [tests CI / MySQL 8（run #874）](https://github.com/wulei0214/django-hotel2/actions/runs/42500995030)：MySQL 8.0.46，887 tests，OK；迁移和 MySQL append-only guards 安装/校验成功。
- MySQL 本地记录保留一次 1 error 的首次运行和随后 887 OK 的复跑，不删除失败证据。

**注意：** 两条 CI 没有执行 scripts/e2e_full_chain.py。在脚本修复并于隔离 MySQL 数据库重新运行前，不得把提交标题中的 36/36 当成已验证结果。

## 3. 核心业务链路

公开预订（不预付）→ Reservation=PENDING → 前台收取押金/人工收款 → Payment 成功、库存确认、Reservation=CONFIRMED → 排房入住 → 夜审按营业日过账房费 → 退房时按 Stay 补记未入账房费并结清 → 客房清洁 → 值班经理/店长净房放行 → 退款申请/审批/执行三方分离。

退款三方角色示例：
- 前台/财务有申请权限；
- 值班经理/店长有审批权限；
- 财务有执行权限；
- 同一笔退款的申请人、审批人、执行人必须是三个不同账号。

## 4. 九角色测试账号

专用账号：test_owner、test_front、test_finance、test_duty、test_housekeeping、test_nightaudit、test_fnb、test_fnbsup、test_parking。

当前注册表只有店长、前台、财务、值班经理、客房服务员五个 ACTIVE 角色；夜审员、餐饮服务员、餐饮主管、停车安保四个为 PLANNED 空组（0 权限）。PLANNED 账号可用于认证身份链验证，但不得被当作已启用业务角色。

初始化命令通过隐藏交互输入密码并执行 Django 密码校验。**不得把测试密码、Session、Token 或住客敏感数据提交到仓库。** 当前全链路脚本存在硬编码测试凭据，应先轮换专用测试账号密码，再使用安全注入方式。

## 5. 本地开发与测试

~~~~powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --noinput -v 2
python manage.py seed_role_test_accounts
~~~~

破坏性测试必须使用独立测试库；禁止对生产库、未知数据库或含真实住客数据的开发库执行清库脚本。

## 6. 主要文档

- [文档索引](docs/文档索引.md)
- [系统架构说明](docs/系统架构说明.md)
- [PMS 业务流程与名词说明](docs/PMS业务流程与名词说明.md)
- [角色权限验证指南](docs/角色权限验证指南.md)
- [PMS2 API 接口与测试人员指南](docs/PMS2接口文档与测试人员使用指南.md)
- [PMS2 最终验收标准](docs/PMS2.0最终开发完成与上线验收标准.md)
- [最新单元测试与白盒审查报告](docs/PMS2最新代码单元测试与白盒全链路审查报告_2026-11-09.md)
- [PMS3.0 最终方案 V2.3](docs/PMS3.0全项目开发最终方案V2.3.md)

- [docs Markdown 全目录清单与维护状态（64 篇）](docs/Markdown文档清单与维护状态_2026-11-09.md)

## 7. 验收声明

887 项 SQLite/MySQL CI 通过是自动化回归证据，不代表 PMS2 最终验收通过。九角色真实登录、核心业务操作走查、真实浏览器 E2E、OpenAPI partial 接口关闭和 G14 独立签署仍需完成。PMS3 正式业务代码开发继续受 PMS2 最终验收门禁约束。
