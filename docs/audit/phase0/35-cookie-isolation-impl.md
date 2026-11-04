# 35. 平台/业务 Session Cookie 网络隔离实施记录

> 专项：PMS2 平台与业务 Session Cookie 网络隔离（方案 A）
> 依据：`docs/PMS2平台与业务Session_Cookie网络隔离专项设计与验收基线.md`
> （§B 规格表与中间件契约、§C 四动作、§D legacy 清理、§E /permissions*、§H P0 决议）
> 实施基线：main@b75df27（评审基线 909427fc；历史参考 8187a429 不作为施工依据）
> 记录日期：2026-11-05
> 范围：只覆盖服务端 Cookie 路由模型（阶段 0-4）；浏览器 E2E（阶段 5）与上线回滚演练（阶段 6）另行执行

---

## 1. 实施前配置核查记录（设计基线 §10 checklist 逐项）

| # | 核查项 | 实际结果 | 证据 |
|---|---|---|---|
| 1 | `SESSION_COOKIE_PATH` 实际值 | 未配置 → Django 默认 `/` | `hotel/settings.py` 无该键；`.env`/`.env.example` 无覆盖 |
| 2 | `SESSION_COOKIE_DOMAIN` 实际值 | 未配置 → `None`（host-only） | 同上；git 全历史无配置记录 |
| 3 | `SESSION_COOKIE_SAMESITE` 实际值 | 未配置 → `None`（无 SameSite 属性） | 同上。§4.2 要求"若实际值不同须评审"——沿用现值，未静默改变 |
| 4 | `SESSION_COOKIE_SECURE` 实际值 | `not DEBUG`（dev=False / prod HTTPS=True） | `hotel/settings.py:167` |
| 5 | `SESSION_COOKIE_HTTPONLY` 实际值 | `True` | `hotel/settings.py:166` |
| 6 | `ADMIN_SESSION_COOKIE_NAME` 实际值及部署覆盖 | 部署未覆盖（`.env` 无 `DJANGO_ADMIN_SESSION_COOKIE_NAME`），实施前默认 `admin_sessionid`；自 dc892c5 引入后默认值从未变更 | `.env`（键清单）、`git log -S ADMIN_SESSION_COOKIE_NAME` |
| 7 | 历史 admin_sessionid 的 Path/Domain 组合 | 真实历史组合仅 **Name=admin_sessionid, Path=/, Domain=host-only**（旧中间件按 `settings.SESSION_COOKIE_PATH` 写出，而该设置全历史均为默认 `/`）。`/admin/`、`/platform/`、`/permissions/`、`/permissions/save/` 为 §D.1 候选清单保守覆盖（删除指令幂等，对不存在组合无副作用）。dc892c5 之前管理页共用 Django 默认 `sessionid @ /`，与现 PMS Cookie 同名同 Path，由 PMS 域既有行为自然覆盖，无独立清理需求 | `git show dc892c5:apps/identity/session_middleware.py`、`git log -S SESSION_COOKIE_PATH/DOMAIN` |
| 8 | SplitSessionMiddleware 在 MIDDLEWARE 中的位置 | `MIDDLEWARE[1]`：SecurityMiddleware 之后、CommonMiddleware 之前，**先于** CsrfViewMiddleware 与 AuthenticationMiddleware——CSRF_USE_SESSIONS 下 CSRF Secret 正确读自本中间件选中的 Session | `hotel/settings.py:57-67` |
| 9 | Admin 登录 Session Key 轮换与 Cookie 写入时机 | AdminSite.login → LoginView.form_valid → `django.contrib.auth.login()` → `session.cycle_key()`（modified=True，数据保留含 CSRF Secret）→ 响应阶段由 SplitSessionMiddleware.process_response 保存并写双 Cookie。轮换后新 Key 双写已由测试覆盖 | 实施后测试 `test_admin_login_sets_both_platform_cookies`、`test_both_entries_authenticated_with_same_session_key` |
| 10 | Admin logout 的响应/flush/Cookie 清理 | POST `/admin/logout/` → LogoutView.post → `auth.logout()` → `session.flush()`（clear+delete，modified=True、empty=True）→ 响应走平台域删除分支（双平台 Cookie + legacy 组合全部过期删除）。GET 行为保持 Django 4.1 原语义不变 | Django 4.1 `SessionBase.flush/clear` 源码核查；实施后测试 `test_admin_logout_deletes_both_platform_cookies_and_legacy` |
| 11 | 平台门户是否存在主动登出 | **实施前不存在**；本轮新增 `POST /platform/logout/`（服务端 flush 机制，非模板 JS 方案） | `apps/identity/platform.py`（本轮变更） |
| 12 | `/permissions/` 与 `/permissions/save/` 全部 pattern、方法与真实调用方 | 当前 `hotel/urls.py` 各**仅一条** pattern（均为 RedirectView 302）。评审文档 §E 所述"重复路由"存在于旧参考基线 8187a429，当前 main 已去重（按 H.3 登记为历史问题，不在本专项恢复）。POST 无真实调用方：前端唯一保存通道是 `/platform/api/permissions/<id>/save/`（JSON API）。`resolve('/permissions/save/')` 命中 RedirectView 且路由唯一性已加防回归测试 | `hotel/urls.py:119-120`；测试 `test_permissions_routes_resolve_to_redirectview_uniquely` |
| 13 | `/permissions/save/` POST 行为冻结核实 | RedirectView.post 调 get → 302 `/platform/permissions/save/`，不读平台 Session、不写库。注意：RedirectView 非 csrf_exempt，生产环境无 Session CSRF Secret 的 POST 由 CsrfViewMiddleware 403——符合 §11.5"缺失 CSRF Token 仍拒绝，不因专项放宽"；测试 client（默认不强制 CSRF）行为为 302，与 H.3 冻结表一致 | 测试 `test_permissions_save_forged_post_changes_nothing`（零写库+零审计） |
| 14 | CSRF_USE_SESSIONS=True 下令牌生成/验证路径 | GET `/admin/login/` 渲染令牌写入 Admin 分类选中的平台 Session；POST 验证读同一 Session；登录 `cycle_key` 保留 Secret → 令牌与 Session Key 不失配。平台表单（`/platform/permissions/`、`/platform/logout/`）用门户 Cookie 选中的同一平台 Session。PMS 表单用 PMS Session——两域 Secret 不共享 | 测试 `test_admin_login_post_with_csrf_succeeds`、`test_platform_api_save_with_csrf_via_real_login_succeeds`、既有 `test_each_entry_point_uses_its_own_session_backed_csrf_secret` |
| 15 | 反向代理 / HTTPS / 日志脱敏 / 缓存 | `deploy_notes.md` 示例为标准 Nginx `proxy_pass`（默认逐条透传多个 Set-Cookie 头），无 Cookie 头记录配置，无应用层缓存。`AuditEvent.record` 仅存 request_id/ip/UA，不含 Cookie 原文；本专项代码零日志输出，无 Cookie/Session Key 落日志。缓存与日志脱敏的生产复核（§F.2/§F.3）列入阶段 6 上线检查表 | `deploy_notes.md:38-51`、`apps/core/models.py:95-111` |
| 16 | 测试环境与生产浏览器 Path 行为一致性 | 单元级：Path 为规格表常量，test client 发送全部 Cookie、由服务端分类保证隔离（测试同时断言交叉不读取）。浏览器级（真实 Path 过滤）按 §11.1 矩阵在阶段 5 用 Playwright 验证 | 本记录第 2 节 + 阶段 5 计划 |

