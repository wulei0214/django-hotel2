# PMS 当前状态总览

> 文档编号：GOV-001 ｜ 类型：GOVERNANCE ｜ 状态：ACTIVE ｜ 版本域：跨版本 ｜ 业务模块：文档治理
> **本文件是全仓唯一「当前值」载体**。更新方式：整体重写固定章节，禁止追加节；旧内容靠 Git 历史与《更新日志》追溯。
> 本轮核实基线：代码事实锚点 main@98673c9（2026-11-15 现取现核，工作区文件与 Git 索引双向核对）。其后纯文档提交不改变代码、OpenAPI 与测试结论；本文件不记录自身提交 SHA，避免自指漂移。

## 1. 基线与 CI

- 业务代码基线 `baf686d`（R1-01~R1-05 Walk-in 幂等仲裁 / 押金强制 / 死锁重试 / 证据元数据整改）。其上为文档治理阶段三~五批次链、DOC-16 签署 `0796a9d`、docs-lint 首跑修复与 C5 基线登记（后者即 `3fd3c0d`），以及公开面错误结构契约-运行时收敛 `98673c9`。
- **代码事实锚点 `98673c9`**：最后一个含代码/契约变更的提交。其上的提交均为纯 `docs/` 变更，不改变本文件 §2/§3 的任何实测结论。
- 2026-11-15 核实时：远端锚点 origin/main = `3fd3c0d`；`98673c9` 及其后的文档提交尚未推送，故没有对应 CI run。工作区另有 1 个未跟踪的本机日志文件，未入库、不作验收证据。
- CI：Django run 43242839234、MySQL run 43242839197 为 2026-11-12 的**历史时点**事实，仅对各自当时 SHA 有效；docs-lint 作业已上线并纳入 django-ci.yml。CI 通过 ≠ 部署或独立验收。
- 门禁：`python scripts/docs_link_check.py check` 为本地等效命令，基线冻结于 `docs_gate_baseline.json`。

## 2. OpenAPI 契约

- 权威快照：`docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json`；兼容镜像：`docs/接口文档/openapi.json`。
- 现值（绑定 `b055666`，2026-11-16 SYS-06/G14-11 收敛）：OpenAPI 3.1.0，**118 paths / 120 operations**；操作级 `x-contract-status`：**verified 120 / partial 0**（原 25 个 partial 已逐项验证后翻转，验证模块 89 项 + 契约 43 项回归证据 `docs/acceptance/evidence/docgate-partial-verification-20261116.log`）。
- 双快照逐字节一致：**973202 字节**，SHA256 前 16 位 `dbd5f9651cd5d2f6`（绑定 `b055666` 实测；fd 封套补可选 meta、RateCalendarData 补双集合分页键、财务汇总补 P0-07 metadata）。历史值按整体重写制移除，追溯走 Git 与更新日志。
- 守卫：`apps/api/test_openapi_schema.py` 以 `read_bytes()` 逐字节断言两快照一致；`openapi-snapshot.yml` 负责导出与镜像同步。**两路径冻结，禁止第三份快照**。
- 最近契约实跑：`docs/acceptance/evidence/docgate-publicapierror-convergence.log`（43 tests OK，环境 DJANGO_ENV=test + SQLite，SHA `3fd3c0d`）。公开面错误结构契约-运行时分歧已在该轮收敛，相关契约测试恢复作为有效验收依据。

## 3. 双引擎测试数

| 证据 | 结论 | 时点限定 |
|---|---|---|
| `docs/acceptance/evidence/test-sqlite-walkin-round.log` | Ran 1307，OK (skipped=25) | 仅对该轮证据头登记的环境与命令有效 |
| `docs/acceptance/evidence/test-mysql-walkin-round.log` | Ran 1307，OK | 同上 |
| `docs/acceptance/evidence/docgate-publicapierror-convergence.log` | Ran 43，OK | 契约套件，SHA `3fd3c0d` |

