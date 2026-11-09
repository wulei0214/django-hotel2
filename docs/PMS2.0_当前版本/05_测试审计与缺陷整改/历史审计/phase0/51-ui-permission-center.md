# 51 · 权限管理中心 UI 一致性修订 与 §8.2 验收自检报告

> 审查基线：《PMS2员工中心最终UI规范与权限管理入住管理一致性审查.md》§4 / §8.2
> 仓库：django-hotel，分支 `main`，工作基线 HEAD `fe24b1c`（已 `git pull`，up to date）
> 执行日期：2026-11-06
> 本轮只动渲染层（模板/CSS/JS）与平台壳一行 box-sizing 修复，不改 API 契约、
> 权限注册表（registry.py）、授权面、并发版本校验、confirm token 与审计规则。

---

## 1. 改动文件与范围

| 文件 | 改动 | 对应条目 |
|---|---|---|
| `templates/platform/permissions.html` | 资产版本 v=2→v=3；确认区新增 `#pc-special-warn` 常驻提示槽；`#pc-result` 加 `role="status" aria-live="polite"`；`#pc-step-hint` 加 `aria-live`；注释标注 §8.2 修订口径 | §8.2 反馈/高危呈现 |
| `static/css/permission_center.css` | 标题 22px→24px（PC-UI-01）；卡片圆角 8px→`var(--radius-lg,10px)`（PC-UI-02）；角色悬停/选中/输入焦点金色→PMS 蓝（PC-UI-03）；`#pc-matrix-box` 横向滚动容器 + 表格 min-width 640px（PC-UI-04）；媒体查询 1100px 单档 → §3.7 三档 1439/1023/720（PC-UI-04/右区堆叠）；专项条目 `需理由`/`需二次确认` 徽标样式；高危确认区视觉升权（`pc-highrisk` 红色左缘 + `pc-special-warn` 琥珀提示块 + `pc-btn-danger` 危险色第二步按钮）；结果四态样式 `pc-result-conflict`/`pc-result-token` + `::before` 状态徽记；`#pc-reason:focus` 恢复可见焦点轮廓（原 `outline:none` 移除项已纠正） | §8.2 全部呈现项 |
| `static/js/permission_center.js` | 角色列表 listbox/option 语义 + `tabindex=0` + Enter/Space 选中（PC-UI-05）；`markSelectedRole` 同步 `aria-selected`；专项条目按 bootstrap 原值渲染 `需理由`/`需二次确认` 标注；`updateSpecialWarn()` 高危常驻提示（触及专项数 + 芯片 + 两步说明 + 令牌 10 分钟有效期）；`setStepButton` 第二步加危险色类；`failureResult()` 按 `err.data.code` 分派四态反馈（version_conflict→琥珀 409 冲突 / token_*→蓝色令牌态 / unknown_permissions→红色整体拒绝 / 其余→红色失败），仅在 `err.message` 与标题叠词时去重前缀，不改变消息语义 | §8.2 键盘/高危/反馈 |
| `static/css/platform.css` | **越出一页文件所有权的说明项**：`.pf-main` 补 `box-sizing: border-box`（一行） | §8.2 窄屏验收前提 |
| `templates/platform/base.html` | `platform.css?v=2→v=3`（缓存版本号，与上一行配套） | 同上 |
| `apps/identity/permission_center.py` | **未改动**。bootstrap 已携带 risk/requires_reason/requires_confirm/label/note 全量展示数据（`page_bootstrap()` L266-273），无需变更即满足 §8.2 呈现要求 | — |

**platform.css 一行修复的理由**：修订前所有平台页（含平台首页）在任何视口下都固定整页横向溢出——`.pf-main` 的 `width: min(100%,1480px)` 与左右 32px padding 按 content-box 叠加，实测 1280px 视口 `scrollWidth 1344 / clientWidth 1280`（+64px），375px 视口 +32px。该缺陷使 §8.2「窄屏不发生整页溢出」在权限页自身的矩阵容器修复完成后仍无法达标。修复仅影响盒模型计算，不改平台导航、品牌色与布局结构；平台首页同步受益（实测溢出归零）。