---

## 2. Cookie 生命周期映射表（实施后）

Cookie 规格（`apps/identity/session_middleware.py` SessionCookieSpec，单一权威）：

| 规格 ID | 名称 | Path | Domain | 身份域 | 可写 | 说明 |
|---|---|---|---|---|---|---|
| PMS | `sessionid`（`SESSION_COOKIE_NAME`） | `/`（`SESSION_COOKIE_PATH`） | `SESSION_COOKIE_DOMAIN`（None=host-only） | PMS | 是 | Django 原生行为不变 |
| PLATFORM_PORTAL | `platform_sessionid`（`PLATFORM_PORTAL_COOKIE_NAME`） | `/platform/` | None（强制 host-only） | 平台 | 是 | 与 ADMIN 同一 Session Key |
| PLATFORM_ADMIN | `django_admin_sessionid`（`ADMIN_SESSION_COOKIE_NAME`，默认已改） | `/admin/` | None（强制 host-only） | 平台 | 是 | 与 PORTAL 同一 Session Key |
| LEGACY_PLATFORM | `admin_sessionid`（`LEGACY_PLATFORM_COOKIE_NAME`） | `/`、`/admin/`、`/platform/`、`/permissions/`、`/permissions/save/`（`LEGACY_PLATFORM_COOKIE_PATHS`） | `SESSION_COOKIE_DOMAIN`（与历史一致） | 历史平台 | 否（仅清理） | 每个平台域响应附加过期删除，幂等 |

安全属性：HttpOnly/Secure/SameSite 沿用全局 `SESSION_COOKIE_*` 配置；删除指令显式携带 Name/Path/Domain（+Secure/HttpOnly/SameSite），并按 Django delete_cookie 同款规则对 SameSite=None 与 `__Secure-`/`__Host-` 前缀兜底 Secure。

