# R4-04 六链路浏览器 E2E 续作日志（2026-11-20）

服务：waitress :8000 @ 4ba29da（venv python 启动，重启于本日 01:37 前后）。
账号：test_front / test_duty / test_housekeeping / test_owner / test_finance / test_nightaudit，测试口令（不入文档——见 scripts/r6_env.txt 受控配置，TASK-04 已轮换）。
班次：班次 #9（test_front，备用金 0.00，OPEN）。

## 自动化配方（IAB 已验证）

- Playwright locator click 大概率超时（actionability 永不过）；可靠路径 = 只读 evaluate 定位坐标 → `tab.cua.click({x,y})` → 效果验证。
- 输入：`fill` 在抽屉小表单可用；公开预订页用 `cua.click 聚焦 + cua.type`（fill/type 超时则降级）。
- `window.prompt`（开班/关班）IAB 瞬时消隐，无法自动化——已用 API 做环境准备开班（记录为 setup，非测试通过项）。
- `window.confirm` 用 `getJsDialog()` 在点击后同单元格轮询应答（否则 JS 全阻塞 30s）。
- 离屏元素：先 `scrollIntoView` + 等滚动稳定再量坐标；表格横向滚动可绕过 D-R4-06 遮挡（scrollLeft=scrollWidth）。
- 订单列表渲染截断订单号显示（20 字符），用前缀匹配定位行。

## 链路状态

- **链路 C 全 PASS**（截图 8 张）：公开页预订 R8EDBEEB418E13EA54C73 → 在线支付确认 → 排房 2825 → 入住 → 退房 → 清洁(认领/开始/完成) → 值班经理净房放行 → 房态恢复"当前可售"（计数 16→17）。
- **链路 A 进行中**：A1 建档✓（链路A客人）→ A3 收款确认✓（新单 R74610C690DDBFA89CF5F 收款 ¥100 自动确认 DEC-PND-01 全流程）→ A4 入住 3287 ✓ → A5 退房✓（toast 房转待清洁）。**待办：A6 财务中心核对凭证（test_finance，需换角色）；A7 夜审（test_nightaudit）**。
- 链路 B/D/E/F 未开始。

## 发现缺陷（测试中如实记录，未修）

- **D-R4-09（高，已修 4ba29da）**：管理中心 #pms-actions 位于 .main 之外，页面级动作全部被固定侧栏遮挡不可达。
- **D-R4-06（中）**：前台工作台表格横向溢出，"查看详情"列初始被侧栏面板盖住，需横向滚动可达（可用性缺陷，未修）。
- **D-R4-07（中，待 ORM 核实）**：排房/确认后房行翻倍：原"待分配"行未复用，房间数=1 但房行 2-4 条（截图为证：R74610... 退房后 4 行）。
- **D-R4-08（高，待 ORM 核实）**：退房后账单摘要归零——已收 100.00 退房前可见、退房后 0.00；且夜租 388 未入账（应收 0.00）。ImmutableFinancialFact 不可删改，疑 folio_summary 显示层缺陷或结算逻辑问题。
- **D-R4-10（低）**：过期 Hold 订单仍显示"收款即确认"引导文案，提交后被服务端正确拒绝（守卫生效，文案误导）。
- **开班 prompt（低，自动化限制）**：window.prompt 无法在 IAB 自动化，非业务缺陷。

## 关键数据

- 链路 C 单：R8EDBEEB418E13EA54C73（在线支付 ¥288）
- 黓路 A 单：R74610C6.CONTINUATION-TODO: A6/A7 用 R74610C690DDBFA89CF5F2026
- 2825 完成全链；3287 退房后待清洁（未走清洁链）
- 32745? — 留意：3287 已退房转待清洁，链路 C 的 2825 已恢复可售

## 下一步（按任务书第三批）

1. A6：test_finance 登录 → /manage/finance/ 核对收款凭证/班次（截图）
2. A7：test_nightaudit → /manage/night-audit/ 夜审（关账 11-20→次日，为链路 B 退款执行铺路）
3. 链路 B：收款→凭证→退款→审批→执行→对账（DEC-RF-01：关账后退款次日营业日执行）
4. 链路 D：Guest→会员→权益→预订→消费
5. 链路 E：员工→任职→授权→WorkItem→执行→审计
6. 链路 F：业务数据→营业日→夜审→ CONTINUATION
7. 缺陷 ORM 核实 + 修复 D-R4-06/07/08/10
8. 交付物：11 中心矩阵/状态机/API 对照表 + R6 文档 + 独立验收材料

## 2026-11-20 续作更新（第二批）

### 缺陷处置结果
- **D-R4-07 已修（9004155）**：loadDetail 缺 replaceChildren，房行重复渲染；复验房行=1 与 DB 一致
- **D-R4-06 已修（9004155）**：详情列 sticky right，实测无需横向滚动可命中
- **D-R4-10 已修（9004155）**：PENDING 文案补充库存保留期说明
- **D-R4-08 口径确认**：CLOSED 不计入摘要聚合是 test_e2e_flows:475 固化的已评审口径；退房多收款按 DEC-02 转 RefundLiability（LI5 ¥100 OPEN 实证），资金完整性无缺口
- **D-R4-11 撤销**：夜审员/停车安保/餐饮主管/餐饮服务员四空组是 0014 决策 5 有意设计（夜审由值班经理/店长兼岗；餐饮/停车模块未启用）——registry.py:387-406 明文
- **D-R4-13（高，待修）**：夜审关账后当日行 CLOSED，夜审中心/前台仍显示"营业中"（按 OPEN 口径显示与 fail-closed 写入矛盾），写操作全部被 WB-079 拒绝且无引导
- **day-use 过租缺口（待负责人裁定）**：当日入住当日退房（营业日内）错过夜审过租窗口且退房按实际入住晚数计 0 晚房费 → folio 不平关账（credits>0/charges=0）由 DEC-02 负债兜底，但房租收入缺失涉及计费语义

### 链路进度
- **链路 A 全链 PASS**：建档→预订→收款（自动确认 DEC-PND-01）→入住→退房→财务对账→夜审 run_id=8 SUCCESS
- **链路 B 全链闭环（API 治理路径，浏览器点击在 billing_center 表格行按钮上受 IAB 限制）**：
  - 申请 RF7（test_finance，幂等键）→ 审批（test_duty，SoD 申请人≠审批人）→ 执行守卫链：执行人≠申请人≠审批人 → 需执行人本人班次 → 班次现金余额 → 营业日 fail-closed（WB-079）→ **重开审批链**（duty 申请 approval 11 → owner 批准 → 11-20 REOPENED）→ 执行 SUCCEEDED（executed_by=31 test_finance2，shift 10）→ **B4 对账截图**：退款 1 笔/100.00、负债已清零、凭证数 3
  - **DEC-RF-01 实证**：退款 business_date=执行时营业日（11-20 当日执行）
  - 新增账号：test_finance2（E2026-FIN2，生命周期服务 create_employee，granted_by=店长，refund_execute ✓）——同时是链路 E 任职/授权证据
- **链路 E**：任职/授权段 PASS（生命周期服务）；WorkItem/审计段待走
- 链路 D/F 待走
- 自动化配方新增：稳定双采样坐标（防粒子动画/平滑滚动导致的 stale 坐标误触侧栏链接）；form-POST 登录/退出（环境准备）；confirm 用 getJsDialog 同轮应答

### 关键 ID
RF7（退款）/ LI5（负债 ¥100 已清）/ shift 9（front）、10（finance2）/ approval 11（重开）/ run 8（夜审 11-20）/ E2026-FIN2（新员工）
