# 60 · PMS2 财务与权限域代码审查报告（code-review 四维，基线 1a39dd0 以来并行提交）

- 审查对象：`git diff 1a39dd0..HEAD -- apps/billing apps/api/approval.py apps/billing/approval_registry.py apps/identity/registry.py apps/identity/context_processors.py`
- 方法：code-review 技能四维扫描（安全 / 正确性 / 契约 / 性能），每缺陷先失败测试后修复
- 审查时点：main HEAD `df2562e`（审查开始时 `3f82615`，并行会话持续提交；本报告修复以工作树未提交改动形式交付，见 §6）
- 审查范围内提交数：**146 个**（任务口径 136 个不含 identity/registry 两文件；扩展后含权限注册表迁移共 146）
- 测试口径：`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite`；MySQL 权限问题按任务约定跳过，并发缺陷以「普通读补丁模拟 RR 旧快照 + select_for_update 侦听」白盒方式在 SQLite 确定性覆盖（见 §3）

## 0. 结论速览

- **安全**：未发现新 P0。审批白名单双层校验（API + Service）、对象级物业校验（非本物业一律 404）、退款申请/审批/执行三方 SoD（服务层 + 模型层 + 数据库 CHECK 三层）、审批事实不可变管理器、SQL 注入面（全部 ORM 参数化）均成立。
- **正确性**：发现 **5 个缺陷并全部修复**——3 个 P1 同属「MySQL REPEATABLE READ 旧快照 + 行锁等待」并发类（退款执行汇总、支付状态机校验、收款应收余额），1 个 P2 押金回减丢失更新，1 个 P2 收款幂等重放契约缺口。P0 = 0。
- **契约**：`collect_payment` 重放谓词漏 `REFUND_PENDING`（BUG-05，已修复）；其余响应封套、状态码语义与 OpenAPI 抽查一致。
- **性能**：新增锁读均为行级锁短事务，锁序统一 Payment→Refund（→Reservation），无反向序；`financial_summary` 全表内存聚合为基线遗留，登记 R-03。
- **权限域**：`identity/registry.py` 权限登记与迁移 0013/0014/0020 授权-回收一致；退款三方权限（refund_request/refund_approve/refund_execute）、冲销两权（reverse_entry/reversal_approve）、班次两权（cashier_shift_manage/cash_movement_manage）均已登记进财务模块权限面；清洁维修权限迁移（housekeeping_view/update、maintenance_manage 与 no_show_handle 解耦）口径一致。
- **identity 三份指定文件**：`apps/identity/services.py`、`invitation_services.py`、`invitation_views.py` 自基线 **0 变更**（邀请 token 一次性消费/过期/撤销逻辑未动），由 `apps.identity` 全量存量测试回归覆盖。

## 1. 审查提交清单（146 个，按主题分组）

| 主题 | 数量 | 代表提交（全量 SHA 见 §1.1） |
|---|---|---|
| 退款三方分离与审批链完整性（request→approve→complete、ApprovalRecord 关联、快照校验、到期释放） | 38 | a9a8857, da1a55e, a32788d, c1a9f16, dd5f7b1, c4af2ab, fd64cd5, 7503dac, 600d0b9, 506b804, fe89cf0, 04b1f0c, 17b7fc7, 3bf6f53, 1bc9aa6, 8950d8e |
| 退款/支付状态机与数据库约束（0016/0019/0021/0022、不可变模型守卫） | 22 | f6ff522, e21ab9f, 24214bf, baacbb9, c30471c, 548d77b, 9be0691, 720a05a, 819acbe, ae979c6, e3cfd92 |
| 红字冲销迁移为财务申请/执行 + 经理审批（0020、ck_ledger_reversal_approval_link） | 17 | 99c2cd4, b73cff8, daceb2d, c4ad92d, 699d2f4, 2fa816e, ba687ba, e4a9573, c05d716 |
| 收银班次与现金事实（0012/0014/0017/0018、班次不可变、余额口径、锁序 Shift→Payment→Refund） | 25 | a9a8857, da6a997, d0deb73, 202dcb2, e62f6e3, 65068b6, de7d90e, bec98b6, 710ea70, 52a182e, 7eb477a, dbe64ce |
| 幂等与并发竞态修复（WB 系列唯一约束仲裁、MySQL 当前读系列） | 20 | 8d6f640, 1414814, 0ebecb0, 414cd03, 46c6698, 994f097, 90b87ff, 596f359, 4f88755, 1e25fe7 |
| 审批 API/注册表白名单与权限面（api/approval.py、approval_registry.py、registry.py、迁移 0013/0014） | 14 | b73cff8, c4ad92d, dccdd2d, d5485c4, 90f30e6, 83533f9, 21f7feb, 98ef02a, 7510def |
| 夜审预览/白盒测试基建 | 10 | 19a4b53, 58b7194, b146746, c05391a, 4721180, 371307a, 4cd4849, 5b27e16 |