| 事件 | PMS `sessionid@/` | PORTAL `/platform/` | ADMIN `/admin/` | LEGACY 组合 | 服务端 |
|---|---|---|---|---|---|
| 匿名访问平台路径（302/200） | 不触碰 | 不写 | 不写 | 删除指令（每平台响应） | 无 Session 建立 |
| GET `/admin/login/`（建 CSRF Secret） | 不触碰 | 写（同键） | 写（同键） | 删除指令 | 新平台 Session |
| 登录 POST 成功（cycle_key 轮换） | 不触碰 | 写新键 | 写新键（同键同 Max-Age） | 删除指令 | 旧 Key 按 Django 轮换语义失效 |
| 登录 POST 失败 | 不触碰 | 不写有效凭证 | 同左 | 删除指令 | 无认证 Session |
| 平台路径 modified / `SESSION_SAVE_EVERY_REQUEST` | 不触碰 | 刷新 | 刷新（同键同过期） | 删除指令 | save()；UpdateError→SessionInterrupted（并发登出保护） |
| 平台域响应 500 | 不触碰 | 不写 | 不写 | 删除指令 | 不保存半完成 Session |
| 平台 Cookie 无效/过期（S3） | 不触碰 | 删除 | 删除 | 删除指令 | 匿名（302 门禁），无 500、无回退读取 |
| POST `/admin/logout/`（Django 原生） | 不触碰 | 删除 | 删除 | 删除指令 | flush → Session 失效 |
| POST `/platform/logout/`（本轮新增） | 不触碰 | 删除 | 删除 | 删除指令 | flush → Session 失效 |
| PMS `/login/`、`/logout/` | 写 / 删 | 不触碰 | 不触碰 | 不触碰 | 仅 PMS Session |
| `/permissions*`（LEGACY_REDIRECT） | 不触碰 | 不写不删 | 不写不删 | 不触碰 | 绑定未触碰空 Session，零 Cookie 操作 |
| 服务端撤销 / Session 过期 | — | 两入口均匿名（S6） | 同左 | — | Session 引擎判空，残留 Cookie 不能恢复身份 |

多 Cookie 写入契约：`set_platform_session_cookies(response, session_key, ...)` 单参数传入同一 Key（不变量 A.3.5 按构造保证）；删除契约 `delete_platform_session_cookies(response)` 逐规格 + 全部历史 Name×Path 组合发出指令，经预构建 Morsel 以独立 Set-Cookie 头逐条输出（Django 4.1 SimpleCookie 以名为键，同名不同 Path 必须独立 Morsel，否则组合互相覆盖）。

---

## 3. 代码变更清单

| 文件 | 变更 |
|---|---|
| `apps/identity/session_middleware.py` | 重写：`SessionCookieSpec` 规格表（§B.1 单一权威）、`resolve_session_cookie_spec(path)` 边界分类（/platform、/admin、/permissions* → LEGACY_REDIRECT cleanup_only、未知默认 PMS）、`SplitSessionMiddleware` 七步契约（分类读取不回退；PMS 域 Django 原语义；平台域双写同键/双清+legacy；契约函数 `set_platform_session_cookies` / `delete_platform_session_cookies` / `delete_legacy_platform_cookies`；Morsel 多 Path 删除）。`is_admin_request` 保留（PropertyContextMiddleware F-10 依赖），与分类器同口径 |
| `hotel/settings.py` | `ADMIN_SESSION_COOKIE_NAME` 默认值 `admin_sessionid` → `django_admin_sessionid`（env 未覆盖时生效）；新增 `PLATFORM_PORTAL_COOKIE_NAME`、`LEGACY_PLATFORM_COOKIE_NAME`、`LEGACY_PLATFORM_COOKIE_PATHS`（均可用 `DJANGO_*` 环境变量覆盖） |
| `apps/identity/platform.py` | 新增 `POST /platform/logout/`（`require_POST` + `platform_superadmin_required` 同款门禁 → `auth_logout()` flush → 302 平台登录页）；文档字符串同步新 Cookie 名 |
| `hotel/urls.py` | 新增 `platform/logout/` 路由；路由表注释更新 |
| `templates/platform/base.html`、`static/css/platform.css` | 平台导航新增"平台登出"POST 表单（`{% csrf_token %}`）及按钮样式 |
| `apps/identity/test_cookie_isolation.py` | **新增 38 项**单元测试（§11.2-11.5）：分类边界、规格表属性、双 Cookie 登录/双入口/交叉不读取、PMS 边界、无效 Cookie 双清不 500、服务端撤销、Admin 原生登出与 `/platform/logout/` 双清+legacy、双域互不影响、CSRF（强制开启的真实登录流 + API 保存）、/permissions* 冻结契约 + 路由唯一性、SAVE_EVERY_REQUEST 续期同步 |
| `apps/identity/tests.py`、`test_platform.py`、`test_permission_center.py`、`test_registry.py`、`test_module_gates.py`（注释）、`hotelapp/tests.py` | 既有测试中"模拟平台登录"的 Cookie 名由 admin_sessionid 更新为新规格（tests.py SplitSession 组改为真实登录流读取 django_admin_sessionid；/platform/ 组复制到 platform_sessionid + django_admin_sessionid 同键） |
| 未改动 | AuthorizationService、platform_superadmin_required 门禁规则、角色/权限数据、`/admin/login/` 登录页（不新增登录页）、CSRF 中间件与豁免面、PMS sessionid 行为 |

