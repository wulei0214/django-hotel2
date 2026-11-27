# 酒店管理系统（PMS）

> 架构：Django 模块化单体；MySQL 8 为目标数据库，SQLite 用于 CI/轻量测试。
> 当前基线与全部“当前值”（main HEAD / CI / OpenAPI / 测试数 / 遗留门禁）：**[PMS当前状态总览](docs/共享规范/PMS当前状态总览.md)**（唯一当前值载体，整体重写制）。
> 文档权威关系：[权威关系矩阵](docs/共享规范/文档治理阶段二_文档权威关系矩阵_2026-11-14.md)；新人按[阅读路线](docs/共享规范/文档治理阶段二_新开发人员阅读路线_2026-11-14.md)执行。历史白盒审查快照（2026-11-09，仅对其基线有效）：[PMS2 最新代码单元测试与白盒全链路审查报告](docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2最新代码单元测试与白盒全链路审查报告_2026-11-09.md)。

## 1. 当前状态

PMS2 已建立员工身份与任职授权、预订库存、前台履约、财务账单、夜审、清洁维修、审计和报表等主要业务能力。当前应以代码、自动化测试和验收证据分别判断状态，不把“代码存在”写成“用户验收通过”。

| 范围 | 当前代码事实 | 验收边界 |
|---|---|---|
| 认证与授权 | User / StaffProfile / PropertyStaffMembership / StaffRoleAssignment；AuthorizationService；九角色目录 | 九角色真实账号登录和全部权限边界仍需本地验收 |
| 预订与库存 | 公开预订、PENDING Hold、前台排房/入住/退房 | 未预付订单可由前台成功收款/收押金后自动确认 |
| 财务 | Folio、Payment、Refund、LedgerEntry、CashierShift、审批与冲销服务 | 真实银行/支付渠道不是本地人工登记或 Mock 测试 |
| 夜审 | BusinessDate、NightAuditRun、NightAuditService | 营业日、夜审失败恢复和生产定时任务仍按部署验收 |
| 清洁维修 | DIRTY→CLEANING→INSPECTING→CLEAN；维修专项权限 | 客房服务员不得执行净房放行 |
| API | OpenAPI 3.1 静态快照；paths、operations 与 verified/partial 当前值统一以 [PMS当前状态总览](docs/共享规范/PMS当前状态总览.md) §2 为准 | 当前快照由 CI 守卫同步，不能用历史 partial 数量描述现状 |
| 测试 | 双引擎测试数与绑定 SHA **现值以 [PMS当前状态总览](docs/共享规范/PMS当前状态总览.md) §3 为准**（2026-11-28 现取：MySQL / SQLite 各 Ran **2230**，绑 `33fc13c`）；引用测试数必须与 SHA 同时引用，禁止裸引数字 | 不等于真实浏览器 E2E、用户本地库或生产验收 |
| 全链路串测 | scripts/e2e_full_chain.py 已重写（护栏：--i-understand-reset / 目标库白名单 / 凭据注入 / 零硬编码 / 断言补齐） | 2026-11-17 隔离 MySQL 测试库复跑 56 项 / 失败 0（evidence/e2e-rerun-20261116.log，repo_head 1591ce2）；属开发侧证据、未纳入 CI，不替代独立验收 |
| DEC-02-A1 账务调整 | `BillingAdjustment` 模型+服务+API+财务页面（财务发起→值班经理审批→执行生凭证；净额重建） | 开发完成，13/13 测试通过 |
| DEC-02-B2 异常放行 | `CheckoutExceptionRelease` 模型+服务+API+前台引导+财务核查面板（申请→审批→放行不建负债→受控转负债/关闭） | 开发完成，13/13 测试通过 |
| PAY-P0 支付意图 | `PaymentIntent` 模型+`payment_intents.py` 服务（创建/窗口延长/确认成功/过期/事件记录） | 开发完成，10/10 测试通过 |
| PAY-P0 渠道交易 | `ProviderTransaction` 模型（渠道交易号/事件 ID 唯一约束+验签标志） | 开发完成 |
| PAY-P0 补偿队列 | `PaymentCompensationTask` 模型+服务（租约抢占/指数退避/死信/人工重放） | 开发完成，10/10 测试通过 |
| D2 渠道窗口 | 服务端 `CHANNEL_WINDOW_CONFIG`（客户端不可提交/修改窗口或延长上限） | 开发完成 |
| D4 库存差异 | `InventoryReconciliationRun/InventoryDiscrepancy` 模型+`reconciliation.py` 服务（扫描→申请→审批→执行修复→再次对账；SoD 自批拒绝） | 开发完成，6/6 测试通过 |
| FIN-P0 账务对账 | `FolioReconciliationRun/FolioReconciliationDiff` 模型+`reconciliation.py` 服务（Payment↔凭证/Deposit↔凭证/Refund↔凭证/孤儿化检测） | 开发完成，6/6 测试通过 |

