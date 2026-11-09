# 52 · 入住管理/前台接待一致性修订 与 P0 敏感字段边界核对报告

> 审查基线：《PMS2员工中心最终UI规范与权限管理入驻管理一致性审查.md》§5 / §7 P0 / §8.3
> 仓库：django-hotel，分支 `main`，工作基线 HEAD `fe24b1c5fa3ac059590621517ab854a9ed4b15c2`（已 `git pull`，up to date）
> 执行日期：2026-11-06
> 本轮只动渲染层（模板/CSS/JS），不改 API、模型、权限规则与业务流程。

---

## 1. P0：敏感字段在旧入住模板的展示边界核对（§7 P0 / CK-UI-07）

### 1.1 核查结论：泄露点确认与处置

`templates/checkin.html`（路由 `/checkin/legacy/` → `hotelapp.views.checkin_manage`）在修订前**直接输出完整敏感值，共 4 处**：

| 位置（修订前行号） | 区块 | 输出 | 处置 |
|---|---|---|---|
| L51 | 待确认预订 · 排房 | `{{ line.联系电话 }}` 完整手机号 | `{{ line.联系电话\|mask_tel }}` |
| L82 | 已确认待入住 | `{{ line.联系电话 }}` 完整手机号 | `{{ line.联系电话\|mask_tel }}` |
| L105 | 预抵名单 | `{{ line.联系电话 }}` 完整手机号 | `{{ line.联系电话\|mask_tel }}` |
| L121 | 在住列表 | `{{ line.顾客id.身份证号 }}` 完整身份证号 | `{{ line.顾客id.身份证号\|mask_idcard }}` |

数据源核实：`Accommodation.联系电话`（`hotelapp/models.py:54`）与 `Client.身份证号`（`hotelapp/models.py:80`）均为明文 CharField，模板直出即响应体含完整 PII（违反 DATA-03 服务端渲染脱敏纪律）。

掩码实现：**复用既有 `hotelapp/templatetags/pms_filters.py`**（任务书写的 `apps/guests/templatetags/pms_filters.py` 路径不存在；实际既有过滤器在 hotelapp，`mask_idcard` 前2后2、`mask_tel` 前3后4，与 `apps/api/front_desk.py::_mask_phone`、`apps/guests/services.py::_mask`、揭示审计 `after={"masked":...}` 口径一致）。模板加 `{% load pms_filters %}` 即接入，服务端渲染输出掩码，响应体不含完整值。空值/异常短值按既有过滤器语义回退（不虚构、不回显长值）。执行中曾按任务书路径误建 `apps/guests/templatetags/pms_filters.py`，发现既有同名模块后已删除，未留重复定义。

### 1.2 frontdesk（正式页）脱敏现状核查：已合规，未改动

- `apps/api/front_desk.py`：全部住客电话输出均走 `_mask_phone` → `phone_masked`（L237/L489/L535/L844 历史订单），**无任何完整 phone 字段**；列表/详情/历史订单三条链路均脱敏。
- frontdesk **不输出身份证号**：`front_desk.py` 无 idcard/doc_no 类字段返回。
- 旧页详情（legacy 订单）电话同为 `phone_masked`（L844），`frontdesk.js` 仅渲染 `phone_masked`（loadDetail 两分支 L555/L590）。
- 完整值揭示走已批准流程：`/client/reveal_id/`、`/vip/reveal_tel/`（理由必填 8–240 字 + 审计）与 GuestService reveal（理由必填 + AuditEvent）；前台列表/工作台 UI 无揭示入口。符合 §8.3"完整值仅按已批准的揭示权限展示"。

### 1.3 匿名/越权泄露风险评估

- **匿名风险：无。** `/checkin/legacy/` 与 `/checkin/` 均挂 `@staff_required + @pms_permission_required('hotelapp.view_accommodation')`（`hotelapp/views.py:1871-1873, 1861-1862`），匿名请求被登录门禁拦截。
- **越权风险：无（权限门控在位）。** 页面读取需 `view_accommodation`；冲账/反结账列额外受 `hotelapp.reverse_checkout` 门控（`can_void`）。
- **内部用户可见明文（修订前现状）：是。** 持 `view_accommodation` 的登录内部用户（含客房等非前台岗位）在旧页曾可见完整电话/身份证。按《敏感字段权限边界设计》口径（证件/电话默认掩码、完整值限持权岗位+揭示审计），此为过度暴露。**本轮已按基线收敛为默认掩码**；完整值获取收敛到既有揭示接口（持修改权岗位 + 理由 + 审计）。
- 残余说明：掩码后值（前2后2/前3后4）对持 `view_accommodation` 的内部用户可见，属敏感字段基线允许的"掩码可见"档；如需按岗位进一步分级（如客房岗完全隔离），属权限模型决策，超出本轮 UI 范围。