测试结果：`DJANGO_DB_ENGINE=sqlite manage.py test --noinput` → **Ran 447 tests, OK**（实施前同工作树基线 334 + 并行特性新增 75 + 本专项 38；本专项无回归）。

线级证据（WSGI 逐条 Set-Cookie，值不记录原文）：

```
登录响应：
  Set-Cookie: platform_sessionid;      Path=/platform/; Max-Age=1209600; HttpOnly
  Set-Cookie: django_admin_sessionid;  Path=/admin/;    Max-Age=1209600; HttpOnly   （值与上一条相同）
  Set-Cookie: admin_sessionid=; Path=/;                  Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT
  Set-Cookie: admin_sessionid=; Path=/admin/;            Max-Age=0; Expires=...
  Set-Cookie: admin_sessionid=; Path=/platform/;         Max-Age=0; Expires=...
  Set-Cookie: admin_sessionid=; Path=/permissions/;      Max-Age=0; Expires=...
  Set-Cookie: admin_sessionid=; Path=/permissions/save/; Max-Age=0; Expires=...
登出响应（/admin/logout/ 或 /platform/logout/）：上述 7 条全部转为 Max-Age=0 删除指令。
```

---

## 4. 并发轮换语义（H.2 务实一致，实施口径）

- 不承诺旧响应绝不写回旧 Key（响应乱序窗口存在）。
- `UpdateError → SessionInterrupted` 原样保留（并发登出保护，两个身份域一致）。
- 已失效旧 Key 到达服务端一律按无效 Session 处理（匿名 + 双清），不能恢复认证或提权——`test_server_side_revocation_kills_both_entries`、`test_no_fallback_to_other_platform_cookie` 覆盖。
- 未使用 LocMemCache 充当跨进程锁。

## 5. 已知边界与 P1 待办

1. `/permissions/save/` 生产 POST 在无 Session CSRF Secret 时由 CSRF 中间件 403（Django 标准语义，冻结契约未放宽）；GET/HEAD 302 跳转不受影响。
2. 平台域每个响应附加 5 条 legacy 删除指令（幂等迁移清理）；迁移完成后（建议覆盖 ≥2 个 `SESSION_COOKIE_AGE` 周期）可下线该附加。
3. 浏览器 E2E（§11.1 请求矩阵、§F.5 document.cookie/多 Set-Cookie 透传/并发轮换乱序）与代理缓存/日志脱敏生产核查属阶段 5/6，不在本次交付范围。
4. 平台登出沿用 `platform_superadmin_required` 同款门禁（任务口径）：不变量破损（如 is_staff=False）的超管在 `/platform/logout/` 会 403，仍可经 `/admin/logout/` 登出。

---

## 6. 回滚方案（§F.4）

1. **回滚版本口径**：回滚到实施前版本后，认证只识别 `admin_sessionid`（Path=`SESSION_COOKIE_PATH`=/）；旧版本**不得**读取 `platform_sessionid` / `django_admin_sessionid`，不提供新旧双认证回退（禁止长期并行接受两套平台凭证）。
2. **新 Cookie 处置**：回滚后新 Cookie 不再被读取，平台管理员**重新登录**（可接受的安全行为，设计基线 §6 明确）。如需清理浏览器残留，可临时部署"仅删除模式"中间件（只发 7 条过期删除指令），或由管理员手动清除对应站点 Cookie。
3. **部署顺序**：滚动发布期间所有实例必须对 Cookie 规格兼容；不能保证时采用维护窗口或蓝绿切换。回滚同样按此执行。
4. **服务端**：无需数据迁移；旧版重新登录自然建立新 Session，残留 Session 记录由 `SESSION_COOKIE_AGE` 过期机制回收。
5. **代理/缓存**：本部署无缓存层；回滚后确认 Nginx 未缓存带 Set-Cookie 的响应、多 Set-Cookie 头逐条透传。
6. **验证**：回滚后重新执行身份域与 Cookie 请求矩阵（单元测试 + 浏览器核对），不得以服务进程启动成功作为回滚成功标准。
