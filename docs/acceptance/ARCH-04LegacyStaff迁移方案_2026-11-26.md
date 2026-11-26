# ARCH-04 LegacyStaff 渐进迁移方案（2026-11-26）

> 文档编号：PMS2-ARCH-04-PLAN-001 ｜ 类型：ACCEPTANCE ｜ 状态：ACTIVE ｜ 版本域：PMS2.0 ｜ 业务模块：架构治理
> 任务依据：《PMS 2.0 剩余事项裁定执行、G14 最终验收与 PMS 3.0 启动准备任务书》PMS2-FINAL-DECISION-EXEC-002 §3.3（DEL-08）
> 裁定口径：**渐进迁移，保留历史数据** —— 本轮不删旧表、不破坏历史关联；新业务统一以 `StaffProfile` + `EmployeeLifecycleService` 为正式入口；**禁止新增**直接依赖旧 Staff 表的业务代码。
> 基线：`main` `0ca1ce4` ｜ 证据来源：全仓只读盘点（代码/迁移/命令/测试四类），下述每条均给 `文件:行号`

---

## 一、总体判断

旧 `Staff` 表（`db_table='staff'`）在 PMS2.0 中**已退化为只读历史档案**，但尚未达到可退役条件：

- ✅ **写路径已清零**：全仓生产代码中旧表的 `create/update/save/get_or_create` 命中 **0** 处。
- ✅ **审计已解耦**：`AuditEvent.entity_type` 中不存在 `"Staff"`，审计只使用新模型主键。
- ❌ **读路径仍在**：11 处生产代码 + 2 个管理命令 + 1 个新员工中心页面依赖旧表。
- ❌ **反向 FK 阻断删表**：`Room.负责人编号` / `Food.厨师id` / `Order.服务员编号` 三处 `ForeignKey('Staff', DO_NOTHING)`，外加 `StaffProfile.legacy_staff` 的 `PROTECT` OneToOne。
- ❌ **权限码仍绑旧 ContentType**：新员工中心 registry 仍引用 `hotelapp.view_staff` 等 4 个旧权限码，删表会连带删权限 → 新员工中心 403。
- ❌ **两表可长期漂移且无告警**：旧表与新模型之间**不存在任何同步机制**（零信号 / 零 worker / 零触发器），唯一写入是 2026 年的一次性数据迁移。

因此本轮只做「映射规则 + 迁移/回滚/核对方案 + 退役条件」三件事，**不动旧表数据、不改旧路由**。

## 二、旧 Staff 模型本体

| 项 | 事实 | 证据 |
|---|---|---|
| 定义 | `class Staff(models.Model)`，`db_table='staff'` | `hotelapp/models.py:280-308` |
| 字段 | `员工id`(AutoField pk，verbose_name="工号")、`姓名`、`启用`(bool)、`职务`(中文自由文本)、`月薪`(**字符串存数值**)、`工龄`(字符串) | 同上 |
| 密码 | **无密码字段** —— 登录凭据一直在 `auth.User`，旧表从不承担认证职责 | 同上 |
| 唯一约束 | **无**（除主键外均无 `unique`/`db_index`） | 同上 |
| 时间/审计列 | **无**（无入职/离职/创建时间、无审计列） | 同上 |
| 迁移仍建表 | `0002_auto_20220321_2039.py:80-92` 建表；`0012:215-219` 加 `启用`；`0013:264-288` 改字段；**无删除/冻结迁移** | `hotelapp/migrations/` |
| 阻断删表的 FK | `Room.负责人编号`(:258)、`Food.厨师id`(:103)、`Order.服务员编号`(:121) 三处 `DO_NOTHING`；`StaffProfile.legacy_staff`(:28-36) `PROTECT` | `hotelapp/models.py`、`apps/identity/models.py` |
| 权限码绑定 | `view_staff` / `export_approve` 建在 `ContentType("hotelapp","staff")` 上；新员工中心 registry 仍引用 `hotelapp.view_staff/add_staff/change_staff/delete_staff` | `hotelapp/migrations/0018:10-18`、`0019:19,34`、`apps/identity/registry.py:201-204` |

## 三、全仓库引用盘点

### 3.1 生产代码中的 ORM 引用