## 2. 自动化测试证据

**唯一当前口径**：双引擎测试数与绑定 SHA 一律以 [PMS当前状态总览](docs/共享规范/PMS当前状态总览.md) §3 为准（2026-11-28 现取：MySQL / SQLite 各 Ran **2230**，绑 `33fc13c`）。**禁止裸引数字**——引用测试数必须与 SHA 同时引用。

**证据在哪**

- 各轮全量回归原始日志：`docs/acceptance/evidence/`，按「轮次 + 引擎 + SHA」命名；当前权威轮为 [`g14-final-20261127-r2/mysql-33fc13c.log`](docs/acceptance/evidence/g14-final-20261127-r2/mysql-33fc13c.log) 与 [同轮 SQLite 版](docs/acceptance/evidence/g14-final-20261127-r2/sqlite-33fc13c.log)。
- CI 运行记录：<https://github.com/wulei0214/django-hotel2/actions>（历史 run 各自绑定其当时 SHA）。
- 逐轮变更明细：[docs/更新日志.md](docs/更新日志.md)。

**历史数字纪律**：887 / 948 / 1307 / 1598 / 1602 / 1878 等均为各自证据头 SHA 的**时点值**，仅对其基线有效；不得挪用到其他基线，不得用于描述现状。逐轮记录见 [docs/更新日志.md](docs/更新日志.md) 与 `docs/acceptance/evidence/`。

**已知边界（不可省略）**

- 两条 CI **均不执行** `scripts/e2e_full_chain.py`。该脚本 2026-11-16 重写后在隔离 MySQL 测试库复跑 56 项 / 失败 0（[`e2e-rerun-20261116.log`](docs/acceptance/evidence/e2e-rerun-20261116.log)，repo_head 1591ce2），属**开发侧证据**，不等于 CI 化串测，也不等于独立验收。
- 自动化测试通过 ≠ 真实浏览器 E2E ≠ 用户本地库验收 ≠ 生产部署验收。
- MySQL 全量回归须遵守错峰与串行纪律，见 [AGENTS.md](AGENTS.md)「会话纪律」。

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
- [系统架构说明](docs/共享规范/系统架构说明_2026-11-10.md)
- [PMS 业务流程与名词说明](docs/共享规范/PMS业务流程与名词说明_2026-11-10.md)
- [角色权限验证指南](docs/共享规范/角色权限验证指南_2026-11-10.md)
- [PMS2 API 接口与测试人员指南](docs/PMS2.0_当前版本/03_API接口与前后端规范/PMS2接口文档与测试人员使用指南_2026-11-10.md)
- [PMS2 最终验收标准](docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2.0最终开发完成与上线验收标准_2026-11-10.md)
- [最新单元测试与白盒审查报告](docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2最新代码单元测试与白盒全链路审查报告_2026-11-09.md)
- [PMS3.0 最终方案 V2.3](docs/PMS3.0_后续规划/00_总体规划基线/PMS3.0全项目开发最终方案V2.3_2026-11-10.md)
- 强制规范编号库（**STD-01～STD-06**）与全部文档分组/效力状态：[文档索引](docs/文档索引.md) §六

## 7. 验收声明