### 1.1 全量 SHA 清单

```
3f82615 fe89cf0 506b804 600d0b9 d5485c4 4bbe16e 8950d8e 134944a 38dd25c 1bc9aa6
52a182e 710ea70 b9ba8c3 3bf6f53 17b7fc7 04b1f0c 47050ba dccdd2d 4f88755 1e25fe7
59ade32 2ad7a28 dbfaf9b 71b1fb7 596f359 9cb4f8e 36c39d7 c05391a 1f7f43b c30471c
ccbc58d baacbb9 17041a0 8d6f640 6718c99 98ef02a fdc5bf0 c05d716 e4a9573 15d4415
ba687ba 7510def 0dd5c4d 150aaff 08adaf6 ba99a41 f2e446c 699d2f4 5fd345c 324bf82
99f1c28 388f1ed 990a396 c4ad92d daceb2d 247a7b7 b73cff8 99c2cd4 2fa816e 7503dac
fd64cd5 5b27e16 07c745c 7b8a2f7 4721180 371307a 4cd4849 46c6698 90b87ff 994f097
d897e84 dbe64ce bd5cd83 47727f9 aeba0f3 d304c6b 24214bf df015ed f0088c3 c4af2ab
dd5f7b1 c1a9f16 9756bcf fcb80b3 984fb5b c0af2a8 277434c 414cd03 a62a29a 77af28a
0ebecb0 794469c 1414814 55fc4e3 7976b50 e62f6e3 65068b6 6d75ff6 bd1d725 c2acc4c
792af40 9aecb06 720a05a fd089ab 8f5a50b de7d90e bec98b6 d3216cb a1df315 9be0691
548d77b 5fbdb31 9f6c593 cdf4362 d0deb73 202dcb2 e21ab9f f6ff522 b8a0cd3 a1edb06
e3cfd92 819acbe ae979c6 f9083a5 56a04de dbc110f 085e32b 7eb477a 42c25e2 11b9fd8
21f7feb 83533f9 90f30e6 da6a997 da1a55e a32788d a9a8857 82fd293 12890fb 2e6bb75
27ecec4 ec5534b 58b7194 19a4b53 5684a4b b146746
```

## 2. 四维审查记录（通过项摘要）

| 维度 | 检查点与结论 |
|---|---|
| 安全 | ① 退款/冲销/夜审重开/敏感导出四动作全部登记于 `ApprovalActionRegistry`，未知 action_code 双层（API+Service）拒绝且零副作用；`privacy.export_staff` 显式 `api_requestable=False` 关闭通用面入口。② 审批列表专项只读权 `billing.approval_view` + 物业过滤 + 无审批权者仅见本人申请；对象级校验非本物业 404 不泄露存在性。③ 退款专用入口拒绝通用面审批/驳回（`dccdd2d`/`d5485c4`：先授权检查后路由守卫）。④ SoD 三层：服务层（approve/reject/execute 申请人≠审批人≠执行人）、模型层（Refund.save）、数据库层（0016 三条 SoD CHECK，白盒测试用 `_base_manager` 绕过模型仍被数据库拒绝）。⑤ 敏感值：payload 白名单 + 身份证/卡号/密钥正则拒绝；日志不含金额外敏感信息。⑥ 退款审批链四路径（approve/reject/execute/expire）全部经 `_validate_refund_approval_link` 校验审批事实与退款一一对应（`600d0b9`/`506b804`），错链/畸形快照 fail-closed。 |
| 正确性 | 金额全程 Decimal（float 拒收、quantize 分边界、超大值转 ValueError）；状态机前置校验（Payment/Refund 模型双状态机 + CHECK 约束）；现金退款班次归属（执行人本人 OPEN 班次 + 抽屉余额不足拒绝 + 数据库非负约束）；过期审批终态释放预占（expire_due_refunds 逐笔事务、锁序 Payment→Refund→ApprovalRecord）。发现缺陷见 §3。 |
| 契约 | 幂等重放统一「同 key 同事实返回既有记录」（审批/凭证/收款/押金/班次/退款申请/退款执行七处均有唯一约束兜底仲裁）；关账/结账后重放返回原事实而非报错。缺口：收款重放谓词漏 REFUND_PENDING（BUG-05）。 |
| 性能 | 审批列表 prefetch 消除 N+1；锁读集中在行级短事务；锁序全局单向（现金：Shift→Payment→Refund；账务：Payment→Refund→Reservation）无反向等待；`financial_summary` 内存聚合登记 R-03；`_money` 每次调用重复构造 CENT 常量为类属性，无热点问题。 |