| 文件:行号 | 引用 | 读/写 |
|---|---|---|
| `hotelapp/views.py:42` | `from .models import (Staff, ...)` | — |
| `hotelapp/views.py:195,196` | `Staff.objects.get(员工id=sid, 职务=job)` | 读 |
| `hotelapp/views.py:221,222` | `Staff.objects.order_by(...)` + `filter(启用=...)` | 读 |
| `hotelapp/views.py:316-323` | CSV 导出逐行读 `员工id/姓名/职务/月薪/工龄/启用` | 读 |
| `hotelapp/views.py:355,357` | `_search(Staff, "员工id", ...)` | 读 |
| `hotelapp/admin.py:12,86-95,142` | `StaffAdmin` 注册（`NoAdminDeleteMixin`） | 读 |
| `apps/identity/staff_center_views.py:115,126` | `LegacyStaff.objects.filter(pms_staff_profile__isnull=True)` —— 查「未映射旧员工」 | 读 |
| `apps/identity/staff_center_views.py:140,151` | `LegacyStaff.objects.filter(员工id=legacy_id).first()` —— 建档反查 | 读 |
| `hotelapp/management/commands/audit_employee_data.py:14,29,30,75,76` | 只读盘点（输出 CSV，不改库） | 读 |
| `hotelapp/management/commands/seed_hotel_data.py:211,213` | 只读旧表以填充 `LegacyRecordLink` | 读 |
| `apps/identity/migrations/0001_initial.py:63,94` | 一次性历史迁移 | 读 |

**生产写路径 = 0 处。**

### 3.2 Seed / Fixture / 归档通道

| 项 | 事实 | 证据 |
|---|---|---|
| fixtures | **全仓无 `fixtures/` 目录**，无任何 JSON/YAML fixture 引用 Staff | `glob **/fixtures/**` → 0 |
| 旧表写入 | 仅 `apps/identity/migrations/0001_initial.py:92-111` 的一次性 `RunPython`（**只读旧表**）；reverse = `noop`（`:199`） | 同上 |
| 归档通道 | `apps/billing/legacy_archive.py:111-201` 的 `ARCHIVE_SPECS` **不含 Staff**（仅 Folio/FinanceRecord/NightAudit/Accommodation/Client/Vip） | 同上 |
| 权限迁移 | `0018`/`0019` 在旧 ContentType 上建权限码 | `hotelapp/migrations/` |

### 3.3 管理命令

| 命令 | 引用旧 Staff | 性质 |
|---|---|---|
| `hotelapp/management/commands/seed_hotel_data.py` | **是（只读）** | 写 `LegacyRecordLink`（`source_model="Staff"`），**不写旧表** |
| `hotelapp/management/commands/audit_employee_data.py` | **是（只读）** | 只读出 CSV |
| `seed_role_test_accounts.py` / `classify_users.py` / `reconcile_test_users.py` / `bind_staff_user.py` | 否 | 只用新栈 |

### 3.4 测试引用（32 命中 / 11 文件）

| 测试文件 | 命中 | 用途 |
|---|---|---|
| `hotelapp/tests.py` | 8 | 造数 + Admin 权限 + 薪酬可见性 |
| `apps/identity/test_module_gates.py` | 5 | 零写入探针（旧表计数基线） |
| `hotelapp/test_legacy_read_contract.py` | 3 | 旧读契约造数 |
| `hotelapp/test_m4_2_readonly.py` | 2 | 旧 Room 只读回归 |
| `apps/api/test_legacy_equivalence.py` | 2 | 新旧读路径等价对比（Staff 仅作造数道具） |
| `apps/api/test_partial_contract_verification.py` | 2 | 部分契约核验 |
| `apps/billing/test_legacy_archive.py` / `_query.py` / `test_legacy_commands.py` / `test_legacy_order_map.py` / `test_legacy_restore_drill.py` | 各 2 | 仅作 FK 造数道具 |

**零命中（重要否定事实）**：`hotelapp/test_m5_2_cleanup.py`（**不含任何 Staff 断言**）、`apps/identity/test_staff_center.py`（**零**旧表引用）、`apps/api/staff_center.py`（零 `LegacyStaff`）。

### 3.5 特别核查项（任务书点名）