## 2. §8.3 入住管理验收标准逐条对照

| # | §8.3 验收项 | 结果 | 证据/说明 |
|---|---|---|---|
| 1 | 正式入口与模板映射明确，旧/新页面职责无重复入口冲突 | ✅（路由已在本轮前明确） | `/checkin/` → `checkin_workspace` → `templates/frontdesk.html`（正式页）；`/checkin/legacy/` → `checkin_manage` → `templates/checkin.html`（兼容过渡页）。侧边栏唯一入口指向 `/checkin/`；全库无任何链接指向 `/checkin/legacy/`（仅直连 URL 可达）。旧页写路由已全部 410 退役、按钮无 JS 绑定（`wi-do/ck-do/...` 仅存在于模板，无处理器），实际只读——本轮未新增入口、未改跳转，符合文档"路由归属未确认前不删模板、不改跳转"。 |
| 2 | 表单标签、输入高度、按钮、表格、状态标签与员工中心规范一致 | ✅（本轮收敛） | 输入高度统一 36px（搜索框 34→36、日期 32→36、列表筛选 select 31→36、抽屉操作 select/input 35→36；均在 §3.3 的 36–40 区间；抽屉内按钮 34px 属 §3.3 按钮 34–40 区间）。标签上置、间距 6px（`.fd-operation-label`）。表格：表头浅灰、行分隔线、内嵌横向滚动容器（`.fd-table-wrap` + `min-width:760px`）、加载/空/错三态齐全（`fd-table-loading/-empty/-error`）。状态标签全部徽标化（`.fd-status` + 文字，颜色不单独承载语义）。卡片圆角 13→12px（§3.1 的 8–12 区间）；侧栏面板标题 14→15px（§3.3 卡片标题 15–17）。 |
| 3 | 高风险操作不与普通操作使用相同视觉等级；二次确认遵循现有业务规则 | ✅（本轮补齐） | 缺陷：JS 已用 `fd-btn-danger` 但 CSS 从未定义该类，"标记未到店"实际渲染为无样式按钮。本轮新增 `.fd-btn-danger`（`--color-danger-strong` 底 + hover `--color-danger-strong-hover`，白字对比达标）。危险=红："标记未到店"（释放库存，不可逆）、"确认已实际退款并审批"（写入不可变凭证）改用 `fd-btn-danger`。普通=蓝主按钮："确认排房/办理入住/办理退房/登记已收款"；次级="续住/换房/申请退款/登记已收押金"。二次确认全部保留既有 `window.confirm` 文案（收款/押金/退房/No-Show/退款审批），No-Show 与退款仍要求原因输入——零 API 改动。 |
| 4 | 敏感字段默认掩码，完整值仅按已批准的揭示权限展示 | ✅ | 正式页本已合规（§1.2）；旧页 4 处明文本轮掩码（§1.1）；揭示仅经 `/client/reveal_id/`、`/vip/reveal_tel/`、GuestService reveal（理由+审计）。 |
| 5 | 桌面、平板、窄屏无整页横向溢出 | ✅（本轮收敛） | 缺陷：≤820px 时 `.fd-main{margin-left:0}` 与固定 220px 侧边栏重叠（site.css 无断点，sidebar 恒为 fixed 220px）。本轮在 frontdesk 命名空间内收敛：≤820px 侧边栏 `translateX(-102%)` 收起，页头新增"导航"开关（`fd-nav-toggle`，桌面隐藏），JS 切换 `body.fd-nav-open` 并同步 `aria-expanded`，Esc/点击导航项收起。横向溢出逐项核过：表格在 `.fd-table-wrap` 内滚、Tabs `overflow-x:auto`、KPI 2 列 + `minmax(0,1fr)`、searchbar 换行、抽屉 ≤480px 全屏宽、`.fd-panel{min-width:0}`——375px 无整页横向滚动。 |
| 6 | 不改变预订、排房、入住、退房、账单和审批业务流程 | ✅ | 零 API/端点/参数/状态机改动；仅渲染层（掩码、样式、焦点、导航开关）。全量 729 测试通过（含 frontdesk 页面渲染断言与预订/入住/退房/退款/夜审全链路）。 |