## 3. 缺陷清单（全部已修复，先失败后通过）

| 编号 | 等级 | 位置 | 根因 | 修复 | 测试 |
|---|---|---|---|---|---|
| BUG-01 | P1（资金状态错误） | `apps/billing/services_v2.py` execute_refund 汇总段（原 927-933 行） | MySQL RR 下 `probe` 普通读先行建立事务快照，等待 Payment 行锁期间其他事务可提交同支付的其他退款执行；锁后汇总 `refunded_total`/`has_pending_refund` 仍用旧快照普通读，漏掉并发已提交的 SUCCEEDED 退款，把应为 REFUNDED 的支付写成 PARTIALLY_REFUNDED（与 `4f88755` 已给 reject_refund 的同款修复漏配执行路径） | 两处汇总改 `select_for_update().order_by("pk")` 当前读（与 reject_refund 修法一致） | `ConcurrencyCurrentReadTests::test_execute_refund_totals_see_concurrent_refunds`（补丁隐藏并发退款模拟旧快照：修复前 PARTIALLY_REFUNDED，修复后 REFUNDED） |
| BUG-02 | P1（状态机放行错误终态） | `apps/billing/models.py` Payment.save 退款状态校验（4 处普通读） | 同为 RR 旧快照：迁移到 PARTIALLY_REFUNDED/REFUNDED/REFUND_PENDING/回退 SUCCEEDED 的四组校验读退款事实用普通读，与服务层当前读结果不一致时要么放行错误状态、要么误拒正确状态 | 新增 `_current_refund_facts()`：事务内 `select_for_update` 当前读（autocommit 下普通读即当前读），替换 4 处校验读；锁序维持 Payment→Refund | 同 BUG-01 测试（模型层汇总与服务层汇总必须一致看到并发退款）；既有 `test_payment_status_update_does_not_query_refund_only_fields` 守护无回归 |
| BUG-03 | P1（超额收款窗口） | `apps/billing/services_v2.py` collect_payment 应收余额汇总（原 362-374 行） | `0ebecb0`/`a62a29a` 为修复关账后重放把「幂等预检普通读」移到 Folio 行锁之前，RR 快照建立点随之前移；锁定账单后汇总账本条目仍用旧快照普通读，漏掉等待期间并发收款提交的负向凭证 → 应收余额虚高 → 可超额收款（客人多付形成隐性预收） | 条目汇总（余额 + 房费已入）改 `folio.entries.select_for_update()` 当前读 | `ConcurrencyCurrentReadTests::test_collect_payment_balance_uses_current_reads_after_folio_lock`（并发收款 100 后旧快照口径允许再收 250，修复后正确拒绝「超过应收余额 200」） |
| BUG-04 | P2（账实不符丢失更新） | `apps/billing/services_v2.py` execute_refund 押金回减段（原 941-945 行） | 押金累计 `reservation.deposit_amount` 的写入口 `record_deposit` 以行锁保护读-改-写，但退款回减路径直接 `payment.reservation` 无锁读-改-写；与并发押金登记（或其他押金退款）互相覆盖：押金账面被写回旧值，后续「累计押金≤房费总额」上限按错误基数放行 | 回减前 `Reservation.objects.select_for_update().get(pk=payment.reservation_id)` 当前读锁定预订行（锁序 Payment→Reservation，全库无反向序） | `ConcurrencyCurrentReadTests::test_deposit_refund_locks_reservation_before_balance_update`（select_for_update 侦听断言预订行被锁定 + 押金余量正确） |
| BUG-05 | P2（幂等契约） | `apps/billing/services_v2.py` collect_payment 重放谓词（原 351-354 行） | 函数注释承诺「同一参考号重试必须返回原支付，不因后续状态变化制造失败假象」，但状态白名单只含 SUCCEEDED/PARTIALLY_REFUNDED/REFUNDED；支付进入 REFUND_PENDING（有人发起退款）后客户端重试同一参考号被误报「该收款参考号已用于不同支付」 | 重放谓词补入 `Payment.Status.REFUND_PENDING`（事实匹配校验不变，无资金副作用） | `ConcurrencyCurrentReadTests::test_collect_payment_replay_survives_refund_pending_state`（退款处理中重放返回原支付、无重复凭证） |

