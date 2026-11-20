> 文档编号：R6-08 ｜ 类型：DUAL-ENGINE-REGRESSION ｜ 状态：CURRENT ｜ 版本域：PMS2.0 当前版本 ｜ 业务模块：MySQL/SQLite 双引擎回归

# R6-08 MySQL 与 SQLite 双引擎回归报告

| 元信息 | 值 |
| --- | --- |
| 任务书依据 | §八 双数据库质量回归 + §3.3 MySQL 错峰纪律 |
| 被测树 | `fc62c79bd19ad880a8aef87245bba40909b74a09`（最新 main，隔离 worktree 干净树） |
| 权威口径 | MySQL 为锁/约束/并发/触发器的最终权威；SQLite 为快口径（CI test job + 本地迭代），不替代 MySQL |
| 独立验收 | G14 已于 2026-11-20 确认通过 |

---

## 一、本轮实测结果（fc62c79）

| 门禁 | 引擎/环境 | 结果 | 证据 |
| --- | --- | --- | --- |
| Django system check | SQLite 内存库 | 0 issues，退出码 0 | 本轮隔离树实测 |
| makemigrations --check --dry-run | SQLite 内存库 | No changes detected，退出码 0 | 本轮隔离树实测 |
| **SQLite 全量** | `:memory:`，DJANGO_ENV=test，CI 口径不带 keepdb | **Ran 1754 tests，OK (skipped=26)，退出码 0** | `docs/acceptance/evidence/test-sqlite-r6-gate-20261120.log`（本轮新增，420.794s） |
| mypy 基线门禁 | 隔离干净树 | 通过，无新增错误（总 2146 < 基线 2206） | 本轮隔离树实测 |
| ruff 0.6.9 + black 24.8.0 | CI 差异范围 | PASS | CI `django-ci.yml` lint job @ fc62c79 |
| docs_link_check | 本地 + CI | 新增违规数=0 | 本轮实测 + CI docs-lint job |
| OpenAPI 双快照 cmp | 文件哈希 | 逐字节一致（SHA256 均为 D74ACA07…30A01） | 本轮实测 |
| **MySQL 全量** | CI MySQL 8.0 service（test_hotel_ci 独立库） | **success** | CI `ci.yml` django-tests job @ fc62c79（run 44698920145，07:58:43→08:04:47Z） |
| OpenAPI 快照守卫 | CI | success | CI openapi-snapshot 工作流（历史提交均绿；fc62c79 为纯文档提交，快照未变） |

**本轮结论：双引擎全量均通过，CI 两套工作流（Django CI 4 job + tests MySQL）全部 success。**

## 二、MySQL 权威结果细节（引用 R5 报告 + 本轮 CI）

| 项 | 结论 | 证据 |
| --- | --- | --- |
| MySQL 全量回归 | R5 报告 1747 项仅 1 失败（MC-006-R1 已登记隔离缺陷，隔离复跑 OK）；该缺陷已在提交 `69bf584` 根因闭环（assigned_to 契约错位），本轮 fc62c79 的 CI MySQL tests 全绿 | `test-mysql-r5-gate-20261119.log`、`mc006r1-assign-contract-fix-20261120.log` |
| 空库全新迁移 | 128 条 Applying...OK 全绿（11 app 全量迁移链） | `empty-db-migrate-20261116.log` |
| e2e 56 项断言 DB 副作用 | 56/0，含 D1 夜审房费入账、D3a 账实相符、D3 退房两讫、F5 LedgerEntry 只追加等库事实核对 | `e2e-rerun-20261116.log` |
| 触发器只追加守卫 | CI root 安装 6 组 + legacy batch/map 守卫验证通过；本地 1419 权限边界（无 SUPER + log_bin=ON）走 API 契约面等价断言，不降准 | R5 报告 §1.3 |

## 三、并发与幂等专项（MySQL-gated）

R5 报告 §二 17 组真实 MySQL 并发/幂等专项覆盖八面（walk-in 幂等、支付意图窗口、夜审先锁、退款执行幂等、现金退款 vs 关班、权限双批准、工号/成员唯一、PendingBookingGate 上限、押金上限、异常放行），本轮全绿。SQLite 下 `select_for_update` 为 no-op，相关用例显式 skip（本轮 skipped=26 含此类），符合"SQLite 不能替代 MySQL 权威"纪律。

## 四、MySQL 错峰纪律执行（任务书 §3.3）

| 规则 | 本轮执行 |
| --- | --- |
| 全量串行独占，不并行建库 | ✅ 本轮仅 CI 单进程跑 MySQL 全量；本地 SQLite 全量在隔离 worktree 执行，与 MySQL 不同库不争抢 |
| 不追加 test_% 授权 | ✅ 未修改 hotelhotel 授权；CI 用独立 `test_hotel_ci` 库（CI root 环境，非本机开发账号授权扩张） |
| 1213 分阶段登记 | ✅ 本轮无 1213；历史 1213 定性为建库/迁移阶段并行争抢，已在 GOV-025 §六/§七固化登记要求 |
| 记录执行人/时间/库名/命令/退出码/报告 | ✅ 见本报告 §一证据表 + `test-sqlite-r6-gate-20261120.log` 证据头 + CI run 元数据 |

## 五、本机工作区在途工作线的影响说明（书面例外）

本机主工作区存在未提交的 day-use 计费工作线（`apps/billing/models.py`、`apps/reservations/models.py` 的 DayUsePolicy/billing_type/day_use_* 字段，未生成迁移）。这导致：
- 主工作区 `makemigrations --check` 报 day-use 模型漂移、`mypy_baseline.py check` 报 15 个新增 var-annotated 错误（均在两个在途文件）。
- **已提交树 fc62c79 在隔离 worktree 中三项均绿**（migrations=0、mypy=0、check=0），证明 CI 口径不受影响。

本轮所有权威门禁均在隔离干净树或 CI 上执行，主工作区在途工作线未被夹带、未被提交（见 R6-10 §四）。此为本机环境的书面说明，非 CI 失败例外。

## 六、结论

MySQL（权威）与 SQLite（快口径）双引擎全量回归均通过，CI 门禁全绿，错峰纪律执行到位。本轮新增 SQLite 全量证据 1754 OK。独立验收 G14 确认通过。
