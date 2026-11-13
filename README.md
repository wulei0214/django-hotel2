# 酒店管理系统（PMS）

> 架构：Django 模块化单体；MySQL 8 为目标数据库，SQLite 用于 CI/轻量测试。
> 当前业务代码审查基线：main@8e310b5a8491f31dd91b5461160fd6dfc1f22e06（2026-11-09）。
> 当前文档审查报告：[PMS2 最新代码单元测试与白盒全链路审查报告](docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2最新代码单元测试与白盒全链路审查报告_2026-11-09.md)。

## 1. 当前状态

PMS2 已建立员工身份与任职授权、预订库存、前台履约、财务账单、夜审、清洁维修、审计和报表等主要业务能力。当前应以代码、自动化测试和验收证据分别判断状态，不把“代码存在”写成“用户验收通过”。

| 范围 | 当前代码事实 | 验收边界 |
|---|---|---|
| 认证与授权 | User / StaffProfile / PropertyStaffMembership / StaffRoleAssignment；AuthorizationService；九角色目录 | 九角色真实账号登录和全部权限边界仍需本地验收 |
| 预订与库存 | 公开预订、PENDING Hold、前台排房/入住/退房 | 未预付订单可由前台成功收款/收押金后自动确认 |
| 财务 | Folio、Payment、Refund、LedgerEntry、CashierShift、审批与冲销服务 | 真实银行/支付渠道不是本地人工登记或 Mock 测试 |
| 夜审 | BusinessDate、NightAuditRun、NightAuditService | 营业日、夜审失败恢复和生产定时任务仍按部署验收 |
| 清洁维修 | DIRTY→CLEANING→INSPECTING→CLEAN；维修专项权限 | 客房服务员不得执行净房放行 |
| API | OpenAPI 3.1 静态快照 116 paths / 118 operations（verified 为主，个别 partial） | partial 接口仍需逐项真实执行验证 |
| 测试 | 双引擎全量 SQLite 1307 OK（skipped=25）/ MySQL 1307 OK（0 跳过，2026-11-14 本地证据，证据头含 SHA/环境/命令/退出码，见 evidence/test-{sqlite,mysql}-walkin-round.log）；历史 1201=round34 基线 a317ca2 时代，仅对其 SHA 有效 | 不等于真实浏览器 E2E、用户本地库或生产验收 |
| 全链路串测 | scripts/e2e_full_chain.py 已提交 | 当前脚本存在导入缺失、空断言、凭据硬编码和清库护栏不足；36/36 尚不作为有效验收结论 |
| DEC-02-A1 账务调整 | `BillingAdjustment` 模型+服务+API+财务页面（财务发起→值班经理审批→执行生凭证；净额重建） | 开发完成，13/13 测试通过 |
| DEC-02-B2 异常放行 | `CheckoutExceptionRelease` 模型+服务+API+前台引导+财务核查面板（申请→审批→放行不建负债→受控转负债/关闭） | 开发完成，13/13 测试通过 |
| PAY-P0 支付意图 | `PaymentIntent` 模型+`payment_intents.py` 服务（创建/窗口延长/确认成功/过期/事件记录） | 开发完成，10/10 测试通过 |
| PAY-P0 渠道交易 | `ProviderTransaction` 模型（渠道交易号/事件 ID 唯一约束+验签标志） | 开发完成 |
| PAY-P0 补偿队列 | `PaymentCompensationTask` 模型+服务（租约抢占/指数退避/死信/人工重放） | 开发完成，10/10 测试通过 |
| D2 渠道窗口 | 服务端 `CHANNEL_WINDOW_CONFIG`（客户端不可提交/修改窗口或延长上限） | 开发完成 |
| D4 库存差异 | `InventoryReconciliationRun/InventoryDiscrepancy` 模型+`reconciliation.py` 服务（扫描→申请→审批→执行修复→再次对账；SoD 自批拒绝） | 开发完成，6/6 测试通过 |
| FIN-P0 账务对账 | `FolioReconciliationRun/FolioReconciliationDiff` 模型+`reconciliation.py` 服务（Payment↔凭证/Deposit↔凭证/Refund↔凭证/孤儿化检测） | 开发完成，6/6 测试通过 |

## 2. 最新自动化测试证据