### 2.1 附：§5.2 关注点（键盘与可访问性）处置

- 抽屉焦点管理补齐（§2.3 基线）：打开→焦点入抽屉（关闭钮），关闭→焦点回触发按钮，Tab/Shift+Tab 焦点圈闭防逃逸，Esc 仅在开启时生效；`aria-hidden` 同步。
- 键盘焦点可见：新增 `:focus-visible` 焦点环（按钮/Tab/KPI/行链接/输入/分页），修复搜索框 `outline:0` 无替代指示的问题（`:focus-within` 高亮容器）。
- `prefers-reduced-motion`：frontdesk 命名空间内关闭过渡与 KPI 悬停位移。
- 导航开关带 `aria-label`/`aria-expanded`；图标装饰均 `aria-hidden`（原有）。

## 3. 改动文件清单（仅本智能体所有权文件）

| 文件 | 改动 |
|---|---|
| `templates/checkin.html` | P0 掩码：`{% load pms_filters %}` + 3 处 `联系电话\|mask_tel` + 1 处 `身份证号\|mask_idcard`（不做视觉重做，无 viewport/布局改动——CK-UI-08 留待旧页整体处置时随产品决策处理） |
| `templates/frontdesk.html` | 页头新增窄屏"导航"开关按钮；css v2→v3、js v3→v4 版本戳 |
| `static/css/frontdesk.css` | `.fd-btn-danger` 定义；`:focus-visible`/`:focus-within` 焦点样式；控件高度统一 36px（4 处）；面板圆角 13→12；侧栏标题 14→15；≤820px 侧边栏收起+开关显隐；`prefers-reduced-motion` |
| `static/js/frontdesk.js` | **修复 L652 缺右括号 SyntaxError（致命：此前整个工作台 JS 不执行，页面无法加载数据）**；抽屉焦点管理（进入/返回/圈闭/Esc 门控）；窄屏导航开关逻辑；Esc 同时收起导航 |

未触碰：`hotelapp/views.py`、API、模型、权限、site.css、_sidebar.html、staff_center/platform permissions（并行智能体所有权域）。

## 4. 测试回归

- 命令：`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite ".venv/Scripts/python.exe" manage.py test --noinput`
- 结果：**Ran 729 tests in 190.868s — OK**（与基线 729 持平，零回归、零新增失败）。
- 掩码影响评估：全库测试无对 `/checkin/legacy/` 渲染内容或明文电话/身份证的断言（grep 核实），无需改断言；`/checkin/` frontdesk 页两处断言（`data-can-operate`、`detail-operations`、CSRF）在改动后仍通过。
- 过滤器链路验证：Django shell 模板渲染 `13812345678→138****5678`、`110101199001010011→11**************11`；`checkin.html` 空上下文独立渲染通过（语法+load 链路）。
- JS 语法：`node --check static/js/frontdesk.js` 通过（修复后）。

## 5. 遗留与建议（不阻塞本轮验收）

1. CK-UI-08（旧页缺 viewport）与 CK-UI-01~06（旧页视觉重整）：按文档 §5 结论，旧页仅作 410 时代的只读过渡兼容，建议随路由退役决策一并处理，避免对将退役页面二次投入。
2. `checkin.html` 上的无功能按钮（写路由已 410、无 JS 绑定）建议在旧页退役时一并清理；本轮按"不改跳转、不删模板、不改流程"保留。
3. 敏感字段按岗位分级（如客房岗对证件完全隔离）属权限模型决策，建议在《敏感字段权限边界设计》正式基线拍板后另行立项。
4. frontdesk 窄屏导航为 CSS 抽屉式过渡方案；若产品要求全站统一移动导航，应在 site.css/`_sidebar.html` 层统筹（非本轮所有权）。
