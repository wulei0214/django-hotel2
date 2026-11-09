# Phase 0 审计 12：角色授权快照与漂移分析（工作流 D）

- 审计基线：`main` HEAD `100a93a`（git rev-parse 确认），只读审计，未做任何写操作。
- 数据来源：Django shell 只读查询 local-dev MySQL（`DJANGO_DB_NAME=hotel`），SELECT/统计，无写入。
- 代码依据：`apps/identity/permission_admin.py`（矩阵）、`apps/identity/roles.py`（角色目录）、
  `hotelapp/migrations/0014_role_actions.py`、`hotelapp/migrations/0016_unify_authorization.py`、
  `hotelapp/migrations/0017_guest_pii_permissions.py`、`hotelapp/migrations/0018_finance_view_staff_readonly.py`、
  `apps/billing/migrations/0008_grant_refund_permissions.py`、`apps/core/migrations/0007_break_glass_record.py`、
  `apps/identity/migrations/0004_staff_manage_and_d04.py`、`apps/inventory/migrations/0004_rateplan_manage_rates.py`。

---

## 1. 九角色 Group 权限快照

角色目录来源 `apps/identity/roles.py:3-6`（9 组）。数据库实际共 9 个 Group（无多余组）。

| 组名 | 权限总数 | hotelapp 域 | 非 hotelapp 域 | 状态 |
|---|---|---|---|---|
| 前台 | 20 | 17 | billing.refund_request + 2 项 reveal | 现役 |
| 值班经理 | 31 | 29 | billing.refund_approve + core.breakglass_review | 现役 |
| 财务 | 13 | 12 | billing.refund_request | 现役 |
| 店长 | 39 | 35 | billing.refund_approve, core.breakglass_review, identity.staff_manage, inventory.manage_rates | 现役 |
| 客房服务员 | 2 | 2 | 无 | 现役 |
| 夜审员 | 0 | 0 | 无 | 预留空组 |
| 餐饮服务员 | 0 | 0 | 无 | 预留空组 |
| 餐饮主管 | 0 | 0 | 无 | 预留空组 |
| 停车安保 | 0 | 0 | 无 | 预留空组 |

### 完整 codename 清单（app_label.codename）

**前台（20）**
`hotelapp.view_client, add_client, change_client; hotelapp.view_vip, add_vip, change_vip; hotelapp.view_order, add_order, change_order; hotelapp.view_accommodation, add_accommodation, change_accommodation; hotelapp.view_park, add_park, change_park; hotelapp.view_food; hotelapp.view_room; hotelapp.guest_document_reveal; hotelapp.guest_phone_reveal; billing.refund_request`

**值班经理（31）**
`hotelapp.view_client, add_client, change_client; hotelapp.view_vip, add_vip, change_vip; hotelapp.view_order, add_order, change_order, delete_order; hotelapp.view_accommodation, add_accommodation, change_accommodation, delete_accommodation; hotelapp.view_park, add_park, change_park; hotelapp.view_food; hotelapp.view_room, change_room; hotelapp.room_release_ready, night_audit_execute, business_date_reopen, reverse_checkout, no_show_handle, finance_access, export_pii, guest_document_reveal, guest_phone_reveal; billing.refund_approve; core.breakglass_review`

**财务（13）**
`hotelapp.view_client, view_vip, view_order, view_accommodation, view_food, view_room, view_park; hotelapp.finance_access, export_pii, delete_order, delete_accommodation, view_staff; billing.refund_request`

**店长（39）**
`hotelapp.view_staff, add_staff, change_staff; hotelapp.view_client, add_client, change_client; hotelapp.view_food, add_food, change_food; hotelapp.view_vip, add_vip, change_vip; hotelapp.view_park, add_park, change_park; hotelapp.view_room, add_room, change_room; hotelapp.view_order, add_order, change_order; hotelapp.view_accommodation, add_accommodation, change_accommodation; hotelapp.room_release_ready, night_audit_execute, business_date_reopen, reverse_checkout, no_show_handle, finance_access, export_pii, guest_document_reveal, guest_phone_reveal; billing.refund_approve; core.breakglass_review; identity.staff_manage; inventory.manage_rates`

