# R4-04 六链路浏览器 E2E 续作日志（2026-11-20）

服务：waitress :8000 @ 4ba29da（venv python 启动，重启于本日 01:37 前后）。
账号：test_front / test_duty / test_housekeeping / test_owner / test_finance / test_nightaudit，口令 Pms#Demo2026。
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