- **双引擎全量回归（2026-11-16 重跑，G14-01/02 前置证据）**：SQLite **1558 tests OK（skipped=26）**（`docs/acceptance/evidence/test-sqlite-g14pre-20261116.log`）；MySQL 首轮因陈旧 keepdb 测试库与 .env 泄漏两度中断，已修复证据生成器（`--noinput` 全新建库 + 环境变量凭据 + 被测树绑定）并重跑中，结果回填本行。walkin-round 旧数字仅作轮次记录。
- 规则：历史报告中的测试数字仅对其各自 SHA 有效；readme 历史段落的 887/948/…/1285 均为绑定值，不得挪用到其他基线。

## 4. 遗留门禁（截至本轮）

| 遗留 | 状态 |
|---|---|
| 25 个 partial operation 逐项真实执行验证 | 未完成 |
| 真实浏览器 E2E、九角色真实登录走查、Apifox 手工执行 | 未完成 |
| G14 独立签署 | `docs/PMS2.0_当前版本/06_验收与发布/96-g14-signoff_2026-11-10.md` 复核清单与结论栏仍为空白，待独立验收负责人填写 |
| dev 库 hotel 迁移（guests.0007、billing.0030） | 上线前应用 + 核对；本轮未连接该库 |
| 生产 PII 独立密钥受控切换 | 按 07 目录 PII 手册流程，禁止直换环境变量 |
| WB-015~045、WB-068、WB-113、WB-46-01~13 原始映射找回 | 待办 |
| 双引擎全量回归 | SQLite 1558 OK 已重跑；MySQL 重跑中（陈旧 keepdb 库与 .env 泄漏已修复） |
| `scripts/test_evidence.py` 凭据与证据树绑定 | **已修复（`60481e9`）**：凭据入环境变量（缺失快速失败）、`--noinput` 全新建库、被测树绑定检测 |

## 5. 待负责人裁定项

1. ~~押金：固定 30% vs 配置化~~ **已裁定（2026-11-16 负责人）**：维持服务端固定 30% 为正式业务口径；DepositPolicy 为未来预留、配置值不消费；切换须另行审批。裁定记录见六项规则基线追加节与映射表 NS-A DEC-03 行。
2. ~~NS-C 与代码域 DEC-18 / DEC-29 重号~~ **已消解（2026-11-16）**：代码域两项登记为 **NS-D DEC-D-01（缓存韧性）/ DEC-D-02（容量观察）**，历史映射见 11-14 映射表；billing_center 注释改名随其在途批次收口同步。
3. **阶段四一致性整改**中无法由开发侧决定的业务口径，逐项上报。

已裁定与已收敛事项（历史追溯走 Git 与更新日志）：TD-1「DEC-16 批准与签署关系」已由负责人 2026-11-14 裁定；公开面错误结构分歧已于 `98673c9` 收敛；文档治理 DOC-16 已签署通过。

## 6. 文档状态入口表

| 事项 | 唯一入口 |
|---|---|
| 文档治理规则、类型/效力/编号/元数据、五阶段依据 | `docs/共享规范/PMS项目文档体系重构与知识入口治理实施方案_2026-11-15.md`（GOV-020，重建本） |
| 共享规范 47 个文件的分组导航 | `docs/文档索引.md` §六 |
| 治理目录职责与权威层级 | `docs/共享规范/多AI协作治理文档结构与文件职责.md` |
| 目录导航与索引规则 | `docs/文档索引.md` §一 / §四 / §五 |
| 文档治理验收 | DOC-01~16 已由负责人 2026-11-14 签署通过（`docs/共享规范/文档治理最终验收报告_2026-11-14.md`） |
| 主题域权威关系与效力裁定 | `docs/共享规范/文档治理阶段二_文档权威关系矩阵_2026-11-14.md` |
| 全仓文件台账 | `docs/共享规范/文档治理阶段一_文档总台账_2026-11-14.md`（f13e32f 时点快照） |
| 新人阅读路线 | `docs/共享规范/文档治理阶段二_新开发人员阅读路线_2026-11-14.md` |
| 一致性校准历史 | `docs/共享规范/docs全量一致性校准报告_2026-11-12.md`（历史快照） |
| 变更事件 | `docs/更新日志.md` |
| 自动化门禁 | `scripts/docs_link_check.py` + `docs/共享规范/docs_gate_baseline.json` |
