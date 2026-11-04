# PMS2 平台与业务 Session Cookie 网络隔离专项设计与验收基线

> 文档状态：技术设计基线（待评审、待实施）  
> 决策状态：方案 A 已确认；预留未来独立管理子域名迁移  
> 本轮范围：只形成设计与验收基线，不修改代码、配置、数据库、路由或现有认证行为  
> 基线代码：main@8187a429ed4bc11efe2d544a0f5b5d006ee2bdec  
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
