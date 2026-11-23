# PMS 当前状态总览

> 文档编号：GOV-001 ｜ 类型：GOVERNANCE ｜ 状态：ACTIVE ｜ 版本域：跨版本 ｜ 业务模块：文档治理
> **本文件是全仓唯一「当前值」载体**。更新方式：整体重写固定章节，禁止追加节；旧内容靠 Git 历史与《更新日志》追溯。
> 本轮核实基线：main HEAD 3ae016（2026-11-23 现取现核；V4.1 执行终点 9050cc2 及其后续文档同步提交）。当前值以远端 main 现取现核；历史证据仍绑定各自原始 SHA。本次修订依据：PMS 全项目收口任务书 V1.0 TASK-01（需求总账建立 + 基线区分防混淆），总账入口见 GOV-REQ-001。

## 1. 基线与 CI

- **当前基线 `a3ae016`**（V1.0 收口任务书起点；文档头基线=本文件所在提交，逐次现取现核）。提交链（自 `21e04df` 起，择要）：`0d865eb`（type-check）→ `a04ade5`（TASK-01 落库）→ `0c61398`/`9fa3520`（扫描门禁修复+证据）→ `d55b328`/`ade5cca`（V13-04/03）→ `0a3d3ce`（裁定落档）→ `0d67e0b`/`99c020e`（RES-04/05 修复+复验）→ `d30ef14`/`3a85acd`（复算增强）→ `b9c9a4c`（九岗位矩阵）→ `b4d5aa0`（扫描去字面量）→ `8efbfc1`（V3.2 交付记录）→ `efd9453`（资产处置）→ `6b6519e`（PMS2→PMS3 移交+NS-E 裁定）→ `9050cc2`（双引擎 1899/1899）→ `ddb4dea`（状态总览 V4.1 终点）→ `a3ae016`（六链复验+汇总报告）。不得继续使用 `c4c15b6`、`8467012`、`c24b1da`、`1591ce2`、`387e6bf` 或更早 SHA 描述当前 main 状态。
- **CI（现取，2026-11-23，绑定 `9050cc2`）**：六个 job（type-check / secret-scan / docs-lint / test / django-tests / lint）全部 success；`a3ae016` 本体 CI run 以 CI 历史页为准。**secret-scan 为真绿**：工作流 `-o pipefail` 且 `hit_count: 0`，详见 `docs/acceptance/evidence/v3-security-scan-2026-11-22.md` §六 与 `docs/acceptance/evidence/v3-security-scan-history-2026-11-22.md`。CI 通过 ≠ 部署或独立验收。
- **基线区分（防混淆，V1.0 任务书要求）**：①当前 main HEAD=所在提交；②全量测试绑定代码态=`6b6519e`（SQLite 1899 skipped=28 / MySQL 1899 skipped=3，pms2-final-regression-2026-11-22/ 与 9050cc2 归档——后续文档提交不改变该绑定，直至下一次全量回归重绑）；③V3.2 交付记录=`8efbfc1`；④最新文档同步提交=本文件所在提交。文档提交 SHA 不得冒充测试代码基线。
- **需求总账**：全项目历史需求唯一追踪入口=「[PMS全项目历史需求追踪与未完成任务总台账_2026-11-22](PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md)」（GOV-REQ-001）；派发开发任务前必查，防止已完成项重复开发。
- 当前部署形态：**Windows 本地开发/测试/演示/验收**（.env + waitress）。真实生产部署 `WAITING_FOR_SERVER / PRODUCTION PENDING`（等服务器与生产资源具备），不得把本地运行结果冒充生产验收。
- 门禁：`python scripts/docs_link_check.py check` 为本地等效命令，基线冻结于 `docs_gate_baseline.json`。

## 2. OpenAPI 契约

- 权威快照：`docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json`；兼容镜像：`docs/接口文档/openapi.json`。
- **现值（绑定 `b4d5aa0`，2026-11-22 现取现核 + 本地重导出核验）**：OpenAPI **172 paths / 174 HTTP operations**；双快照逐字节一致（SHA-256 均为 `C44EC999…D154`），**1,357,610 字节**；本地 `export_openapi_schema` 重导出哈希不变（导出稳定）。较 2026-11-17 值（168/169）净变更为 V3.0 第一批新增的 walk-in options、前台取消、day-use 转过夜等端点；抽查新增端点（walk-in-options/walk-ins/convert-to-overnight/cancel）权限说明与 4xx/5xx 错误响应定义齐备。
- 守卫：`apps/api/test_openapi_schema.py` 逐字节断言两快照一致；`openapi-snapshot.yml` 负责导出与镜像同步。**两路径冻结，禁止第三份快照**。

## 3. 双引擎测试数