**① `apps/identity/staff_center_views.py`** —— 依赖旧表 **2 处，均为只读**：
- `:126` 反向关系查「未映射旧员工」→ 渲染 `templates/pms/staff_center.html:53-57` 的下拉；
- `:151` 按工号反查旧行 → 作为 `EmployeeLifecycleService.create_employee(legacy_staff=legacy, ...)`（`:164`）入参 → 写入 `StaffProfile.legacy_staff`。
该文件**从不写**旧 Staff 表。

**② `hotelapp/management/commands/seed_hotel_data.py`** —— **不写旧表**（零 `Staff.objects.create/update/save/get_or_create`）。它只读旧表并写 `LegacyRecordLink`（`:223-227`），`mapping_status` 取值：`LINKED` / `MATCH_CANDIDATE` / `UNREVIEWED`（`:214-221`）；无对应 StaffProfile 时直接 `continue`（不建 link，`:221-222`）。
**与员工生命周期服务的关系 = 互斥，不是双写**：该命令不调用 `EmployeeLifecycleService`、不创建 StaffProfile/Membership/StaffRoleAssignment/User（`:97-99` 显式声明）。员工域写入的唯一通道是 `seed_role_test_accounts.py:121-186`（只写新栈）。

## 四、旧→新模型映射规则

| 旧 `Staff` 字段 | 新模型落点 | 规则 | 现状 |
|---|---|---|---|
| `员工id`（pk，全局自增） | `PropertyStaffMembership.employee_no`（物业内唯一业务号） | 一次性迁移写作 `str(legacy.pk)`；**语义不等价**（旧=全局 pk，新=物业内业务号，新号由 `EmployeeNumberCounter` + `generate_employee_no()` 按「物业+部门代码+4 位流水」生成） | 已写入，但新旧号体系并存 |
| `姓名` | `StaffProfile.full_name` | 直接映射 | `0001_initial.py:95-102` |
| `启用`(bool) | `StaffProfile.employment_status`（ACTIVE/INACTIVE/TERMINATED）+ `PropertyStaffMembership.status`（ACTIVE/SUSPENDED/TERMINATED） | **粒度提升为两层**；旧单开关→新「全局雇佣状态 × 单物业任职状态」 | **迁移时丢失**：`:100` 恒置 ACTIVE，旧「停用」语义未迁移 |
| `职务`（中文自由文本） | `PropertyStaffMembership.position_code`（受控码，默认 `UNASSIGNED`）+ `StaffRoleAssignment.group` | **三重不等价**：旧职务 ≠ 新岗位 ≠ 新授权；受控码需映射表，未识别值归 `UNASSIGNED` | **脏数据**：`:108` 把中文串原样塞进 `position_code` |
| `月薪` | **无对应字段** | 需负责人裁定：迁移到新模型 / 保留在旧表 / 退役 | **未迁移**，新模型零承载 |
| `工龄` | **无对应字段** | 同上 | **未迁移** |
| （旧表无） | `StaffProfile.user` / `phone` / `must_change_password` / `last_password_change` / `legacy_staff` | 新增能力，无旧来源 | 已具备 |
| （旧表无） | `PropertyStaffMembership.started_at/ended_at`、`StaffRoleAssignment.starts_at/ends_at/granted_by` | 时间维度与授权有效期 | 已具备 |

**权限维度**：旧表零权限字段；旧权限靠 Django Group + 4 个绑旧 ContentType 的权限码。新模型用 `StaffRoleAssignment`（任职 × Group × 有效期 × 授权人）+ `AuthorizationService` + `PermissionMatrixApproval`（SoD 审批）完整覆盖。

**审计维度**：旧表无审计列，旧操作审计只写 `logs/audit.log` 文本；新审计只落 `AuditEvent(entity_type="StaffProfile"/"StaffRoleAssignment")`。**旧表主键不进审计表**，只以字符串形态出现在 `LegacyRecordLink.source_pk` 与 `StaffProfile.legacy_staff_id`。

## 五、迁移 / 回滚 / 核对方案

### 5.1 迁移方案（本轮不执行，仅定义）