历史 CI 通过（如 run #900/#874 的 887 项）是各自 SHA 时点的自动化回归证据，不代表 PMS2 最终验收通过；当前自动化证据规模见 [PMS当前状态总览](docs/共享规范/PMS当前状态总览.md) §3。……OpenAPI 25 个 partial 已全部收敛 verified（2026-11-16，verified=170/partial=0）；九角色真实登录与核心业务走查已有开发侧走查证据（docs/acceptance/evidence/walkthrough-g14-dev-side-20261116.md），G14 已由项目负责人本人逐项复核后于 2026-11-17 代录“通过”（96 号签署页【2026-11-17 负责人验收代录节】，独立第三方验收人未另行指派）。真实浏览器 E2E、独立第三方签署（如需）与 MySQL 目标环境部署验收仍需完成。PMS3 启动门禁状态以 96 号代录结论与 [PMS当前状态总览](docs/共享规范/PMS当前状态总览.md) 为准。

## 8. 文档入口与阅读路线

- 当前状态唯一入口：[PMS当前状态总览](docs/共享规范/PMS当前状态总览.md)
- 主题权威关系：[权威关系矩阵](docs/共享规范/文档治理阶段二_文档权威关系矩阵_2026-11-14.md)
- 新人阅读路线：[阅读路线](docs/共享规范/文档治理阶段二_新开发人员阅读路线_2026-11-14.md)
- 全仓导航与强制规范编号库：[文档索引](docs/文档索引.md) · 重复/过期/冲突处置裁定：[GOV-DISPOSE-001](docs/文档处置清单_重复过期冲突_2026-11-27.md)
- 验收资产（**只读**）：[验收资产索引](docs/共享规范/验收资产索引_2026-11-27.md)（113 篇 md + 55 个证据轮次）

## 9. 目录结构说明

> 是否入库以 `git ls-files` 实测为准（2026-11-27）。**gitignored 目录是本机产物，不入库、也不得被引用为验收证据路径。**

**入库（提交进版本库）**

| 路径 | 用途 | 已跟踪文件 |
|---|---|---|
| `ARCHITECTURE.md` | 架构宪法，最高约束（修改属 L3 架构变更） | 1 |
| `AGENTS.md` | AI 协作规则入口：硬红线 + 规则导航（指向 STD-01～06） | 1 |
| `README.md` | 项目总览与阅读起点（本文） | 1 |
| `apps/` | Django 业务模块：认证授权、预订库存、前台履约、财务账单、夜审、清洁维修、审计报表 | 402 |
| `hotel/` | Django 项目配置（settings / urls / wsgi / asgi） | 4 |
| `hotelapp/` | 早期单体 app；已退役 410 入口的旧写体保留区（不得在其上新增功能） | 40 |
| `templates/` | Django 模板（`templates/pms/` 为业务中心版式基准） | 29 |
| `static/` | 静态资源源文件 | 162 |
| `scripts/` | 工具脚本：文档门禁 `docs_link_check.py`、取证、E2E 串测、mypy 基线 | 33 |
| `docs/` | 全部文档；结构与效力分组见 [文档索引](docs/文档索引.md) §六 | 498 |
| `manage.py`、`pyproject.toml`、`requirements.txt`、`requirements-dev.txt`、`mypy-baseline.txt` | 工程与工具链配置 | 5 |

**不入库（gitignored，本机运行产物）**

| 路径 | 用途 |
|---|---|
| `staticfiles/` | `collectstatic` 收集产物 |
| `logs/`、`reports/` | 本机运行日志与报告 |
| `backups/` | 数据库/文件备份 |
| `understand-out/` | 代码分析工具输出 |
| `_wbi_*`、`__pycache__/` | AI/工具缓存与字节码 |

**`docs/` 之下**：强制规范集中在 [文档索引](docs/文档索引.md) §六-A1（编号 **STD-01～STD-06**）；历史件已原位标记 `ARCHIVED`、不作开发依据；`docs/acceptance/` 为**不可变审计资产**（只读，不移动、不改写）。
