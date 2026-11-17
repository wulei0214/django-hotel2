# PMS2 本地部署验收走查报告（开发侧证据 · G14-06/07/09 前置）

- 走查日期：2026-11-16
- 部署形态：git worktree `F:/Desktop/django-hotel-accept` @ `37a93fc`（main HEAD，工作区与并行会话在途改动隔离），waitress `:8001` threads=12，数据库 dev `hotel`（迁移零漂移）
- 走查方式：浏览器（ZCode IAB）真实 UI 操作 + requests 真实 HTTP API（角色分离会话）
- 性质声明：**本报告是开发侧走查证据，不构成 G14 独立验收签署**（签署权在独立验收人）

## 一、R6/G14-07 核心业务链路（全链通过）

| 环节 | 操作方 | 结果 | 关键证据 |
|---|---|---|---|
| 公开预订 | 匿名 | PASS | 订单 RBE3162524DE18151275A，标准间 ¥288，令牌一次性展示 |
| 本地模拟支付 | 匿名 | PASS | Payment mock 288 SUCCEEDED → 订单 CONFIRMED |
| 排房（UI） | test_front | PASS | 房 1410（pk24）分配成功，状态"已完成排房" |
| 押金登记 30% 强制 | test_front | PASS | 请求 86.40 → 服务端计得 86.40（288×30%，WB-030）落库 + DEPOSIT 凭证 -86.40 |
| Walk-in 散客直入（新端点） | test_front | PASS | POST /front-desk/walk-ins 201：R791ADA1B048322FAA96B / 房 2680 / 押金 86.40=288×30% |
| 同键幂等重放 | test_front | PASS | 200 + created=false（R1-01 服务层仲裁语义） |
| 夜审执行 | test_duty | PASS | run 11-16 SUCCESS（run_id=4），stay_count=1 过租 688（7874 商务套房） |
| 营业日重开授权链 | duty→owner | PASS | 申请 201（approval 6）→ 批准 → REOPENED；第二次重开被正确拒绝（防反复改历史） |
| 退房（净额房费） | test_front | PASS | stay 7 CHECKED_OUT；离店清洁任务自动生成（WB-055） |
| 清洁链 | housekeeping→duty | PASS | DIRTY→CLEANING→INSPECTING→(服务员 403)→经理放行 CLEAN；**WB-056 联动验证：未开工任务在放行时被 CANCELLED** |
| 退款三分离 | front→owner→finance | PASS* | 申请 201（refund 5, 86.40 PENDING）→ 自批 403（SoD 负向）→ 店长审批 APPROVED → **财务执行被关账守卫正确拦截（WB-079 生效）**，待 11-17 营业日开启执行 |
| 审计留痕 | owner | PASS | walk_in_created/checked_in/checked_out/task_created/task_released/deposit_collect/refund_approved/night_audit.run 全链在档 |

终态复核：`evidence/walkthrough-final-state-20261116.log`（Django shell 全量核对）。

## 二、R5/R10 九角色矩阵（12/12 通过）

- 5 个 ACTIVE 角色（owner/front/finance/duty/housekeeping）登录 → /home/ 全通过
- 4 个 PLANNED 角色（nightaudit/fnb/fnbsup/parking）登录后业务入口默认拒绝 ✓
- R10 负向探测：前台访问夜审中心 403、访问审计中心 403、API 直调夜审执行 403 ✓
- **D-07 强制改密实测生效**：test_housekeeping 首登被拦 → 完成改密流程后正常进入（新口令已登记于走查记录）

## 三、走查发现（3 项，登记待处置）

1. **前端原生 confirm() 阻塞自动化/部分环境**（可用性）：押金登记按钮触发浏览器原生 confirm；在自动化环境中阻塞主线程导致超时。建议与通知中心等已改页面同口径：改页面内确认框。
2. **关账后退款执行的运维口径待确认**（业务）：夜审关账后押金退款执行被 WB-079 守卫拦截（设计行为），需等下一营业日或走重开授权。实际酒店"夜审后退押金"是高频场景——建议负责人确认标准作业口径（次日执行 or 重开审批）。
3. **走查脚本房间定位缺陷**（过程记录，非产品缺陷）：清洁链曾误定位到历史脏房（pk22），已按 stay.room 补正到 2680 并复验联动语义。

## 四、证据文件

- `evidence/walkthrough-final-state-20261116.log` — Django shell 终态全量核对
- `evidence/waitress-accept-20261116.log` — :8001 部署运行日志
- 走查脚本：workspace 会话留存（walkthrough_r6_v3/v4/v5/v5b/v5d/r5_roles.py）

## 五、G14 对应状态（排期 V2.0 编号）

- G14-06（九角色登录）：开发侧证据 **齐备**（本报告第二节）——待独立验收人复核签署
- G14-07（核心业务走查）：开发侧证据 **齐备**（本报告第一节）——同上
- G14-09（HTTP 串测）：**已完成**——e2e_full_chain.py 重写（36→56 断言）后隔离 MySQL 复跑 56/56 全 PASS（evidence/e2e-rerun-20261116.log，786becb）
- 其余：G14-01/02/03/04/05 机器证据已有历史版本，需按最新 SHA 复跑更新后交独立验收