| 证据 | 结论 | 时点限定 |
|---|---|---|
| `docs/acceptance/evidence/v3-sqlite-fullsuite-20261122.log` | Ran **1878**，OK (skipped=28)，exit 0 | V3.0 第一批交付轮（绑定 1b882eb 代码态），SQLite 引擎 |
| `docs/acceptance/evidence/v3-mysql-fullsuite-20261122.log` | Ran **1878**，OK (skipped=3)，exit 0 | 同轮，**MySQL 8 权威**（文件头含错峰登记） |
| `docs/acceptance/evidence/mc11-{sqlite,mysql}-fullsuite-20261121.log` | 1846 / 1849 OK | 历史（11C-001 验收轮，e4599db 链） |

- 最新权威全量为 **1878/1878 双绿**（2026-11-22，绑定 V3.0 第一批代码态 `1b882eb`；净增 32 例=walk-in-options/日租建单/转过夜/前台取消/幂等契约/复算冒烟/夜审员矩阵）。其后 `21e04df`/`0d865eb` 为文档与 type-check；V3.2 增量（`d30ef14`）复算工具新增 15 例专项测试（apps.api.test_verify_report_consistency，SQLite 实测 OK），并入 CI test job（`b4d5aa0` run `45024442610` success）；双引擎数字不替代安全扫描与生产验收。
- **V3.2 TASK-14 验收证据**：`docs/acceptance/evidence/v3-task14-reconcile-devmysql-20261122.log`——dev MySQL DEFAULT 物业 2026-11-16..11-22 真实数据三项核对全 PASS、exit 0，绑定 `d30ef14`。复算逻辑独立实现（不调用 report_center 内部函数），差异输出 name/date/report_value/source_value 结构化 JSON、失败退出码 1。
- 规则：历史报告中的测试数字仅对其各自 SHA 有效（1846/1849/1602/…均为绑定值）；MySQL 全量测试须遵守错峰纪律（同一共享测试环境同一时刻仅一个全量进程，登记执行人/起止/库名/命令/退出码），剥 `DJANGO_PLATFORM_ADMIN_*` 实名环境变量；SQLite 全绿不替代 MySQL 权威（锁/约束/并发以 MySQL 结果为准）。

## 4. MC11 管理中心闭环状态（当前）

- **权威载体**：`docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2_11个管理中心功能闭环总盘点表_2026-11-20.md`（负责人 2026-11-22 裁定后修订版）。
- **当前统计（裁定基线 `21e04df`，V4.1 执行后）**：92 项缺口（90 原始 + V13-02 新登记 2）已关闭 **46** 项；**开放 46 项 = P1 0 + P2/生产待验收/待裁定登记 46**；P0 决策项清零（NA-13 已由负责人批准落地 1b882eb）。**六项 P2 已由负责人 2026-11-22 正式裁定落档**（NS-E-01/02 `APPROVED_NO_CODE_CHANGE`，NS-E-03/04 `APPROVED_PMS3_HANDOFF`，NS-E-05 `APPROVED_WITH_PENDING`，见 `00_项目基线与决策/六项P2待裁定事项正式记录_2026-11-22.md`），**"移交 PMS3"不计入 PMS2 已实现功能**。**RES-04/RES-05 **已于 `0d67e0b` 修复、`99c020e` 复验入账并改 CLOSED**（押金 OTHER 漂移 + 凭证面板重建；浏览器 E2E `docs/acceptance/evidence/res04-05-2026-11-22-10-14/` 6/6 PASS，回归用例 `apps/api/test_center_template_regressions.py` 固化），**MC11 P1 开放清零**。
- **TASK-01 / HK-10 已关闭（`a04ade5`）**：任务池 PENDING「开始」按钮、已指派他人守卫、REJECTED 重开伴随缺陷（服务层此前只收 PENDING，重开必然 409）连同三组浏览器证据正式落库，状态由 OPEN 转 CLOSED / BROWSER VERIFIED。
- V3.0 第一批已验收部分：Day-use 前台建单+转过夜（12/12 PASS）、前台取消 PENDING（5/5 PASS）、任务池 PENDING 开始（3 组 E2E，a04ade5）、报表源数据复算实测 PASS。交付记录 `docs/PMS2.0_当前版本/04_开发实施记录/`（V3.0 第一批）。
- 已关闭项不再以原始 P0/P1 状态计入剩余工作量；生产条件限制类标记 `WAITING_FOR_SERVER`，与代码未实现严格分开。
- 浏览器验收：V13-02 32 项写操作 + 九岗位高危拒绝矩阵 PASS（绑定 `c4c15b6`，其 NA-13 缺口已由 `1b882eb` 授权 + 2026-11-22 九岗位矩阵复跑收口，证据 `docs/acceptance/evidence/task06-nine-role-20261122/`）；维修链（任务书链 B）浏览器 E2E 9/9 PASS（f5aeea1）；V3.0 第一批已验证证据为 v3-task02-03、v3-task04、task01-hk10 三组。