**客房服务员（2）**
`hotelapp.view_room, change_room`

**夜审员 / 餐饮服务员 / 餐饮主管 / 停车安保（各 0）**
无任何权限（预留空组，与 0014 RESERVED_ROLES 设计一致）。

---

## 2. 漂移分析

### 2.1 实际组权限 vs 迁移声明链（0014/0016 + 后续授权迁移）

按「0016 ROLE_PERMS + 0016 EXTRA_PERMS + 0017 guest reveal + 0018 view_staff + billing.0008 + core.0007 breakglass + identity.0004 staff_manage + inventory.0004 manage_rates」逐组比对：

| 组 | 迁移链声明数 | 实际数 | 组缺失 | 组多出（无迁移声明） |
|---|---|---|---|---|
| 前台 | 20 | 20 | 无 | 无 |
| 值班经理 | 31 | 31 | 无 | 无 |
| 财务 | 13 | 13 | 无 | 无 |
| 店长 | 39 | 39 | 无 | 无 |
| 客房服务员 | 2 | 2 | 无 | 无 |
| 夜审员/餐饮服务员/餐饮主管/停车安保 | 0 | 0 | 无 | 无 |

结论：**数据库与迁移声明链零漂移**——没有手工在后台追加、也没有迁移声明了但未落地的权限。

与 `0016.EXTRA_PERMS`（0016_unify_authorization.py:64-73）逐项核对：
- 值班经理 7 项高危、店长 10 项、财务 5 项、客房服务员 2 项——全部在组内实际存在。
- 与 `0014.ACTION_GRANTS`（0014_role_actions.py:52-60）核对：7 个高危动作 → 值班经理/店长/财务 的授予全部落地（`room_release_ready`/`night_audit_execute`/`business_date_reopen`/`reverse_checkout`/`no_show_handle` → 值班经理+店长；`finance_access`/`export_pii` → 财务+值班经理+店长）。

### 2.2 实际组权限 vs /permissions/ 矩阵页（permission_admin.MODULES/ACTIONS）

矩阵白名单 `ALL_CODENAMES`（permission_admin.py:62-63）= 8 模块 × 4 动作（32 点）+ 9 高危动作 = **41 点，全部限定 hotelapp 域**。

漂移点 1 —— **矩阵外权限"看不见但留得住"（双轨管理）**。各组实际持有 6 类矩阵域外权限：

| 权限 | 持有组 | 声明迁移 | 矩阵页可见性 |
|---|---|---|---|
| billing.refund_request | 前台、财务 | billing.0008 | 不可见，保存时保留 |
| billing.refund_approve | 值班经理、店长 | billing.0008 | 不可见，保存时保留 |
| core.breakglass_review | 值班经理、店长 | core.0007 | 不可见，保存时保留 |
| identity.staff_manage | 店长 | identity.0004 | 不可见，保存时保留 |
| inventory.manage_rates | 店长 | inventory.0004 | 不可见，保存时保留 |

矩阵保存逻辑（permission_admin.py:144-150）只整体覆盖 hotelapp 域并显式保留非 hotelapp 权限（代码注释已说明），因此保存不会清掉它们；但**这些权限不在矩阵页展示、也无法在矩阵页回收**，`perm_total` 显示为 hotelapp 域计数（如店长显示 35 而实际 39）。矩阵页不是角色权限的完整事实源——审计与运营需同时核对 Django admin 的组权限页。

漂移点 2 —— 矩阵白名单中 6 个 delete 点（`delete_client/delete_food/delete_park/delete_room/delete_staff/delete_vip`）当前无任何组持有，属矩阵允许但未使用（非缺陷，记录备查）。

