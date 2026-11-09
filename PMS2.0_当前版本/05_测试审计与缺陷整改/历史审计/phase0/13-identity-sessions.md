# Phase 0 审计 13：身份、Session 与账号关系审计（工作流 F）

- 审计基线：`main` HEAD `100a93a`，只读审计（Django shell 仅 SELECT/统计；无写入、无 seed/迁移/改密）。
- 分类依据：方案《PMS2身份认证与员工住客账户体系改造开发方案2.0》§12.1 七分类标准（docs 同名文件 620-633 行）。
- 本报告只输出计数、用户名与布尔/日期，不含密码、Cookie/Session 值、完整手机号/证件号。

---

## 1. 账号关系统计（只读计数）

### 1.1 关系链总量

| 对象 | 计数 | 备注 |
|---|---|---|
| User 总数 | 26 | 其中 is_active=True 23 |
| StaffProfile 总数 | 30 | 其中 8 个**未关联任何 User**（legacy 迁移档案） |
| 未关联 User 的档案中持有 ACTIVE DEFAULT 任职 | 7 | 身份链异常：有任职无账号（不计入 User 七分类，单列观察） |
| PropertyStaffMembership 总数 | 29 | 全部 status=ACTIVE（dev 库无离职样本） |
| StaffRoleAssignment 总数 | 22 | 全部有效（见 12 号报告 §3.2） |
| django_session 行数 | 220 | 未过期 219；最早一条过期于 2022-04-05（长期未清理） |

### 1.2 §12.1 七分类结果

分类优先级（本审计口径，可复现）：`DISABLED`（is_active=False，优先）> `SUPERUSER_OR_TECHNICAL`（is_superuser=True）> `CONFLICT`（重复绑定/双档案/超管持业务授权/一人多 ACTIVE 任职）> `STAFF_LINKED`（档案+ACTIVE DEFAULT 任职+有效授权齐全）> `PROFILE_ONLY`（有档案无有效任职）> `UNLINKED_USER`（无档案的员工侧账号）> `PUBLIC_REGISTERED_USER`（旧公开注册入口来源）。

| 分类 | 数量 | username |
|---|---|---|
| STAFF_LINKED | 20 | front, manager, finance, owner, test_front, test_duty, test_finance, test_owner, test_housekeeping, test_nightaudit, test_fnb, test_fnbsup, test_parking, staff_gm_duty, staff_fd_01, staff_fd_02, staff_fin_01, staff_hk_01, staff_hk_02, demo_owner |
| PROFILE_ONLY | 0 | — |
| UNLINKED_USER | 2 | test（挂值班经理组+组外直授权限）、testfront（挂前台组） |
| PUBLIC_REGISTERED_USER | 0 | —（/register/ 已止血为 302 → /reserve/，不再建号，views.py:1945-1950） |
| SUPERUSER_OR_TECHNICAL | 1 | admin |
| CONFLICT | 0 | —（重复档案/多任职/超管持授权均未检出） |
| DISABLED | 3 | d07new, d07new2（身份链完整仅停用）、d07new3（无档案） |
| **合计** | **26** | |

**注**：方案 §12.2 要求的 User 盘点 management command（`--dry-run`/`--output`/分类规则版本化）**尚未实现**——`apps/identity/management/commands/` 仅有 `bind_staff_user.py`。本节分类为审计用只读查询代行，正式处置前应先补该命令。

### 1.3 关系链不一致明细（User 侧）

| 现象 | 数量 | 明细 |
|---|---|---|
| 有 StaffRoleAssignment 但 User.groups 未镜像 | 9 | staff_gm_duty, staff_fd_01, staff_fd_02, staff_fin_01, staff_hk_01, staff_hk_02, demo_owner（Lifecycle 新建，代码约定不镜像，services.py:171）+ d07new, d07new2（停用） |
| 有 User.groups 镜像但无任何任职/授权 | 2 | test（值班经理组）、testfront（前台组） |
| User 级 user_permissions 直授（绕过组） | 1 | test：hotelapp.change_order、sessions.change_session |
| StaffProfile 无 User 但有 ACTIVE 任职 | 7 | legacy 迁移档案（未开通账号，观察项，不阻塞） |

说明：运行时 PMS 授权唯一入口是 `AuthorizationService.has_permission`（只读 StaffRoleAssignment→Group，authorization.py:59-67），`User.groups` 镜像仅影响 Django Admin 侧的模型权限视角，两种口径并存（0016 演示账号镜像 vs Lifecycle 不镜像）属一致性漂移而非权限漏洞。

---

## 2. 平台超管三项不变量核查

不变量：`is_active=True + is_superuser=True + is_staff=True`（三者同时成立）。

| username | is_active | is_superuser | is_staff | 结论 |
|---|---|---|---|---|
| admin | True | True | True | **符合** |

- is_superuser=True 的用户共 1 个，**违反不变量的账号数：0**。
- 反向核查：superuser 均不持有 StaffRoleAssignment（平台身份≠门店岗位，authorization.py:41 注释约定成立）；admin 无 StaffProfile/任职，PMS 业务页不可进（staff_required 对 superuser 不豁免，views.py:108-130）。

---

## 3. 认证入口与会话边界核对（代码走查）

### 3.1 双会话隔离机制（apps/identity/session_middleware.py）

| 项 | PMS | 平台 |
|---|---|---|
| 路径域 | 其余全部路径 | `/admin(/...)`、`/permissions(/...)`（is_admin_request, 19-30 行） |
| Cookie | `sessionid`（SESSION_COOKIE_NAME） | `admin_sessionid`（ADMIN_SESSION_COOKIE_NAME, settings.py:159-161） |
| 会话存储 | 同一 django_session 后端，按各自 Cookie 取 session | 同左 |
| CSRF | `CSRF_USE_SESSIONS=True`（settings.py:162 注释明示隔离意图）→ CSRF secret 存于各自会话，两域 token 天然隔离 | 同左 |