## 5. 验收状态

- **G14 项目验收**：负责人 2026-11-17 复核后代录 **PASS WITH RESERVATIONS**（96 号签署页代录节；保留项：生产库备份恢复、Redis 多 worker、生产部署与 PII 独立密钥切换须环境具备后重做）。负责人 2026-11-22 裁定：该结论**继续有效，不要求第三方独立签字**；但最新基线在 TASK-01 落库复验、TASK-06 九岗位浏览器矩阵、最新 CI 与安全扫描证据、owner 补充确认四项完成前**不得标记最终 PASS**。
- **V1.3 未关闭事项专项总表**：本轮总体结论 **NO-GO**——V13-01 MySQL 孪生账户取证被 DBA 阻塞、生产侧 V13-03/V13-04 等待真实生产输入。G14 历史验收结论与 V1.3 专项 NO-GO 是两个口径，不得混同。
- **V3.0 第一批**：主体交付完成，总体暂定 **PASS WITH RESERVATIONS**；负责人裁定的四项前置已全部闭环——TASK-01 已落库复验（a04ade5）、TASK-06 九岗位矩阵已复跑 PASS（task06-nine-role-20261122）、最新 CI 与安全扫描证据齐备（b4d5aa0 run 45024442610 全绿；工作树扫描 PASS + 历史扫描分类处置见 v3-security-scan-history-2026-11-22.md）；**仅剩 owner 对最新基线的补充确认**（材料已按 V13-08 §十就位），开发自测不替代负责人确认。生产资源缺失仍不计入代码完成率。

## 6. 待负责人裁定项

1. HK-07（工单取消库存语义）、NTF-05（预订四类任务确认入口）、AUD-04（链式哈希演进）、NA-09/10（调度停摆探测与告警）、FIN-06/MB-11（权限码扩展 L3）：登记待裁定（见 MC11 总盘点表 §14）。
2. ~~夜审员岗位权限面（NA-13）~~ **已裁定并落地（2026-11-22 负责人批准，1b882eb）**：授常规夜审查看/预览/执行，不授管理例外。
3. ~~押金 30% 固定口径~~ 已裁定（2026-11-16）；~~day-use 计费语义~~ 已落地（V12-01，9f162de）。

## 7. 文档状态入口表

| 事项 | 唯一入口 |
|---|---|
| MC11 管理中心闭环缺口与整改状态 | `docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS2_11个管理中心功能闭环总盘点表_2026-11-20.md`（TASK-00 修订版：原始发现/当前状态双轨） |
| G14 签署与代录 | `docs/PMS2.0_当前版本/06_验收与发布/96-g14-signoff_2026-11-10.md` |
| V1.3 未关闭事项专项收口 | `docs/acceptance/V1.3_未关闭事项专项收口/V1.3_未关闭事项总表与最终验收报告.md` |
| OpenAPI 契约 | §2 两条快照路径（冻结，禁止第三份） |
| 文档治理规则、类型/效力/编号/元数据、五阶段依据 | `docs/共享规范/PMS项目文档体系重构与知识入口治理实施方案_2026-11-15.md`（GOV-020，重建本） |
| 共享规范 47 个文件的分组导航 | `docs/文档索引.md` §六 |
| 治理目录职责与权威层级 | `docs/共享规范/多AI协作治理文档结构与文件职责.md` |
| 目录导航与索引规则 | `docs/文档索引.md` §一 / §四 / §五 |
| 文档治理验收 | DOC-01~16 已由负责人 2026-11-14 签署通过（`docs/共享规范/文档治理最终验收报告_2026-11-14.md`） |
| 管理中心闭环审查（MC-001~MC-010 首轮静态审查基线） | `docs/PMS2.0_当前版本/05_测试审计与缺陷整改/PMS全项目管理中心功能闭环审查报告_2026-11-17.md` |
| 主题域权威关系与效力裁定 | `docs/共享规范/文档治理阶段二_文档权威关系矩阵_2026-11-14.md` |
| 全仓文件台账 | `docs/共享规范/文档治理阶段一_文档总台账_2026-11-14.md`（f13e32f 时点快照） |
| 新人阅读路线 | `docs/共享规范/文档治理阶段二_新开发人员阅读路线_2026-11-14.md` |
| 变更事件 | `docs/更新日志.md` |
| 自动化门禁 | `scripts/docs_link_check.py` + `docs/共享规范/docs_gate_baseline.json` |
