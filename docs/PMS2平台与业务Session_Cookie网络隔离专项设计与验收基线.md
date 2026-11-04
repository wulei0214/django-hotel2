# PMS2 平台与业务 Session Cookie 网络隔离专项设计与验收基线

> 文档状态：P0 设计评审结论已确认（待实施）  
> 决策状态：方案 A 已确认；预留未来独立管理子域名迁移  
> 本轮范围：只形成设计与验收基线，不修改代码、配置、数据库、路由或现有认证行为  
> 评审基线代码：main@909427fc23e1d5cb24ca132a0141615a8490174e  
> 历史参考基线：8187a429ed4bc11efe2d544a0f5b5d006ee2bdec（仅用于追溯，不作为当前施工依据）  
> 编制日期：2026-11-05

---

## 1. 背景与当前问题

PMS2 当前主要入口：

| 入口 | 路径 | 当前身份/访问方式 |
|---|---|---|
| 客人预约 | /reserve/ | 公开预约流程及现有订单访问机制 |
| PMS 员工工作台 | /login/、/home/ 等根路径 | PMS Session，Cookie 名称 sessionid |
| 平台治理与数据维护 | /platform/*、/admin/* | 平台 Session，当前 Cookie 名称 admin_sessionid |

当前 apps/identity/session_middleware.py 中的 SplitSessionMiddleware 根据请求路径选择读取的 Session Cookie。这是服务端会话读取隔离，不等于浏览器网络层发送隔离。

当前 SESSION_COOKIE_PATH 沿用 Django 全局配置；未显式配置时为 /。因此浏览器访问 /login/、/reserve/ 等路径时，也可能在 HTTP 请求头中携带 admin_sessionid。PMS 中间件虽然不读取该 Cookie，但凭证已经到达 Web 服务器、反向代理及可能的请求日志链路。

本专项补齐平台凭证的浏览器发送范围控制，同时保持原有身份授权规则。

## 2. 已确认决策

1. 采用同域名方案：平台门户与 Django Admin 分别使用两个 Cookie 名称和两个 Path。
2. 两个平台 Cookie 指向同一个服务端平台 Session，即同一个 Session Key；不是两套独立平台登录态。
3. PMS 继续使用现有独立 Session，不与平台 Session 合并。
4. 平台 Cookie 不应发送到 PMS 根路径业务页面、公开预约页面或普通 PMS API。
5. 平台登录、Session Key 轮换、续期、失效和登出必须同步维护两份平台 Cookie。
6. 预留未来迁移到独立平台管理子域名的能力。
7. 不调整角色、权限矩阵、员工任职、平台门禁或业务授权规则。

### 2.1 方案 A 的隔离边界

由于 PMS 业务路由分散在根路径，当前 PMS Cookie sessionid 需要继续使用 Path=/。因此方案 A 的效果是：

- 平台 Cookie 不再发送到 PMS/预约路径；
- PMS 的 sessionid 仍会发送到 /platform/* 和 /admin/*；
- SplitSessionMiddleware 在平台路由仍只读取平台 Cookie，不读取 PMS Cookie。

所以本方案实现的是平台凭证不向 PMS/预约路径发送，并保持服务端双向身份读取隔离；不是两个方向都完全不携带对方 Cookie 的网络隔离。若未来要求双向传输层隔离，需要迁移 PMS 路由到独立前缀或采用不同主机名/子域名，不能随意缩窄 PMS Cookie Path。

## 3. 目标与非目标

### 3.1 目标

- /platform/* 只接收平台门户 Cookie。
- /admin/* 只接收 Django Admin Cookie。
- 两份平台 Cookie 映射同一个平台 Session，登录态一致。
- /login/、/home/、/system/、/reserve/、普通 /api/v1/* 请求不携带平台 Cookie。
- PMS 与平台登录、退出、过期、权限判断继续相互独立。
- 旧版根路径平台 Cookie 可安全迁移清理。
- 通过真实浏览器请求头和响应头完成验收。

### 3.2 非目标

- 不新增登录页面、业务页面、角色或权限点。
- 不改变 Django User、Session 后端或数据库 Session 表结构。
- 不改变 PMS sessionid 名称和根路径作用域。
- 不改变 platform_superadmin_required 的授权规则。
- 不让平台超管自动获得 PMS 任职或业务权限。
- 不把 Cookie Path 当作服务端授权控制。
- 本轮不启用独立子域名，只预留迁移能力。

## 4. Cookie 目标设计

### 4.1 Cookie 清单

| 用途 | 目标默认名称 | Path | Domain | Session 指向 |
|---|---|---|---|---|
| PMS 员工 | sessionid | / | 不设置，host-only | PMS Session |
| 平台门户 | platform_sessionid | /platform/ | 不设置，host-only | 平台 Session |
| Django Admin | django_admin_sessionid | /admin/ | 不设置，host-only | 与平台门户相同的平台 Session |

名称为目标默认值，实施前须核对部署环境、现存 Cookie 和运维配置。平台 Cookie 名称应有独立配置项，不应继续用一个 ADMIN_SESSION_COOKIE_NAME 同时表达两个路径。

两份平台 Cookie 的值相同，均为同一个 Django Session Key。它们不是独立会话，也不是双因素认证。

不设置 Domain，保持 host-only，避免平台凭证扩散到同一父域下其他子域。PMS Cookie 继续保持当前行为，本轮不收窄其 Path。

### 4.2 安全属性

| 属性 | 目标要求 |
|---|---|
| HttpOnly | 必须开启，禁止客户端脚本读取 Session Key |
| Secure | 生产 HTTPS 必须开启；本地 HTTP 开发环境按现有策略处理 |
| SameSite | 延续经验证的 Lax 策略；若当前实际值不同，实施前评审，不可静默改变 |
| Domain | 不设置，使用 host-only Cookie |
| Path | 严格按上表设置 |
| Expires/Max-Age | 两份平台 Cookie 使用同一 Session 有效期策略，并同步刷新 |

不要给 Path 非根路径的 Cookie 使用 __Host- 前缀，因为该前缀要求 Path=/，与本专项目标不兼容。

## 5. 平台 Session 双 Cookie 生命周期

### 5.1 登录与建立会话

平台登录入口继续使用 Django Admin 登录页 /admin/login/，不新增平台登录表单。

成功认证后：

1. Django 认证流程建立或轮换平台 Session Key。
2. 服务端保存该平台 Session。
3. 响应同时设置 django_admin_sessionid，Path=/admin/；以及 platform_sessionid，Path=/platform/。
4. 两份 Cookie 的 HttpOnly、Secure、SameSite、过期时间保持一致。
5. 登录后访问 /platform/ 时发送门户 Cookie；访问 /admin/ 时发送 Admin Cookie。

不得只在登录响应中设置当前请求路径对应的 Cookie，否则跳转到另一个平台路径时会出现刚登录又被当作匿名的循环。

### 5.2 请求读取

- /platform、/platform/*：只读取 platform_sessionid。
- /admin、/admin/*：只读取 django_admin_sessionid。
- PMS 根路径、/reserve/*、普通 API：只读取 PMS sessionid。
- 不允许目标 Cookie 缺失时回退读取另一个平台 Cookie 或 PMS Cookie。
- 路径分类必须集中定义并有测试，避免中间件、登录响应和登出逻辑各自维护不一致的判断。

### 5.3 Session 刷新与轮换

平台 Session 在任一平台路径被续期或轮换时：

- 服务端只维护一个平台 Session 记录；
- 两个 Cookie 必须同步写入同一个最新 Session Key；
- 两份 Cookie 的过期策略保持一致；
- 旧 Session Key 按 Django Session 轮换语义失效；
- 并发请求中旧响应不得把旧 Session Key 重新写回浏览器，需设计并发轮换保护和测试。

不能让两个路径各自创建 Session，否则平台门户与 Django Admin 登录状态会分裂。

### 5.4 登出与失效

平台登出无论从 Django Admin 还是平台门户触发，都必须：

1. 使服务端平台 Session 失效，遵循 Django 认证语义；
2. 删除 django_admin_sessionid，Path 精确为 /admin/；
3. 删除 platform_sessionid，Path 精确为 /platform/；
4. 删除历史遗留平台 Cookie；
5. 不删除或轮换 PMS 的 sessionid。

PMS 登出只清理 PMS Session，不得清理两份平台 Cookie。

### 5.5 过期与服务端撤销

- 任一平台 Cookie 对应的 Session 已过期或删除，两个平台入口均应视为未登录。
- 只删除浏览器其中一份 Cookie 不应创建新身份或会话。
- 管理员禁用平台账号、撤销平台身份或服务端清除 Session 后，两条路径均不得继续访问。
- 不允许仅凭残留 Cookie 值恢复已失效 Session。

## 6. 旧 Cookie 迁移与兼容

当前历史平台 Cookie 默认名称为 admin_sessionid，其 Path 可能为 /。浏览器 Cookie 删除必须匹配原 Cookie 的 Name、Path、Domain 组合。

迁移要求：

- 新版本停止把旧 admin_sessionid 作为有效平台凭证读取。
- 对旧 Cookie 发送过期删除指令，至少覆盖已知历史组合：Name=admin_sessionid、Path=/、Domain 与原配置一致。
- 若历史部署曾配置自定义 SESSION_COOKIE_DOMAIN 或其他 Path，必须按真实历史配置清理，不能猜测 Domain。
- 新平台 Cookie 使用新名称，避免新旧同名不同 Path 产生解析歧义。
- 迁移后要求平台管理员重新登录是可接受的安全行为。
- 不得把旧 Cookie 值复制到新 Cookie 以绕过重新认证。

回滚前必须评估浏览器中新 Path Cookie。旧版本若只识别 admin_sessionid，回滚时应要求平台重新登录或提供受控清理步骤。不得为了无感回滚而长期同时接受旧根路径 Cookie 和新 Path Cookie。

## 7. CSRF 与 Django Admin 兼容

当前项目配置 CSRF_USE_SESSIONS=True，CSRF Secret 存在所选 Session 中，而不是依赖一个跨路径的 CSRF Cookie。

实施必须确认：

- /admin/login/ 的 CSRF Token 使用 Admin 路径选中的平台 Session。
- /platform/* 表单使用门户 Cookie 选中的同一个平台 Session。
- 两个入口的 CSRF Secret 一致且可验证。
- 登录前后的 CSRF Token 轮换遵循 Django 认证安全语义。
- 登录成功后两份 Session Cookie 同步更新，不导致 CSRF Token 与 Session Key 不匹配。
- PMS CSRF Session 仍使用 PMS Session；PMS 与平台的 CSRF Secret 不得因双 Cookie 实现而共享。
- Django Admin 登录、登出、密码修改、权限矩阵 POST 等真实表单均需测试。

不得为了绕过 CSRF 失败而关闭 CSRF 中间件、扩大豁免路径或退回共享 CSRF Cookie。

## 8. 路由与兼容性专项

### 8.1 目标路由映射

| 路径 | 选用 Cookie | 备注 |
|---|---|---|
| /platform/、/platform/* | platform_sessionid | 平台门户 |
| /admin/、/admin/* | django_admin_sessionid | Django Admin |
| /login/、/logout/、/home/、/system/ | sessionid | PMS |
| /reserve/ | 不读取平台 Cookie；公开页按现有规则运行 | 平台 Cookie 不应随请求发送 |
| /api/v1/* | sessionid 或接口现有认证策略 | 平台 Cookie 不应随请求发送 |
| /permissions/、/permissions/save/ | 单独处理兼容跳转 | 见下文 |

### 8.2 旧路径别名问题

当前中间件把 /permissions/ 也识别为平台请求，但浏览器的 Path=/platform/ Cookie 不会发送到 /permissions/。

因此实施时必须核对当前路由顺序与真实行为：

- 旧 /permissions/ 若仅负责 302 到 /platform/permissions/，应在不读取平台 Session 的情况下完成跳转；目标路径再接收平台 Cookie。
- /permissions/save/ 若存在写入或兼容处理，不能未经分析直接改成公开跳转。必须核对 URL pattern、请求方法、CSRF 和历史调用方。
- 不得为了旧路径读取平台 Cookie 而把平台 Cookie Path 扩大回 /。
- 路由兼容处理必须保留原有 302 状态、目标 URL、查询参数策略和 POST 安全语义。

## 9. 预留未来独立子域名迁移

本轮不启用子域名，但设计避免把主机名硬编码到 Session 逻辑。

未来可采用：

- PMS 与预约继续使用现有业务主域名；
- 平台门户与 Django Admin 使用独立管理子域名，例如 admin.example.com；
- 平台 Cookie 使用 host-only、Path=/，不设置父域 Domain；
- PMS Session 与平台 Session 继续使用不同名称和服务端读取边界；
- 管理子域名单独配置 HTTPS、DNS、反向代理、ALLOWED_HOSTS、CSRF_TRUSTED_ORIGINS、可信代理和安全响应头；
- 不把 Cookie 设置为 .example.com，避免扩大到业务主域名及其他子域。

迁移子域名时需评估 SameSite、跨站 POST、登录跳转、CSRF Origin、静态资源和回调来源；不得把当前双 Path Cookie 直接改成父域共享 Cookie。

## 10. 实施前审查清单

开发开始前必须核对：

- [ ] 当前生产/测试环境 SESSION_COOKIE_PATH、SESSION_COOKIE_DOMAIN、SESSION_COOKIE_SAMESITE、SESSION_COOKIE_SECURE 实际值。
- [ ] ADMIN_SESSION_COOKIE_NAME 的实际值及所有部署覆盖值。
- [ ] 是否存在历史 admin_sessionid 的其他 Path/Domain 组合。
- [ ] SplitSessionMiddleware 在 MIDDLEWARE 中的位置及与 CSRF、Authentication Middleware 的顺序。
- [ ] Django Admin 登录成功时 Session Key 轮换和响应 Cookie 写入时机。
- [ ] Django Admin logout 的响应类型、会话 flush 行为和 Cookie 清理行为。
- [ ] 平台门户是否存在主动登出入口；若没有，不得虚构。
- [ ] /permissions/ 与 /permissions/save/ 的全部 URL pattern、方法和真实调用方。
- [ ] CSRF_USE_SESSIONS=True 下 Admin 和平台表单 Token 的生成、验证路径。
- [ ] 反向代理、HTTPS 终止、Cookie 头日志脱敏和缓存配置。
- [ ] 测试环境使用与生产一致的浏览器 Cookie Path 行为。

审查结果必须记录为实施前技术检查记录，不得仅凭 settings 默认值推断生产部署配置。

## 11. 自动化验收基线

### 11.1 浏览器请求 Cookie 矩阵

平台登录后，通过真实浏览器（Playwright/Selenium）检查网络请求头。预期如下：

| 请求路径 | PMS sessionid | 平台门户 platform_sessionid | Admin django_admin_sessionid |
|---|---:|---:|---:|
| /login/ | 可能发送（Path=/） | 不发送 | 不发送 |
| /home/ | 可能发送 | 不发送 | 不发送 |
| /system/ | 可能发送 | 不发送 | 不发送 |
| /reserve/ | 可能发送 | 不发送 | 不发送 |
| /api/v1/ | 可能发送 | 不发送 | 不发送 |
| /platform/ | 可能发送 | 发送 | 不发送 |
| /platform/permissions/ | 可能发送 | 发送 | 不发送 |
| /platform/system/ | 可能发送 | 发送 | 不发送 |
| /admin/ | 可能发送 | 不发送 | 发送 |
| /admin/login/ | 可能发送 | 不发送 | 发送（若已有） |

“可能发送”表示 PMS Cookie 仍为根路径作用域；中间件必须只读取当前路径所属的 Session Cookie。验收报告不得把 PMS Cookie 的存在误判为平台 Cookie 泄漏，也不得声称双向网络隔离已完成。

### 11.2 登录与会话一致性

- [ ] 匿名访问 /platform/ 按现有规则跳转 /admin/login/。
- [ ] 平台管理员在 /admin/login/ 登录成功后，响应同时设置两个平台 Cookie。
- [ ] 两个平台 Cookie 的值完全相同。
- [ ] 登录后访问 /platform/ 与 /admin/ 均保持登录，不出现重定向循环。
- [ ] 平台 Session Key 轮换后两个 Cookie 同步更新。
- [ ] 任一平台路径触发 Session 续期后，两份 Cookie 的过期策略保持一致。
- [ ] 并发请求不会把已轮换的旧 Session Key 写回浏览器。

### 11.3 身份边界

- [ ] PMS 员工只有 sessionid 时访问 /platform/，仍按现有规则跳转平台登录。
- [ ] 平台 Cookie 不会让用户获得 PMS 任职或岗位角色。
- [ ] 平台超管没有 PMS 任职时，访问受保护 PMS 页面仍被拒绝或要求 PMS 登录。
- [ ] PMS 员工不会因 is_staff 或 PMS 角色获得平台权限。
- [ ] 平台 Cookie 不发送到 /login/、/reserve/、/api/v1/*。
- [ ] 平台路由不读取 PMS sessionid。

### 11.4 登出、过期与迁移

- [ ] Django Admin 登出后，两个平台 Cookie 均删除，服务端平台 Session 失效。
- [ ] 平台门户登出（如存在/本轮实现）执行相同清理。
- [ ] PMS 登出不删除平台 Cookie。
- [ ] 平台登出不删除 PMS Cookie。
- [ ] 服务端 Session 过期/撤销后，两个平台入口均不可继续访问。
- [ ] 旧 admin_sessionid; Path=/ 被正确过期删除。
- [ ] 删除 Cookie 的 Path、Domain 与原设置完全匹配。
- [ ] 回滚步骤有验证，不会长期同时接受旧、新平台凭证。

### 11.5 CSRF 与页面功能

- [ ] Admin 登录 POST CSRF 成功。
- [ ] Admin 登出按 Django 真实行为成功。
- [ ] 平台权限矩阵保存 POST CSRF 成功。
- [ ] 非法/缺失 CSRF Token 仍返回拒绝，不因专项放宽。
- [ ] PMS 登录、退出、改密及现有 POST 不回归。
- [ ] /permissions/ 兼容跳转不会导致匿名循环或平台 Cookie Path 扩大。
- [ ] /permissions/save/ 的 GET/POST 行为与冻结的兼容策略一致。

### 11.6 工程验证

- [ ] Django 单元测试覆盖 Cookie Path、名称、删除属性和 Session Key 同步。
- [ ] 浏览器 E2E 检查真实 Request Cookie Header 和 Response Set-Cookie。
- [ ] 至少覆盖 Chromium；必要时补充 Firefox/WebKit。
- [ ] 测试生产安全配置（HTTPS、Secure、HttpOnly、SameSite）。
- [ ] 代理访问日志、应用日志不记录 Cookie 原文或 Session Key。
- [ ] CI 全部通过，并保留测试报告、浏览器网络记录和构建 SHA。

## 12. 实施顺序与交付物

1. 阶段 0：只读核查——核对当前设置、路由、登录/登出调用链、代理和旧 Cookie 实际配置，输出实施前检查记录。
2. 阶段 1：Cookie 路由模型——定义路径分类、配置项、双 Cookie 生成/读取/删除策略，先完成单元测试设计。
3. 阶段 2：登录与轮换——实现同一个平台 Session Key 的双路径 Cookie 同步设置与轮换。
4. 阶段 3：登出与迁移——同步清理两份新 Cookie 和历史根路径 Cookie。
5. 阶段 4：CSRF 与路由兼容——验证 Admin、平台矩阵、旧别名和 PMS 表单。
6. 阶段 5：浏览器 E2E——核对真实请求头、响应头、并发轮换和回归。
7. 阶段 6：上线与回滚——先测试环境，再生产灰度；记录部署参数、回滚条件和重新登录提示。

必须交付：实施前配置与路由核查记录、Cookie 生命周期映射表、代码变更清单、单元测试与浏览器 E2E 报告、脱敏后的请求/响应头证据、上线检查表、回滚方案和验收签字记录。

## 13. 风险与回滚

| 风险 | 影响 | 控制措施 |
|---|---|---|
| 登录响应只设置一个 Cookie | 另一个平台入口反复跳登录 | 登录响应同步设置两个 Cookie |
| Session Key 轮换未同步 | 两个入口登录态不一致 | 统一 Session Key 生命周期并测试并发 |
| 删除 Cookie 的 Path 不匹配 | 旧 Cookie 残留 | 按历史 Name/Path/Domain 精确清理 |
| Admin logout 只删除当前 Cookie | 另一平台 Cookie 残留 | 统一平台登出清理逻辑 |
| CSRF Session 选错 | Admin/矩阵 POST 403 | 验证中间件顺序与 CSRF Session 来源 |
| 旧别名路径分类不当 | 登录循环或边界绕过 | 对 /permissions* 单独审查 |
| 反向代理记录 Cookie | 凭证进入日志 | 日志脱敏，禁止记录 Cookie 原文 |
| 回滚后新 Cookie 不被识别 | 平台管理员无法访问 | 明确重新登录/清理新 Cookie 的回滚操作 |

回滚必须先明确新 Cookie 的失效与清理；不得长期同时启用旧根路径 Cookie 和新路径 Cookie 的双重认证回退。

## 14. 完成定义

只有同时满足以下条件，专项才可标记为完成：

1. 平台门户与 Admin 使用两个不同 Path 的 Cookie，且值映射同一个平台 Session。
2. 平台 Cookie 不出现在 PMS、预约和普通 PMS API 的真实浏览器请求中。
3. 登录、轮换、续期、登出、过期、旧 Cookie 迁移均通过测试。
4. PMS 与平台的服务端 Session 读取、认证、授权边界没有回归。
5. CSRF、Django Admin、权限矩阵和 PMS 表单均通过回归。
6. 浏览器 E2E 与 CI 通过，并留存可复核证据。
7. 生产配置与回滚步骤经过评审。

在上述标准全部满足前，状态只能标记为“设计已确认 / 实施待开始”或“实施中”，不得宣称 Cookie 网络传输隔离已完成。


---

# 设计修订：P0 可施工细化与 P1 运维验收补充

> 本节为本次评审后的强制修订，优先级高于本文前文中相对概括的描述。原方案 A 决策不变。本节补齐前，文档状态仍为“设计待完善”，不得进入阶段 1 编码。

## A. P0-1 双 Cookie 状态机

### A.1 状态定义

平台只有一个服务端 Session。两个路径 Cookie 是该 Session Key 的两个传输别名，不是两份独立会话。

| 状态 | 服务端平台 Session | 门户 Cookie（/platform/） | Admin Cookie（/admin/） | 处理 |
|---|---|---|---|---|
| S0 未登录 | 不存在/未认证 | 缺失 | 缺失 | 按原平台门禁跳转登录 |
| S1 已登录完整 | 有效 | 存在且有效 | 存在且有效 | 两个入口均认证为同一平台用户 |
| S2 部分 Cookie | 有效或未知 | 仅一份存在 | 另一份缺失 | 不创建新 Session；验证现存 Cookie，成功后补发缺失别名；失败则清理两份并按匿名处理 |
| S3 无效/过期 | 不存在、过期、签名/后端读取失败 | 一份或两份无效 | 一份或两份无效 | 不回退到其他 Cookie；安全清理并按匿名处理，不返回 500 |
| S4 Session 轮换中 | 新 Session Key 已建立，旧 Key 应失效 | 可能仍为旧值 | 可能仍为旧值 | 同一响应对两个 Path 写入同一个新 Key；禁止旧响应覆盖新 Key |
| S5 登出中 | Session 正在 flush/delete | 可能残留 | 可能残留 | 服务端先失效，再发送两份 Path 精确的删除 Cookie |
| S6 已登出 | 不存在/已失效 | 缺失 | 缺失 | 两个入口均回到匿名流程 |

### A.2 状态转换

- S0 → S1：只允许通过平台认证成功触发。认证完成、Session Key 轮换和保存成功后，同一响应设置两份 Cookie。
- S1 → S4：Django 登录安全机制触发 Session Key 轮换，或其他明确的 Session Key 变更。两份 Cookie 必须在同一响应同步写入新 Key。
- S1 → S2：浏览器只携带一份 Cookie，可能由用户清理、浏览器策略或历史部署造成。服务端验证该 Cookie 指向的 Session 后，可在当前响应补齐另一 Path 的 Cookie；不能仅凭“另一份缺失”判定用户已登出。
- S2 → S1：现存 Cookie 对应 Session 有效、用户仍满足平台超管门禁，补发缺失 Cookie。
- S2 → S3：现存 Cookie 无效、Session 不存在或账号不再满足平台门禁。清理两份 Cookie，按匿名/403 的既有门禁行为处理。
- S1/S2 → S5 → S6：平台登出或平台身份撤销。先使服务端 Session 失效，再删除两份 Cookie。
- 任意状态遇到 Session 后端暂时不可用、数据库连接错误等基础设施异常：不得把基础设施异常伪装成“无效 Cookie”并清理会话；应按正常 5xx/故障处理并记录不含凭证的错误。只有可识别的无效/过期 Session 才进入 S3。

### A.3 关键不变量

1. 平台门户与 Admin 永远映射同一个平台 Session Key。
2. 任何时候都不得因某一 Cookie 缺失而新建第二个平台 Session。
3. 不允许 Cookie 间回退：/platform/ 不读取 Admin Cookie，/admin/ 不读取门户 Cookie。
4. 不允许平台 Cookie 回退读取 PMS sessionid，反之亦然。
5. 同一响应不得为两个平台 Cookie 写入不同 Session Key。
6. Session 已在服务端失效时，残留 Cookie 不得恢复身份。
7. Cookie 值、Session Key 不得进入日志、审计详情或错误页面。

## B. P0-2 Cookie 规格表与多 Cookie 写入/删除模型

### B.1 单一规格表

实现必须维护唯一的 Cookie 规格注册表（Cookie Specification Registry），路径识别、读取、写入、刷新、删除和测试全部从同一规格表派生，不得在多个函数中散落硬编码名称与 Path。

| 规格 ID | 身份域 | 名称 | Path | 读取路由 | 写入策略 |
|---|---|---|---|---|---|
| PMS | PMS | sessionid（沿用现有配置） | / | PMS 路由 | 保持现有 Django Session 行为 |
| PLATFORM_PORTAL | 平台 | platform_sessionid | /platform/ | /platform/* | 平台 Session 保存/轮换时写入 |
| PLATFORM_ADMIN | 平台 | django_admin_sessionid | /admin/ | /admin/* | 平台 Session 保存/轮换时写入 |
| LEGACY_PLATFORM | 历史平台 | admin_sessionid | 历史 Path 集合 | 仅迁移清理，不再认证读取 | 只发送过期删除指令 |

规格表至少包含：cookie_name、path、domain、secure、httponly、samesite、身份域、读取路由分类、是否可写入、是否仅清理。Domain 必须能表达“未设置/host-only”，不能将空字符串和父域配置混为一谈。

### B.2 SplitSessionMiddleware 职责

SplitSessionMiddleware 应按以下顺序工作：

1. 依据规范化后的 request.path_info 分类为 PMS、平台门户、Django Admin、兼容跳转或其他公开路径。
2. 只从当前分类允许的 Cookie 名称读取 Session Key。
3. 使用现有 Django Session Engine 加载对应 Session；不得自行解析或拼接 Session 数据。
4. 将当前请求的 Session 对象绑定到 request.session，保持 Django Authentication/CSRF Middleware 的既有依赖顺序。
5. 响应阶段按当前身份域的 Cookie 规格写入、刷新或删除 Cookie。
6. 平台域写入时对 PLATFORM_PORTAL 与 PLATFORM_ADMIN 两条规格执行多 Cookie 写入，值必须相同。
7. 删除操作按规格逐项调用响应的 delete_cookie，明确传入匹配的 Path、Domain、Secure、SameSite 参数；不得只删除当前请求路径的 Cookie。
8. PMS 响应不得写入或删除平台 Cookie，平台响应不得写入或删除 PMS Cookie，除明确的历史平台 Cookie 迁移清理外。

### B.3 多 Cookie 写入/删除接口约束

建议在中间件内部抽取可单元测试的私有操作（名称由实现人员决定）：

- set_platform_session_cookies(response, session_key, expiry)：遍历两条平台 Cookie 规格，设置相同 session_key 和一致的过期属性。
- delete_platform_session_cookies(response)：遍历两条平台规格及历史迁移规格，逐项发出删除指令。
- set_pms_session_cookie(response, session_key, expiry)：沿用 PMS 现有行为，不复用平台规格。
- resolve_session_cookie_spec(path)：只返回唯一的读取规格；对兼容路径返回明确的兼容分类，不得模糊归入平台主路径。

这些是职责契约，不要求必须使用上述函数名。所有 Set-Cookie/Delete-Cookie 行为都必须可测试。

### B.4 写入时机与 Django 兼容

- 必须兼容 Django SessionMiddleware 的 save、modified、accessed、SESSION_SAVE_EVERY_REQUEST、浏览器关闭时会话等语义。
- 不能简单地每个请求无条件重写 Cookie，避免缓存污染、响应头膨胀和并发旧值覆盖。
- Session Key 发生轮换时，必须在轮换后的响应同步写入两份新 Cookie。
- 如果响应状态为 500，不得因为中间件保存逻辑而持久化一个半完成 Session；遵循 Django 当前版本 SessionMiddleware 的错误处理语义。
- 若 Session 保存抛出 UpdateError/SessionInterrupted，应保留明确的并发登出保护语义，不得吞掉异常后继续发放旧 Cookie。
- 不允许在一个响应中混用不同的 max-age/expires；两份平台 Cookie 必须完全一致。

## C. P0-3 四个关键动作的行为契约

### C.1 登录

- 认证入口继续为 /admin/login/，不增加第二套平台认证。
- 认证成功后由平台 Session 域创建/轮换 Session。
- 同一响应必须设置两个平台 Cookie，两个 Cookie 值相同、Path 各自准确。
- 登录失败不得创建已认证平台 Session，不得留下其中一份有效 Cookie。
- 登录成功后的重定向目标必须是本地允许路径，不能引入开放重定向。
- 测试：登录响应 Set-Cookie 数量、名称、Path、属性、值相等；随后分别访问 /platform/ 和 /admin/ 均为同一用户。

### C.2 登出

- Django Admin 原生 logout 行为必须先核实，不能假设 Django Admin 会自动清除第二份 Cookie。
- 平台登出必须使服务端平台 Session 失效，并在同一响应清除两份平台 Cookie。
- 若 Admin logout 由 Django 内置视图处理，应通过受控扩展/包装确保双 Cookie 删除，不得仅靠模板 JS 或客户端跳转。
- PMS logout 只清理 PMS Cookie，不清理平台 Cookie。
- 测试：从 /admin/ 发起登出后，两个平台入口都失效；从 PMS 登出后，平台两个入口仍有效。

### C.3 Session Key 轮换

- 登录认证成功、密码变更等 Django 认证安全流程可能触发 cycle_key/flush，必须覆盖真实调用路径。
- Session Key 轮换必须在响应中同步写入两个新 Cookie。
- 并发请求可能出现响应乱序：旧请求不得在新 Key 轮换后将旧 Key 再次写回浏览器。实施前必须明确并验证并发控制策略；不能只依赖“同一个响应 set 两次”就宣称解决。
- 测试：并发触发轮换与普通访问，模拟响应乱序，最终浏览器 Cookie 仍是有效的新 Key。

### C.4 无效 Cookie

必须区分以下情况：

1. Cookie 缺失：匿名流程，不是异常。
2. Session Key 对应记录不存在/已过期：清理当前平台 Cookie 规格，并同步清理平台另一规格，按匿名流程处理。
3. Cookie 格式异常或后端无法解码且被 Session Engine 明确判定为无效：清理两份平台 Cookie，按匿名流程处理。
4. Session Store、数据库、缓存基础设施故障：不能当作无效 Cookie；应保留故障响应并告警，避免误删有效登录态。
5. 用户已登录但不满足平台超管门禁：按现有 403 与审计规则处理，不得把它降级成匿名后自动跳登录。

所有无效 Cookie 路径均不得产生 500、无限重定向、登录循环或跨域身份回退。日志只记录分类、请求 ID、路由类别和异常类型，不记录 Cookie 原文、Session Key 或完整请求头。

## D. P0-4 旧 Cookie 清理：按历史 Path/Domain 组合清理

“清理所有路径”不能理解为服务端可以枚举浏览器 Cookie Jar 中任意未知 Path。浏览器不会在请求中告知 Cookie 原始 Path；服务端只能根据部署历史与配置清单发送删除指令。因此必须建立**历史 Cookie 作用域清单**。

### D.1 清理范围

至少盘点并覆盖以下候选 Path（最终以 Git 历史、部署配置、历史响应头和运维记录确认）：

- /
- /admin/
- /platform/
- /permissions/
- /permissions/save/
- 旧版本曾配置过的其他明确 Path
- 空 Path/框架默认 Path 的历史实际结果（如存在，需验证浏览器行为）

对每个历史 Cookie Name × Path × Domain 组合分别发出过期删除指令。不能只清理 admin_sessionid; Path=/。

### D.2 历史名称与域名

- 历史名称至少核查 admin_sessionid、曾配置过的 ADMIN_SESSION_COOKIE_NAME 值，以及任何曾用于平台 Session 的别名。
- Domain 维度至少核查 host-only（不设置 Domain）和历史部署实际设置过的每个 Domain。
- 不得凭空向未知父域发送删除 Cookie；Domain 必须来自可证实的历史配置。
- 如果历史环境组合无法确定，交付风险说明和管理员侧清理指引，不得宣称“所有浏览器残留已清除”。

### D.3 清理策略

- 迁移响应中为所有已知历史组合发送过期 Set-Cookie。
- 新版本认证读取逻辑不再接受旧名称，不设置旧 Cookie。
- 删除响应必须核对浏览器最终 Cookie Jar，证明同名不同 Path 的旧 Cookie 已消失。
- 清理操作可幂等重复执行；已经不存在的 Cookie 再删除不得引发异常。
- 测试至少构造同名 admin_sessionid 在多个 Path/Domain 下并存的浏览器场景。

## E. P0-5 /permissions* 兼容路径分类

当前 URL 配置需要特别注意：/permissions/ 先注册 RedirectView；/permissions/save/ 既有一个 RedirectView 路由，又在后续出现同一路径到 permission_admin_save 的直接路由。Django URL Resolver 按顺序匹配，后者可能不可达；必须在阶段 0 核实并记录实际命中路由，不能仅按注释判断。

### E.1 分类表

| 路径 | 分类 | 方法 | Session 选择 | 目标行为 |
|---|---|---|---|---|
| /permissions/ | LEGACY_PLATFORM_REDIRECT | GET/HEAD | 不加载平台 Session | 302 到 /platform/permissions/ |
| /permissions/save/ | LEGACY_PLATFORM_REDIRECT 或 LEGACY_PLATFORM_POST（待阶段 0 判定） | GET/HEAD/POST 分别核验 | 不得默认加载平台 Cookie | 保留既有兼容契约；若 POST 仍有真实调用方，必须在平台目标路径执行授权和 CSRF，不得将 POST 无条件 302 丢失语义 |
| /platform/permissions/ | PLATFORM_PORTAL | GET | platform_sessionid | 平台门禁与权限矩阵展示 |
| /platform/permissions/save/ | PLATFORM_PORTAL | POST | platform_sessionid | 平台门禁、权限保存授权、CSRF |

### E.2 实施约束

- 兼容路径必须在 Cookie 路由分类器中显式列出，不能用 startswith('/permissions') 简单并入平台 Path。
- GET/HEAD 纯跳转不得要求浏览器发送平台 Cookie；跳转目标收到请求后才发送 Path=/platform/ Cookie。
- POST 不得在没有方法级评估的情况下由 RedirectView 自动处理。先确认当前调用方、状态码、请求体、CSRF 与幂等影响，再冻结保留/拒绝/迁移行为。
- 必须清查重复 URL pattern，最终保证每个兼容路径只有一个有效路由定义。
- 验收需验证 /permissions/save/ 实际解析到的 view、允许方法、状态码、Location、CSRF 与权限行为。

## F. P1 补充：路由、缓存、日志、审计、回滚与浏览器验收

### F.1 路由

- 维护完整路径分类清单：平台门户、Django Admin、PMS、公开、兼容跳转、静态资源、未知路径。
- 路由匹配必须使用 path_info 的边界匹配，避免 /administrator/ 被误判为 /admin/。
- /admin 与 /platform 的末尾斜杠、子路径、404、静态资源和媒体资源均需覆盖。
- 未分类路径默认不得读取平台 Cookie；不允许“未知即平台”。
- 认证重定向必须避免循环，登录页自身必须能在无 Session 状态下正常加载。

### F.2 缓存与代理

- 所有依赖 Session 的平台响应应设置适当的 Cache-Control（至少 private，敏感页面 no-store 按现有安全策略评估）。
- 代理/CDN 不得缓存带 Set-Cookie 的登录、登出、权限矩阵或平台个人响应。
- 反向代理必须透传多个 Set-Cookie 响应头，不能把它们错误合并成一个 Cookie。
- 缓存键不能忽略身份上下文；平台认证页面不得被公共缓存命中。
- 验收通过浏览器与代理两侧检查多个 Set-Cookie 头完整保留。

### F.3 日志与审计

- 禁止记录 Cookie 请求头、Set-Cookie 响应头、Session Key、CSRF Token。
- 代理 access log、应用 request log、异常追踪平台均需检查脱敏配置。
- 登录成功/失败、平台登出、Session 失效、无效 Cookie 清理、管理员禁用等安全事件按现有 AuditEvent 能力记录必要元数据。
- AuditEvent 只记录操作者、事件类型、结果、请求 ID、IP（按现有策略）、路由类别；不得写 Cookie 值、Session Key、CSRF Token。
- 无效 Cookie 的高频请求可做限速/聚合告警，避免审计日志被刷爆。
- Session 后端故障与用户 Cookie 无效必须区分事件类型。

### F.4 回滚

- 回滚前确认新版本发出的两份 Path Cookie 的名称和作用域。
- 回滚版本若只识别旧 admin_sessionid，不得自动接受新 Cookie 值作为认证凭证。
- 回滚方案应明确：清理新 Cookie、旧版本重新登录、部署顺序、代理缓存清理和验证步骤。
- 不允许新旧版本长期并行接受多套平台 Cookie；滚动发布期间需保证所有实例对 Cookie 规格兼容，或采用维护窗口/蓝绿切换。
- 回滚后重新执行身份域与 Cookie 请求矩阵，不能仅以服务进程启动成功作为回滚成功。

### F.5 真实浏览器验收

必须用 Playwright 或 Selenium 运行真实浏览器，检查：

- document.cookie 不可读取 HttpOnly 平台 Cookie；
- 对 /login/、/reserve/、/api/v1/* 的真实 Request Headers 不包含两种平台 Cookie；
- 对 /platform/* 只发送 platform_sessionid，不发送 django_admin_sessionid；
- 对 /admin/* 只发送 django_admin_sessionid，不发送 platform_sessionid；
- Set-Cookie 响应包含两份不同 Path 的 Cookie，值相同；
- Cookie 部分缺失时能补齐，不发生登录循环；
- 无效/过期 Cookie 不导致 500；
- 平台登出后两份 Cookie 与服务端 Session 均失效；
- PMS 登出不影响平台会话，平台登出不影响 PMS 会话；
- 多 Path 同名旧 Cookie 清理后浏览器 Cookie Jar 中不残留；
- 并发轮换响应乱序后最终 Cookie 仍有效；
- 代理层没有合并 Set-Cookie 或缓存认证响应。

测试证据必须脱敏，保留构建 SHA、浏览器版本、测试用例编号、Request/Response Cookie 名称与 Path、状态码和结果；绝不保存 Cookie 值。

## G. 阶段门槛与状态更新

### G.1 P0 设计完成门槛

以下五项全部落实并通过设计评审，才允许进入阶段 1（实现 Cookie 路由模型与单元测试）：

- [ ] P0-1 双 Cookie 状态机及不变量。
- [ ] P0-2 Cookie 规格注册表与 SplitSessionMiddleware 多 Cookie 写入/删除契约。
- [ ] P0-3 登录、登出、轮换、无效 Cookie 四类动作契约。
- [ ] P0-4 历史 Cookie Name × Path × Domain 清理盘点策略。
- [ ] P0-5 /permissions* 兼容路径分类及重复路由核查要求。

### G.2 P1 施工前/实施验收门槛

- [ ] 路由全分类和未知路径默认策略。
- [ ] 代理/CDN 缓存与多 Set-Cookie 透传。
- [ ] 日志脱敏与 AuditEvent 事件边界。
- [ ] 回滚/滚动发布策略。
- [ ] 真实浏览器 E2E 和证据归档。

### G.3 当前状态

本次修订只更新设计文档，不修改代码、设置、路由、数据库或认证行为。P0-1 至 P0-5 已形成设计契约，但仍需技术评审确认；阶段 1 尚未开始。P1 为实施前必须落地的工程与验收要求。

**不得将“文档补齐”描述为 Cookie 隔离已实施或已完成。**


---

# P0 设计评审决议（2026-11-05）

> 本节记录产品/技术评审已确认的决议；如与前文概括性描述冲突，以本节为准。本节只冻结设计契约，不授权开始编码。

## H.1 部分 Cookie 补发策略：已确认

| 当前请求状态 | 处理规则 | 是否允许跨路径恢复 |
|---|---|---|
| 当前路径 Cookie 有效，且平台用户通过门禁 | 正常认证；在登录成功、Key 轮换或 Session 修改等正常写入机会，同时写入两份平台 Cookie，值为同一 Session Key | 否；只是从当前已认证 Session 向另一 Path 补发别名 |
| 当前路径 Cookie 缺失 | 按匿名处理并执行既有登录流程 | 不允许读取另一份平台 Cookie，也不允许回退读取 PMS sessionid |
| 当前路径 Cookie 无效，或服务端 Session 不存在/已过期 | 清理当前路径 Cookie，按匿名流程处理；不得返回 500 | 不允许 |
| 另一路径 Cookie 缺失 | 当前请求无法获知浏览器是否持有另一 Path Cookie；不做推断 | 不允许在本次请求中自动补发 |
| 两份 Cookie Key 分叉 | 当前请求只以当前路径 Cookie 对应的 Key 为认证依据；平台登出时删除两份浏览器 Cookie，并使当前服务端 Session 失效 | 不允许自动跨路径探测或恢复 |
| 匿名请求返回 302/403/500 | 不得写入另一路径 Cookie，不得用空 Session 覆盖已有登录态 | 不允许 |

**冻结原则：补发只能在已认证请求上向前修复，不能用另一份 Cookie 反向恢复认证。**

若未来要求在两个 Path 间强一致地检测和修复 Key 分叉，必须另行设计服务端会话族/版本机制，不属于本阶段承诺。

## H.2 并发 Session 轮换：Phase 1 采用务实一致

评审选择务实一致，不选择本阶段强一致：

1. 不承诺旧响应绝不把旧 Cookie Key 写回浏览器。
2. 必须保证已经失效的旧 Key 不能重新获得认证。
3. 旧 Key 再次到达服务端时，必须按无效 Session 处理，不得造成身份恢复或权限提升。
4. 必要时允许用户重新登录。
5. 不使用 LocMemCache 充当跨进程锁。
6. 若未来产品要求强一致，另行设计数据库锁或服务端版本机制，并单独评审、测试和估算成本。

因此，前文“并发请求中旧响应不得把旧 Session Key 重新写回浏览器”及同类绝对保证，均调整为上述务实一致验收语义。Phase 1 的核心验收不是零旧 Key 写入，而是**旧 Key 不能重新获得认证，不能造成权限提升**。

## H.3 /permissions/save/ 兼容行为：已冻结

| 路径 | 方法 | 冻结行为 | Session/写入边界 |
|---|---|---|---|
| /permissions/save/ | GET/HEAD | 302 到 /platform/permissions/save/ | 不读取平台 Session，不写数据库 |
| /permissions/save/ | POST | 302；不执行权限保存 | 不读取平台 Session，不写数据库 |
| /platform/permissions/save/ | 按当前路由支持的方法 | 经过平台门禁后 302 回权限矩阵页，不执行权限保存 | 不写入权限数据 |
| /platform/api/permissions/<id>/save/ | JSON API | 唯一允许执行真正权限保存的入口 | JSON + CSRF + 平台门禁及对应授权 |

补充约束：

- Django 4.1 的 RedirectView.post 调用 get；当前 main 的既有测试与此兼容行为一致。旧路径不得恢复 permission_admin_save，也不得为了兼容旧表单而静默将 POST 转为继续写权限。
- 路由唯一性防回归测试：resolve('/permissions/save/') 必须解析到 RedirectView；同名 path 只允许一条 URL pattern。
- 伪造 POST 防回归测试：向 /permissions/save/ 提交 role/permissions，确认 Group.permissions 零变化，且不产生权限变更审计写入。
- 当前 main 的重复路由问题按历史问题登记；不以旧基线的重复路由状态指导当前实现，也不在本专项中恢复旧写入行为。
- 本文冻结的是兼容契约；上述测试作为实施验收要求，不代表本次已执行测试或修改代码。

## H.4 P0 评审退出条件与当前结论

| 退出条件 | 结论 |
|---|---|
| 部分 Cookie 补发状态表签字确认 | 已确认，见 H.1 |
| 并发轮换语义选定 | 已选择务实一致，见 H.2 |
| /permissions/save/ 行为矩阵冻结，重复路由作为历史问题记录 | 已确认，见 H.3 |
| 评审基线更新 | 采用 main@909427fc23e1d5cb24ca132a0141615a8490174e；8187a429ed4bc11efe2d544a0f5b5d006ee2bdec 仅标记为历史参考基线 |
| 以上完成前不写实现代码 | 本次只更新设计文档；未修改认证代码、中间件、路由、数据库或部署配置 |

**当前状态：P0 设计评审决议已确认；阶段 1 编码尚未开始。** 后续若要进入实现阶段，必须由项目负责人另行明确授权；本决议本身不构成编码授权。