- [Django CI / SQLite（run #900）](https://github.com/wulei0214/django-hotel2/actions/runs/42500994999)：887 tests，OK；System Check 和 migration drift check 成功。
- [tests CI / MySQL 8（run #874）](https://github.com/wulei0214/django-hotel2/actions/runs/42500995030)：MySQL 8.0.46，887 tests，OK；迁移和 MySQL append-only guards 安装/校验成功。
- MySQL 本地记录保留一次 1 error 的首次运行和随后 887 OK 的复跑，不删除失败证据。
- 【2026-11-14】历史 run（#900/#874，887 项）仅对其当时 SHA 有效；当前双引擎本地全量为 **SQLite 1303 OK（skipped=24，`docs/acceptance/evidence/test-sqlite-walkin-round.log` 前身 + 本次重跑）与 MySQL 结果见下方证据行**。测试总数随主线持续演进，各历史报告中的数字不得挪用到其他基线。
- 【2026-11-11】白盒修复回归（MySQL 8 本地，948 tests）：第一轮 12 failures + 5 errors（证据保留 `docs/acceptance/evidence/test-mysql-wb-fix.log`）；定位三类根因并修复——①`calculate_early_departure` 无政策/NONE 语义改为按合同金额全额结算（同日退房也必须入账房费，与退房/负债基线一致）；②押金 30% 强制策略的 3 个旧测试期望同步（180=600×30%、CR-04 退款额 90、e2e 上限改由第 4 笔触发）；③WB-011 测试补传 `confirmed_payment`。复跑 7 个受影响模块 139 项 OK 后全量复跑 **948 tests OK（expected failures=1）**（`docs/acceptance/evidence/test-mysql-wb-fix3.log`）；中间轮 2 errors 系本机跑套件漏带 `DJANGO_ENV=test`（runner 强制 DEBUG=False 导致 reconcile_test_users 守卫拒绝测试库），非代码缺陷（`test-mysql-wb-fix2.log` 留证）。
- 【2026-11-11】白盒整改第二批（6 项功能修复 + 对账口径修正，测试 948→960 项）：
  - **WB-037 工号自动编号接入**：`create_employee` 空工号时按部门代码（FO/HK/FIN/MG/ENG）走 DEC-EMP-02 原子编号；员工中心表单新增部门代码选择；岗位代码恰为受控部门代码时自动映射。
  - **WB-044 旧未到入口退役**：`/no_show_one/` 直写入口改 410 Gone（AR-01 收口），统一走 `POST /api/v1/front-desk/reservation-rooms/<id>/no-show`。
  - **WB-055 退房自动生成清洁任务**：退房同事务创建 DEPARTURE 清洁任务（此前 HousekeepingTask 模型无创建方，房态置 DIRTY 但任务断链）+ `housekeeping.task_created` 审计；重复退房幂等。
  - **WB-056 清洁任务与房态链闭环**：净房放行（→CLEAN）联动 DONE→INSPECTED（记录检查人）、未开工任务→CANCELLED；检查返工（INSPECTING→DIRTY）联动 DONE→REJECTED；各落审计。
  - **WB-049~051 异常放行金额绑定**：申请金额必须>0 且≤当前负余额（杜绝超额放行）；审批审计绑定放行金额+审批时点余额快照；放行消费不覆盖已批金额（受阻金额超过已批额维持阻断）；财务转负债按核查时点实际负余额建账，超过已批额拒绝。
  - **WB-022~026 库存对账口径修正**：扫描窗口按物业时区取日；sold 重算按明细行计数（多房预订不再按 distinct 预订低估）；排除已未到明细行；判定口径修正——held/sold 不一致必报（运算符优先级 bug）、total_rooms 按容量语义判超卖（不再与占用和做相等比较）。
  - 回归证据：`docs/acceptance/evidence/test-mysql-wb-batch2.log`。
- 【2026-11-11】白盒整改第三批（审查方第 14~22 轮 WB-067~096，5 路并行子代理修复，测试 960→1020 项）：
  - **支付意图域加固**：WB-067 补偿任务完成强制校验租约所有权+有效期（空值不再绕过）；WB-069/071 create_intent 幂等指纹补齐 currency/purpose/provider_code 且币种必须与 Folio/预订一致；WB-072 confirm_intent_success 校验 Payment 完整归属（物业/预订/账单/金额/币种/状态/未被占用）；WB-073 库存确认失败同事务创建 HOLD_COORDINATION 补偿任务；WB-076/089 渠道事件首次写入校验渠道/金额/币种/验签；WB-077/090 过期服务校验窗口到期。
  - **补偿 Worker 与公开支付接入**：WB-070/078 新增 `payment_compensation_worker` 管理命令（租约领取→分派→完成→退避→死信重放）；WB-087 迟到成功同事务创建持久化补偿任务；WB-088 公开支付意图/回调同事务接入 PaymentIntent+ProviderTransaction（响应契约零改动）。
  - **夜审与账本写入守卫**：WB-079 post_entry 拒写已关账营业日（渠道资金事实豁免，posted_by=None 且 PAYMENT）；WB-080 夜审仅限当前营业日，拒绝未来/历史/任意日期；WB-081 stay_count 按过租窗口同口径；WB-082 already_posted 升级为内容一致校验（folio/stay/金额/币种/营业日不符→critical 阻断）；WB-083 LedgerEntry 实例级 save() 只追加守卫。
  - **续住与退房闭环**：WB-074 walk-in 占位号按幂等键确定性派生（uuid5）+ 房间查询物业隔离 + 幂等重放短路；WB-075 缺逐晚房价快照禁止自动结算（与夜审 critical 同口径，NONE=合同全额基线不动）；WB-084 续住同事务逐晚过账房费凭证（退房归集口径补第三项防双记）；WB-085 续住锁物理房间行（锁序与排房一致）；WB-086 提前退房释放未住晚 sold 库存（历史已住晚保留）。
  - **财务凭证/对账/班次**：WB-091 REFUND_INCREASE 补独立调整凭证（不再只有负债变更无凭证）；WB-093 负债回增清空 settled_at（审计保留原结清时间）；WB-092/096 对账补齐——原始收款凭证覆盖退款中/部分退/全退状态、已关闭 Folio 余额重建、负债净额重建、调整缺凭证检测（新差异类型 ADJUSTMENT_NO_ENTRY）；WB-091-旧 执行参考号物业内唯一（服务层查重双库生效+DB 部分唯一索引）；WB-092-旧 班次现金支出模型层余额校验；WB-095 移除不可达 RECONCILED 状态。
  - **双 CI 漂移修复**：rehire 条件约束与退款参考号 DB 约束测试改 `skipUnless(supports_partial_indexes)`（SQLite 必跑、MySQL 按能力跳过，替代统一 expectedFailure 造成 SQLite unexpected success 假失败）。
  - 迁移：billing.0027（班次状态 choices/对账差异类型/退款参考号部分唯一约束）。回归证据：`docs/acceptance/evidence/test-mysql-wb-batch3.log`。
- 【2026-11-13】第三路全面审查（P1×4/P2×12/P3×9）对 HEAD 93f23cc 复核后收口：26 项中 22 项已由并行轮次在 HEAD 修复（FIN-01/02/03/04、RSV-01/02/03/06/07/08/10/11、SEC-01/02、MAINT-02/02b/03/05/06/07/09，逐项四态核verified），本轮修复剩余 4 项：
  - **MAINT-01**：hotelapp/views.py 32 个 410 退役入口的 747 行不可达旧写体删除（AST 定位行手术，36 个 410 返回完好，hotelapp 全绿）。
  - **PERF-01**：夜审 preview 批量预取主账单与既有凭证（2N+1→固定 3 条查询），语义与原 .first() 一致（含 Folio -opened_at 排序口径）。
  - **RSV-05**：`create_walk_in` 入住/离店日期改物业时区业务日（与夜审/提前离店同口径）+ 弃用标注（生产零调用，供后续 API 接线复用，禁止零散重写）。
  - **MAINT-04**：公开端点（availability/reserve/详情/取消/支付意图/回调）25 处扁平 `{"error": "字符串"}` 收敛为 `{error:{code,message},meta}`；`ApiError` 增补 METHOD_NOT_ALLOWED/SERVICE_UNAVAILABLE/PROPERTY_UNAVAILABLE 三码（与 openapi 声明词汇对齐）；ErrorDetail 词汇表更新并重导 openapi.json 快照（113 paths）；6 处测试断言同步。
  - 回归证据：`docs/acceptance/evidence/test-sqlite-round34.log`（SQLite 全量）、`test-mysql-round34.log`（MySQL 全量）。

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
- [系统架构说明](docs/共享规范/系统架构说明_2026-11-10.md)
- [PMS 业务流程与名词说明](docs/共享规范/PMS业务流程与名词说明_2026-11-10.md)
- [角色权限验证指南](docs/共享规范/角色权限验证指南_2026-11-10.md)
- [PMS2 API 接口与测试人员指南](docs/PMS2.0_当前版本/03_API接口与前后端规范/PMS2接口文档与测试人员使用指南_2026-11-10.md)
- [PMS2 最终验收标准](docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2.0最终开发完成与上线验收标准_2026-11-10.md)
- [最新单元测试与白盒审查报告](docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2最新代码单元测试与白盒全链路审查报告_2026-11-09.md)
- [PMS3.0 最终方案 V2.3](docs/PMS3.0_后续规划/00_总体规划基线/PMS3.0全项目开发最终方案V2.3_2026-11-10.md)

- [docs Markdown 全目录清单与维护状态（78 篇）](docs/共享规范/Markdown文档清单与维护状态_2026-11-09.md)

## 7. 验收声明

887 项 SQLite/MySQL CI 通过是自动化回归证据，不代表 PMS2 最终验收通过。九角色真实登录、核心业务操作走查、真实浏览器 E2E、OpenAPI partial 接口关闭和 G14 独立签署仍需完成。PMS3 正式业务代码开发继续受 PMS2 最终验收门禁约束。