**BUG 统计**：P0=0；P1=3；P2=2；P3=0。**修复数=5**（BUG-01/02 为同一根因的两层，随同修复）。**新增测试=4**（一个测试类覆盖 5 个缺陷，先失败 3F+1E，修复后 4P）。

## 4. 登记项（未修复，附理由）

| 编号 | 等级 | 位置 | 现象与建议 | 不修复理由 |
|---|---|---|---|---|
| R-01 | P3 | `services_v2.py` expire_due_refunds | 单笔退款的审批快照与退款事实不一致时 `_validate_refund_approval_link` 抛 ValueError 会中断整批过期任务。当前不可达：request_refund 单事务内创建退款+审批，PENDING 必有匹配审批；建议后续对该校验包 try/except 逐笔跳过并写审计，增强批任务鲁棒性 | 现状由事务原子性保证不可达；防御性改造属健壮性增强而非缺陷修复 |
| R-02 | P3 | `services_v2.py` collect_payment IntegrityError 竞态分支 | 撞唯一约束后的幂等判定谓词要求 `status == SUCCEEDED` 且不含币种比对；并发窗口内新支付必然 SUCCEEDED、币种同源预订，实际不可达 | 理论微窗口，与主路径（已含 REFUND_PENDING）行为一致性可在后续小版本统一 |
| R-03 | P3（性能） | `services_v2.py` financial_summary | 收入/支出/笔数把全部凭证/流水载入内存求和，大物业月份汇总有性能风险；建议改数据库聚合（Sum/Count） | 基线 1a39dd0 已存在、非本次 146 提交引入；报表只读无资金风险 |
| R-04 | 备注（设计） | `services_v2.py` reverse_entry | 冲销允许申请人==执行人（仅强制审批人≠执行人），弱于退款三方分离 | `ApprovalActionRegistry` 中 `billing.reversal` 的 sod_rule 明示「财务申请/执行；值班经理或店长审批」，属登记设计而非缺陷 |
| R-05 | P3（业务语义） | `services_v2.py` request_refund | 退款申请不校验所属 Folio 是否 VOIDED（执行时才拦截）：账单作废后已批退款会滞留 APPROVED 直至执行失败 | Folio VOIDED 当前无生产写路径；语义取舍（作废后是否允许退款）属业务决策，交产品确认 |

## 5. 测试结果

| 套件 | 结果 |
|---|---|
| 新增 `apps.billing.test_whitebox_services.ConcurrencyCurrentReadTests`（修复前） | 4 项：3 失败 + 1 错误（全部命中 §3 缺陷，满足「先失败」） |
| 新增 `ConcurrencyCurrentReadTests`（修复后） | 4 项全部通过 |
| 全量 `apps.billing apps.identity`（`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite`） | **Ran 316 tests — OK**（基线 309 + 本审查新增 4 + 并行会话期间新增 3），无回归 |
| 关联回归 `apps.api.test_billing_center / test_approval / test_whitebox_api / test_finance_route_regressions / apps.billing.test_approval_registry / apps.billing.tests` | **Ran 129 tests — OK** |

## 6. 交付与变更文件

| 文件 | 变更 |
|---|---|
| `apps/billing/services_v2.py` | 3 处修复：execute_refund 汇总当前读（BUG-01）、execute_refund 押金回减锁预订行（BUG-04）、collect_payment 条目当前读（BUG-03）+ 重放谓词补 REFUND_PENDING（BUG-05） |
| `apps/billing/models.py` | `_current_refund_facts()` 当前读助手 + Payment.save 四处校验读替换（BUG-02）；新增 `transaction` 导入 |
| `apps/billing/test_whitebox_services.py` | +179 行：`ConcurrencyCurrentReadTests` 4 个回归测试（含模拟方法说明） |

- 以上修复按「不擅自提交」纪律保留为工作树未提交改动；工作树中 `apps/api/audit_center.py`、`templates/pms/audit_center.html` 的改动属并行会话，本审查未触碰。
- MySQL 复核建议：CI 的 MySQL 8 流水线（`tests(MySQL8)`）合入后重跑一次全量；§3 三个并发缺陷在 RR 隔离下的真实触发路径只能在 MySQL 上端到端复现，SQLite 侧为白盒等价覆盖。