`SplitSessionMiddleware`（33-98 行）在 process_request 按路径选 Cookie 装载 `request.session`，process_response 仅回写/删除当前域 Cookie；`/admin/login/` 走 Django Admin 标准视图，登录动作只 rotate admin_sessionid；`auth_login`（views.py:1936）只 rotate PMS 的 sessionid。**两域互不覆盖**，核对一致。

### 3.2 登出互不影响

| 入口 | 行为 | 影响域 |
|---|---|---|
| `/logout/`（views.py:1954-1957） | `@login_required` + `auth_logout(request)` → flush 当前（PMS）session，302 → `/login/` | 仅 sessionid；admin_sessionid 不受影响 |
| `/admin/logout/` | Django Admin 标准 logout，flush 当前（admin）session | 仅 admin_sessionid；PMS 登录态不受影响 |

核对一致：任一侧登出都不会注销另一侧（会话对象在中间件层已按路径隔离）。

### 3.3 `/login/`（D-04 任职门 + 限流）

- 已登录且有有效 DEFAULT 任职 → 302 `/home/`；否则展示表单（views.py:1904-1906）。
- 限流：`rate_limit.check(request, "auth.login", username=...)`，策略 `("ip","username") 5 次/300 秒`（apps/core/rate_limit.py:25 DEFAULT_LIMITS），fail-closed，超限 429（views.py:1912-1918）；失败审计 `auth.login_failure`（username 仅 HMAC 指纹，views.py:1878-1890）。DB 中 core_ratelimitcounter 已有 auth.login 计数行（限流在用）。
- D-04 任职门：`_has_active_employment`（views.py:81-105）要求「DEFAULT 任职 ACTIVE + 档案 ACTIVE + 时间窗内任一授权」；无任职登录被拒（reason=no_employment）。停用账号单独识别（reason=disabled，views.py:1921-1926）。
- next 回跳经 `_safe_login_next` 防开放重定向（views.py:1893-1899）。核对一致。

### 3.4 `/permissions/` 平台会话域

`permission_admin_page/save` 均以 `_is_superuser`（is_authenticated + is_superuser）作门禁（permission_admin.py:74-75, 88, 129），且该路径属于 admin 会话域——PMS 会话（即使持有平台组权限）不可达，必须先登录 `/admin/`。核对一致。

### 3.5 发现的不一致/风险

1. **`/admin/login/` 无登录限流**：Django Admin 标准视图无内置节流，`rate_limit.check` 仅挂在 `/login/`（PMS）。暴力破解防护在平台面缺失，建议后续为 `/admin/login/` 补齐同款限流（P1/P2 项）。
2. **PropertyContextMiddleware 对平台路径不豁免**（见 §4）：超管访问 `/admin/`、`/permissions/` 每次请求都触发一次"无 DEFAULT 任职"拒绝审计。
3. django_session 表 220 行中 219 行未过期、最早行遗留自 2022 年，`clearsessions` 未纳入例行清理（dev 库观察项）。

---

## 4. PropertyContextMiddleware 拒绝行为核对（security.property_context_denied）

代码（apps/identity/middleware.py:27-71）：

- 已认证且 is_active 的请求：查「DEFAULT 物业 + 档案 ACTIVE + 任职 ACTIVE + 物业 ACTIVE」的任职；查不到则**不提供物业上下文**（request.property=None，视图层必然授权失败），并 `AuditEvent.record(action="security.property_context_denied", reason="no_default_membership")`。
- 无静默回退：任职在别店/无任职均同样拒绝；匿名客人按 DEFAULT 物业提供公开面。行为与方案 §4.2 一致。**核对一致。**

DB 实测（只读统计）：

| 指标 | 值 |
|---|---|
| security.property_context_denied 事件总数 | 38 |
| 其中 entity_id=1（admin） | 38（100%） |
| 事件 actor 字段 | 全部为空（中间件未传 actor，仅 entity_id 记 user.pk） |
| 审计表时间范围 | 2026-11-02 16:32 ~ 2026-11-03 19:37（约 1 天） |

**发现**：38 条拒绝审计全部由超管 admin 浏览平台面产生——中间件没有为 `/admin/`、`/permissions/` 路径做豁免，超管"平台身份、无门店任职"是常态而非异常，却在每次请求写一条 security 级审计（写放大 + 审计噪声，一天 38 条），且 actor 恒为空降低可读性。建议：平台路径（is_admin_request 命中）或 is_superuser 且无员工档案的账号跳过该拒绝审计，或降级为 debug 级/一次性标记。

---

## 5. 结论（工作流 F）

1. 26 个 User 七分类：STAFF_LINKED 20、UNLINKED_USER 2（test/testfront）、SUPERUSER_OR_TECHNICAL 1（admin）、DISABLED 3、其余为 0；CONFLICT/PUBLIC_REGISTERED_USER 未检出，公开注册通道已确认止血。
2. 平台超管不变量 0 违反；superuser 与 PMS 岗位授权严格分离。
3. 双会话隔离（sessionid / admin_sessionid + CSRF_USE_SESSIONS）与双侧登出互不影响，代码与设计一致；主要缺口是 `/admin/login/` 无限流。
4. 需收尾的三类数据债：`test` 的组外 user_permissions 直授、User.groups 镜像双轨并存（13 行镜像 vs 9 个持授权账号未镜像）、方案 §12.2 盘点 management command 缺失；另有 7 个无账号 legacy 档案挂着 ACTIVE 任职待处置。