## 2. §8.2 验收标准逐条对照表

| §8.2 原文 | 结论 | 证据（本轮实测） |
|---|---|---|
| 保留平台域导航和权限范围提示 | **通过，原文未动** | `templates/platform/base.html` 平台导航四链接与登出结构未改（仅资产版本号）；页面 `pc-scope-notice` 原文「平台超管拥有平台域管理权限；PMS 业务权限仍以员工任职和岗位授权为准」与 `PLATFORM_SCOPE_NOTICE`（permission_center.py L80-82）一字未改；`test_page_renders_scope_notice_and_bootstrap` 原断言原样通过 |
| 角色列表可用键盘操作，焦点和选中状态可见 | **通过** | `#pc-role-list` 渲染为 `role="listbox"`（aria-label=角色目录），每个有 group_id 的角色项 `role="option" tabindex=0 aria-selected`；预留/未初始化项 `aria-disabled="true"` 不可聚焦。实测：Tab 聚焦后 Enter 选中「值班经理」、Space 选中「财务」，`aria-selected` 正确迁移，焦点保持在项上；焦点环用浏览器默认轮廓（本轮无任何 `outline:none` 覆盖，原 `#pc-reason:focus{outline:none}` 已改为可见蓝色轮廓） |
| 权限矩阵在窄屏不发生整页溢出 | **通过** | 矩阵置于 `#pc-matrix-box` 横向滚动容器（表格 min-width 640px），网格子项 `min-width:0` 防撑破。实测 `document.scrollWidth==clientWidth`：1280/1024/768/375px 四档全部无整页溢出；768px 两栏（影响区通栏堆叠）、375px 单栏，矩阵在容器内滚动（`matrixScrollable=true`）。媒体查询按 §3.7：1439 压缩列距 / 1023 两栏 / 720 纵向 |
| 高危权限的风险、两步确认和理由要求明确 | **通过** | 专项权限每项渲染风险徽标（高危红/中危琥珀/低危灰）+ `需理由`（requires_reason）+ `需二次确认`（requires_confirm）标注，数据取自 bootstrap 原值不改语义；矩阵内专项码保留「专项」角标。保存流程：触及专项时确认区出现琥珀色常驻提示块（触及项数 + 芯片 + 「两步确认 / 令牌获取后 10 分钟内有效 / 理由必填写入审计」），确认卡红色左缘（`pc-highrisk`），第二步按钮为危险色实底（`pc-btn-danger`，文案「第二步：确认保存（高危变更）」）——提示/有效期/变更内容/按钮文案四重表达，不只靠颜色（PC-UI-07） |
| 保存成功/失败、版本冲突等反馈明确 | **通过** | 四态各自可辨识且经浏览器实测：①成功=绿色 `✓` + added/removed 计数与芯片 + 新版本号 `<code>`（实测「保存成功…新版本 78f63077c55bcfda」）；②失败（整体拒绝）=红色 `✕` +「整体拒绝（409，不静默过滤）」标题 + 拒绝原因；③版本冲突=琥珀 `!` +「版本冲突（409）」标题 + 服务端当前版本号 + 自动刷新说明（实测双标签页并发：B 先保存推进版本，A 旧版本保存得到 409 且页面自动加载服务端真值 745fcd08fae9e4c9）；④令牌过期/无效/缺失=蓝色 `i` + 服务端原文加粗（「确认令牌已过期（10 分钟有效期）…」/「确认令牌无效：与当前变更内容不匹配」）+ 重新获取指引。`#pc-result` 带 `role="status" aria-live="polite"` 播报；两步中间态（已获令牌）为独立蓝色 info 态。令牌过期态的浏览器实测依赖 10 分钟等待，未live触发，其余 403 分支与过期分支共用同一 `failureResult` 代码路径且服务端文案自解释；过期行为本身由 `test_expired_token_403` 覆盖 |
| 不改变权限注册表、授权面、并发版本、token 和审计规则 | **通过** | `apps/identity/registry.py`、`apps/identity/permission_center.py` 零改动（git diff 为空）；读写 API 请求/响应结构、409/403 拒绝码、confirm token 校验、`permissions.matrix_saved` 同事务审计全部原样；既有 34 项 `test_permission_center.py` 未修改任何断言，全数通过 |