| 阶段 | 内容 | 前置 | 产物 |
|---|---|---|---|
| M0 | **冻结**：新增业务一律不得直接引用 `hotelapp.models.Staff`（以 CI 门禁实现，见 5.3 C13） | 无 | 门禁用例 |
| M1 | **映射表建立**：把旧 `职务` 中文串映射到受控 `position_code`；无法识别的显式登记为 `UNASSIGNED` 并产出待人工核对清单 | 负责人裁定映射口径 | 映射表 + 核对清单 |
| M2 | **状态补迁**：对旧 `启用=False` 的行，在新栈按「雇佣状态 / 任职状态」两层补迁，保留旧值来源可回溯（写入 `LegacyRecordLink.mapping_status`） | M1 | 补迁脚本 + 回执 |
| M3 | **薪酬/工龄落位裁定**：按负责人裁定执行（迁移 / 保留旧表 / 退役），并同步 `敏感字段权限边界设计` 文档 | 负责人裁定 | 裁定落档 |
| M4 | **旧只读入口改读新栈**：`/index/`、`/search_staff/`、`approve_staff_export` CSV 源改为读 `StaffProfile`（与 ARCH-02 矩阵 #33/#37 的 REDIRECT 建议同批） | ARCH-02 批次 | 路由/视图改动 + 回归 |
| M5 | **员工中心解耦**：移除 `staff_center_views.py` 的 2 处旧表依赖与「关联旧员工」下拉（映射全量完成后） | M1/M2 完成 + C11 裁定 | 视图/模板改动 |
| M6 | **权限码解绑**：新员工中心 registry 不再指向 `hotelapp.staff` ContentType 的 4 个权限码（需保证旧权限迁移有回退路径，否则新员工中心 403） | 负责人批准 + 权限迁移方案 | registry + 迁移 |
| M7 | **归档**：为 Staff 补 `ARCHIVE_SPECS` 与归档表（当前无 Staff 归档 spec） | 归档策略裁定 | 归档 spec + 演练 |
| M8 | **反向 FK 与 legacy_staff 处置**：`Room/Food/Order` 三处 FK 与 `StaffProfile.legacy_staff(PROTECT)` 的最终形态 | M4-M7 完成 + 负责人批准 | 迁移 + 数据核对 |

### 5.2 回滚方案

**现状回滚能力 = 0**：`apps/identity/migrations/0001_initial.py:199` 的 reverse 为 `noop`，现有旧→新映射**不可回滚**。因此：

1. M1/M2/M3 阶段的数据变更一律**只写新栈**，不删除/修改旧表任何一列 → 旧表始终是可信基线，回滚 = 丢弃新栈写入 + 按 `LegacyRecordLink` 反向清理新栈记录。
2. M4 起的代码变更按标准提交回滚；旧路由不删除（ARCH-02 矩阵仅给建议，本轮不改代码）。
3. M8 涉及 FK 与删表，**必须**先具备：正向迁移 + 可用反向迁移（不可 `noop`）+ 核对脚本通过 + 负责人书面批准（`ARCHITECTURE.md:24-25`）。
4. 每阶段的回滚步骤、所需备份与核对命令，逐阶段写进执行单。

### 5.3 核对方案（可执行、可断言）

| 核对项 | 方式 | 通过标准 |
|---|---|---|
| N1 映射完整性 | `manage.py audit_employee_data --output x.csv`，读「问题」列 | 「旧员工未映射 StaffProfile」计数 = 0 |
| N2 映射状态 | 查 `LegacyRecordLink` 中 `source_model="Staff"` | `mapping_status` ∈ {LINKED, IGNORED}；无 UNREVIEWED / MATCH_CANDIDATE |
| N3 一对一唯一 | `StaffProfile.objects.filter(legacy_staff=s).count() > 1` 检测（`audit_employee_data.py:76` 已实现） | 每个旧行最多 1 个新档案 |
| N4 状态等价 | 旧 `启用` 与新 `employment_status/membership.status` 逐行比对 | 停用行 100% 有对应的新状态 |
| N5 岗位等价 | 旧 `职务` → 新 `position_code` 逐行映射表核对 | 100% 命中或显式登记 UNASSIGNED |
| N6 权限等价 | 旧 4 权限码 vs 新 `StaffRoleAssignment` 逐人比对 | 差异项逐条登记（不得静默） |
| N7 审计等价 | `AuditEvent` 中 `entity_type="StaffProfile"` 覆盖旧操作类型 | 旧操作类型均有新审计事件 |
| N8 旧表零写入 | `apps/identity/test_module_gates.py:65-70` 的 `_legacy_counts()`（含 Staff）前后一致 | 全量回归中计数零变化 |
| N9 旧表零生产写路径（新增门禁） | 静态检查：生产代码中 `Staff.objects.(create|update|save|get_or_create)` 命中 0 | CI 门禁（当前仅覆盖 35 条 410 路由，**不覆盖新栈写路径**，需补） |