漂移点 3 —— `permission_admin.ACTION_PERMS`（9 点）比 `0014.ACTION_PERMS`（7 点）多出 `guest_document_reveal`/`guest_phone_reveal`：两者通过 0017 迁移达成一致（0017 授予前台/值班经理/店长，矩阵页同步可编辑），无代码-数据不一致。

### 2.3 运行时授权路径旁路检查

- `AuthorizationService.has_permission`（apps/identity/authorization.py:37-67）只读 `StaffRoleAssignment → Group.permissions`，superuser 不绕过；`User.groups`、`User.user_permissions` 均不参与 PMS 运行时授权。
- **例外数据**：User `test` 持有组外 `user_permissions`（直授）：`hotelapp.change_order` + `sessions.change_session`。当前 test 无 StaffProfile/任职，运行时无效果；但此类直授权限绕过角色组与矩阵，矩阵页与授权审计均不可见。见工作流 F 报告 §4。

---

## 3. StaffRoleAssignment 使用统计

总量 22 条；时间基准 = 查询时 `timezone.now()`。

### 3.1 每角色在职任职数（组 + 任职 ACTIVE + 档案 ACTIVE + 物业 ACTIVE + 时间窗内）

| 角色 | 有效在职授权 | 该组总授权行数 |
|---|---|---|
| 前台 | 6 | 6 |
| 值班经理 | 3 | 3 |
| 财务 | 3 | 3 |
| 店长 | 3 | 3 |
| 客房服务员 | 3 | 3 |
| 夜审员 | 1 | 1 |
| 餐饮服务员 | 1 | 1 |
| 餐饮主管 | 1 | 1 |
| 停车安保 | 1 | 1 |

### 3.2 异常记录核查

| 检查项 | 结果 |
|---|---|
| 指向不存在组的记录 | 0（Group 外键 PROTECT，且所有组均在 9 角色目录内，无目录外组） |
| 已过期授权（ends_at <= now） | 0 |
| 未来生效（starts_at > now） | 0 |
| 档案非 ACTIVE 的授权 | 0 |
| 任职非 ACTIVE 的授权 | 0 |
| 目录外组的授权 | 0 |

### 3.3 语义漂移：预留空组已被实际分配

夜审员/餐饮服务员/餐饮主管/停车安保 4 个预留组（0 权限）各有 1 条**有效在职授权**（对应测试账号 `test_nightaudit`/`test_fnb`/`test_fnbsup`/`test_parking`，见 13 号报告 §2）。持有者可通过 D-04 登录门（有任职+有授权）但无任何业务权限。这是演示数据与"预留空组待人员到位"（0014 决策 5）的语义冲突：组预留的本意是 0 授权 0 任职，现已有任职与授权挂靠，后续在矩阵页启用这些角色时需注意这批存量持有人。

---

## 4. billing.0008 退款权限落地状态

迁移声明（billing/0008_grant_refund_permissions.py:31-38）：`refund_request → 前台、财务`；`refund_approve → 值班经理、店长`。

| 权限 | ContentType | 实际持有组 | 与声明一致 |
|---|---|---|---|
| billing.refund_request | billing/refund | 前台、财务 | 是 |
| billing.refund_approve | billing/refund | 值班经理、店长 | 是 |

其他 7 个组（含超管所在的无组状态）均不持有这两个权限点；无 User 级直授。**billing.0008 完整落地，审批链路对四组生效。**

---

## 5. 结论与建议

1. 数据面（DB 组权限）与代码面（迁移声明）零漂移，9 组 96 项权限全部可追溯至具体迁移。
2. 矩阵页只管辖 hotelapp 域 41 点；billing/core/identity/inventory 4 域 6 类权限的授予/回收只能走迁移或 Django admin，建议在方案中明确"矩阵页 + 域外权限清单"的复合事实源，或在矩阵页增加域外权限只读展示区。
3. User `test` 的 `user_permissions` 直授（含 `sessions.change_session`）建议在收尾清理中回收，保持"授权只经角色组"不变量。
4. 预留组的存量任职/授权（各 1 条）建议在角色启用专项中一并处置。