## 3. §4.3 差异表（PC-UI-01~07）处置对照

| 编号 | 处置 |
|---|---|
| PC-UI-01 | 页面标题 22px→24px，与员工中心 h1 一致（间距 `margin 0 0 6px` 对齐） |
| PC-UI-02 | 卡片圆角 8px→`var(--radius-lg, 10px)`，落入 10–12px 规范区间 |
| PC-UI-03 | 角色悬停/选中态、理由输入焦点由金色统一为 PMS 蓝；平台金色仅保留在平台壳导航（platform.css，非本轮文件）；风险语义保持红/琥珀/灰，金色不再与警告色相邻使用 |
| PC-UI-04 | 矩阵信息密度保持；`#pc-matrix-box` 独立横向滚动容器 + min-width 640px，§3.7 三档断点（1439/1023/720）替换原 1100px 单档；1023–720 区间两栏（角色列 190px + 矩阵），影响区通栏堆叠 |
| PC-UI-05 | 角色项 listbox/option 语义化 + Tab 聚焦 + Enter/Space 选中 + aria-selected，默认焦点环保留 |
| PC-UI-06 | 确认区保持独立卡片；窄屏单栏堆叠不与矩阵混淆；提交结果 `#pc-result` 就地呈现且 aria-live 播报 |
| PC-UI-07 | 两步确认提示、令牌有效期、变更内容说明常驻于 `#pc-special-warn`（琥珀块）+ `pc-step-hint`；第二步按钮危险色 + 文案自述，不单靠颜色 |

## 4. 验证记录

- **运行态冒烟**（临时 sqlite 文件库 + runserver + 真实浏览器登录平台超管）：
  - ARIA 树确认：`listbox`/`option`/`[selected]`、专项条目「高危/中危/需理由/需二次确认」、`status` 反馈节点全部呈现；
  - 键盘：Enter 选中值班经理、Space 选中财务，选中态与反馈正确迁移；
  - 高危两步：勾选夜审执行权 → 琥珀警示块出现 → 第一步取得令牌（info 态）→ 第二步危险色按钮保存成功（绿色 + 新版本号）；截图存档于本轮执行记录；
  - 版本冲突：双标签页 + API 侧并发保存，旧版本提交得 409 琥珀冲突反馈并自动刷新；
  - 溢出：1280/1024/768/375 四档 `pageOverflow=false`，矩阵容器内滚动。
- **测试回归**：全量 `manage.py test`（DJANGO_ENV=test, sqlite）**753 项全过**（基线 729 + 本轮执行期间并行工作流新增的 24 项白盒测试，均与本轮文件无关）。`apps/identity/test_permission_center.py` 34 项原样通过，**未修改任何既有断言**。
- **禁止项自检**：registry.py / 授权面 / 内容 hash 并发校验 / confirm token 签发与校验 / 审计规则 / 读写 API 行为均零改动（permission_center.py 无 diff）。

## 5. 遗留与建议（不在本轮处理）

1. 平台首页/运行与安全页与权限页共用 `.pf-main`，本轮 box-sizing 修复对三页同时生效；若后续平台壳需更大布局调整，建议归口 platform.css 维护者。
2. 令牌过期（403 token_expired）的浏览器 live 验证需等待 10 分钟有效期，本轮以代码路径等价性 + 服务端单测覆盖为准。
3. PC-UI-03 中 `pc-held-tag`（持有专项权限）由金色改为琥珀警示色，与「风险语义统一」一致；若设计侧希望保留平台金作为品牌点缀，可仅恢复该标签，不影响交互语义。