> N1、N2 依赖生产数据运行（本地合成数据不足以覆盖）；N8、N9 可在 CI 自动化。**当前无任何自动化断言覆盖映射链路**（`staff_center_views.py:126/151`、`seed_hotel_data.py:209-230`、`audit_employee_data.py`、`0001_initial.py:92-111` 全无测试）。

## 六、最终退役条件清单

| # | 条件 | 仓库内可验证方式 | 现状 |
|---|---|---|---|
| C1 | 旧表零生产 ORM 引用 | 排除模型定义/迁移/FK 声明/测试后，`Staff` 符号命中 0 | ❌ 尚存 11 处 |
| C2 | 员工中心不依赖旧表 | `staff_center_views.py` 中 `LegacyStaff` 命中 0；模板无「关联旧员工」下拉 | ❌ 2 处 + 1 下拉 |
| C3 | 旧 seed 不写旧表 | `Staff.objects.(create\|update\|save\|get_or_create)` 在管理命令中命中 0 | ✅ **已满足** |
| C4 | 旧只读页面退役或改读新栈 | `/index/`、`/search_staff/`、`approve_staff_export` 不再查 `Staff.objects` | ❌ 仍活跃 |
| C5 | 审计不依赖旧表主键 | `entity_type="Staff"` 命中 0 | ✅ **已满足** |
| C6 | 三处反向 FK 解除 | `hotelapp/models.py:103,121,258` 移除/改指且有迁移 | ❌ |
| C7 | `StaffProfile.legacy_staff` 处置 | `PROTECT` OneToOne 改为可空或移除 | ❌ |
| C8 | 旧表数据有归档落点 | `ARCHIVE_SPECS` 含 Staff + 归档表存在 | ❌ |
| C9 | 映射核对通过 | N1/N2 通过（需生产数据） | ⚠ 需生产运行 |
| C10 | 权限码解绑旧 ContentType | `registry.py:201-204` 不再指向 `hotelapp.staff` 权限 | ❌ |
| C11 | 未映射旧员工处置已裁定 | 「关联旧员工」下拉清零 + `MC11-ST-07` 关闭 | ❌ 仍 `PRODUCTION_PENDING` |
| C12 | 薪酬/工龄归属已裁定并落位 | 裁定落档 + 敏感字段边界文档同步 | ❌ |
| C13 | 旧表零写入回归防线 | 新增「新栈写路径不得写 `staff` 表」守护用例 | ⚠ 仅覆盖 35 条 410 路由 |
| C14 | 旧表数据保留策略获批 | `ARCHITECTURE.md:24-25` 要求的人工最终审批 | ⏸ 待批准 |
| C15 | 迁移/回滚可执行 | 正向 + **可用**反向（现 reverse=`noop`） | ❌ 回滚能力 0 |
| C16 | 死代码清理 | `hotelapp/views.py:192-197` `_get_staff()`（全仓零调用方）摘除 | ⚠ 仍在 |

**C3、C5 当前已满足**，可直接作为「已达成」基线写入 PMS3.0 继承清单。

## 七、边界与合规声明

1. 本轮**未修改任何代码**，未删旧表、未改旧路由、未改权限码 —— 与裁定「渐进迁移，保留历史数据」一致。
2. **未伪造验收**：`MC11-ST-07`（旧员工批量迁移映射未实现）仍为 `PRODUCTION_PENDING`；N1/N2 需生产数据，未以合成数据冒充。
3. **未代负责人裁定**：C12（薪酬/工龄归属）、C14（保留策略审批）为负责人闸门，本方案只登记为待裁定。
4. 全部结论基于 `main 0ca1ce4` 的代码只读盘点，每条给出 `文件:行号` 可复核位置。
