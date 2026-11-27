# NS-E-05 privacy.retention_execute 审批动作候选方案（2026-11-24）
> 文档编号：DOC-005 ｜ 类型：DECISION ｜ 状态：ACTIVE ｜ 版本域：PMS2 ｜ 业务模块：项目基线与决策

> 任务编号：PMS2-NS-E05-REVIEW-002 增补 ｜ 响应缺口报告：`docs/acceptance/T02-3与NS-E-05审批材料关联缺口_2026-11-24.md`（DEFECT-T02-004）
> 性质：**第三候选方案（仅材料）**，补充 FIN-06 / MB-11 之外被遗漏的逐项审批事项；不代表已获批。
> 审批状态：`WAITING_OWNER` ｜ NS-E-05 原始裁定内容未做任何修改。
> 基线：`f097c3e`（现取现核）。

---

## 一、缺口接受声明

并行会话 `f097c3e` 指出的缺口**成立**，本会话确认并纳入：

1. `DEFECT-T02-3`（`privacy.retention_execute` 未登记进 `ApprovalActionRegistry`）的修复本质是一次**逐项权限审批**——`ApprovalActionSpec` 必填 `request_permission` 与 `approve_permission` 两个权限码，登记动作码必须先定这两码；
2. 本会话此前产出的 6 份材料（`28051c9`）仅覆盖 FIN-06 / MB-11，未包含该事项，属候选清单遗漏；
3. 不纳入的后果与缺口报告所述一致：数据清理仅剩 dry-run 可用、审批门无法合法满足、存在裸 QuerySet 绕开风险，且该缺口不随 FIN-06/MB-11 获批而消失。

---

## 二、代码事实核查（现取现核，全部只读）

### 2.1 审批动作登记与权限强制点

| 事实 | 位置 | 内容 |
|---|---|---|
| `ApprovalActionSpec` 必填字段 | `apps/billing/approval_registry.py` | `action_code / entity_type / title / request_permission / approve_permission / executor / payload_fields`（`__slots__` 白名单） |
| 申请时强制校验 | `apps/api/approval.py` L170 | `has_permission(request.user, property, spec.request_permission)` |
| 审批时强制校验 | `apps/api/approval.py` L232/L260 | `has_permission(request.user, property, _approve_permission_for(record))` |
| 未知 action_code | `apps/api/approval.py` | → 400，不写 `ApprovalRecord` |
| 当前已登记动作 | `approval_registry.py` | 仅 4 项：`billing.refund`、`billing.reversal`、`night_audit.reopen`、`privacy.export_staff` |
| `privacy.retention_execute` | `ApprovalActionRegistry.get()` | **返回 None**（未登记）→ 审批记录无法合法创建 |

### 2.2 现有 4 个已登记动作的权限码配对（复用候选的事实依据）

| action_code | request_permission | approve_permission |
|---|---|---|
| `billing.refund` | `billing.refund_request` | `billing.refund_approve` |
| `billing.reversal` | `billing.reverse_entry` | `billing.reversal_approve` |
| `night_audit.reopen` | `hotelapp.business_date_reopen` | `hotelapp.business_date_reopen` |
| `privacy.export_staff` | `hotelapp.export_pii` | `hotelapp.export_approve` |

### 2.3 数据清理执行链现状（retention.py，已含 T02-1/T02-2 修复）

| 环节 | 状态 |
|---|---|
| dry-run | ✅ 可用（不落写，审计 `privacy.retention_dry_run`） |
| 正式执行审批门（DEFECT-T02-2 修复） | ✅ 已实现：必须携带 `ApprovalRecord`（`action_code=privacy.retention_execute`、同物业、APPROVED），且**执行人 ≠ 审批申请人** |
| `RetentionExecution` 先落库建立 pk（DEFECT-T02-1 修复） | ✅ |
| 执行审计 | ✅ `privacy.retention_executed` + 逐客 `privacy.guest_anonymized` |
| LegalHold 冻结 | ✅ 持有者跳过（非失败） |
| 未结业务保护 | ✅ 未结束预订/未结账/退款中跳过 |
| 账务/审计不可清理 | ✅（模块头声明，AuditEvent 永不在清理范围） |
| 审批动作登记 | ❌ **缺**（T02-3）——申请与审批两步因无 spec 而不可达 |

**结论**：除"登记"一步外，整条清理链（含 SoD、审计、Hold、业务保护）已实现并有测试覆盖；登记本身只差**两个权限码的选型**。

---

## 三、权限码选型候选（三选一，均 `WAITING_OWNER`）

### 方案 A：复用现有 PII 敏感操作码（零新增）

| 字段 | 复用值 |
|---|---|
| request_permission | `hotelapp.export_pii`（现有：敏感数据导出申请） |
| approve_permission | `hotelapp.export_approve`（现有：敏感数据导出审批） |

| 优点 | 代价/风险 |
|---|---|
| 零新增权限码，治理成本最低；与 `privacy.export_staff` 登记先例同族（PII 敏感操作"申请/审批"配对） | **语义外扩**：两码当前唯一执行点是员工薪酬/证件敏感导出；复用后覆盖"隐私数据保留期满清理"，授权矩阵含义变宽，现有持码岗位自动获得清理申请/审批能力（需岗位影响面评估） |

### 方案 B：新增两个专项码（语义精确）

| 字段 | 新增值 |
|---|---|
| request_permission | `guests.retention_request`（app_label=guests，清理对象是 `Guest` 模型，归属一致） |
| approve_permission | `guests.retention_approve` |

| 优点 | 代价/风险 |
|---|---|
| 语义精确、职责单一；不改变既有码含义；授权矩阵可独立细调 | **两个新增 L3 权限码**——恰是 NS-E-05 要求逐项审批的对象；需六要素评审 + 矩阵页登记 + 授权面双人审批落库 |

### 对缺口报告 §3.1 建议的一处更正

缺口报告建议"request 复用 `hotelapp.finance_access`"——**语义不符，不建议**：该码是财务中心**读**门禁（账单/流水/退款列表可见性），与"隐私数据清理申请"无业务关联；且其授权面（财务/值班经理/店长）与清理场景无对应关系。同理 `billing.refund_approve`（退款审批，SoD 语义绑定退款链）亦不建议复用。**`export_pii`/`export_approve` 才是语义同族的复用候选。**

### 方案 C：不纳入（书面收口）

若负责人裁定暂不启用数据清理正式执行：

1. `privacy.retention_execute` 登记移交 PMS 3.0（归入 P3-G2-04 数据保留/Legal Hold 条目依赖）；
2. 数据清理功能书面标记「**仅 dry-run**」：`plan_retention_cleanup(dry_run=False)` 在当前基线不可用（审批门无法满足），写入功能限制说明与用户文档；
3. T02-3 从 OPEN 改为 `DEFERRED_TO_PMS3`，不再悬空。

---

## 四、登记后的审批/SoD/审计链（方案 A 或 B 落地后的统一行为）

| 环节 | 行为 | 强制点 |
|---|---|---|
| 申请 | 持 request_permission 岗位经审批 API 发起 | `approval.py` L170 |
| 审批 | 持 approve_permission 岗位批准；**申请人 ≠ 审批人**（Q-05 SoD，`ApprovalRecord` 保存时保证） | L232/L260 + sod_rule |
| 执行 | 持有已批准审批引用的操作员执行；**执行人 ≠ 审批申请人** | `retention.py` 执行审批门 |
| 审计 | 申请/审批走 `ApprovalRecord`；执行落 `privacy.retention_executed` + 逐客 `privacy.guest_anonymized`；dry-run 落 `privacy.retention_dry_run` | 既有实现 |
| 拒绝场景 | 无审批引用 / 审批非本物业 / 未通过 / 申请人自执行 / LegalHold / 未结业务 | 既有实现 + 测试已验证（5 拒绝 + 1 正向） |

**岗位建议（待裁定，非本任务批准）**：申请=店长；审批=店长/值班经理（且与申请人不同人）；执行=与申请人不同的管理岗。财务/前台/客房/夜审/餐饮/停车安保：禁止。

---

## 五、测试设计（本阶段只设计 + 核对现有，不新增代码）

| # | 场景 | 现状 |
|---|---|---|
| 1 | 未登记动作码申请 → 400 | ✅ 既有（approval.py 白名单） |
| 2 | 登记后：无 request_permission 申请 → 403 | 获批落地后补 |
| 3 | 登记后：无 approve_permission 审批 → 403 | 获批落地后补 |
| 4 | 申请人自审批 → 拒绝 | ✅ 既有（Q-05 SoD） |
| 5 | 执行人 = 审批申请人 → 拒绝 | ✅ 既有（retention.py 执行门） |
| 6 | 审批跨物业 → 拒绝 | ✅ 既有 |
| 7 | dry-run 不需要审批、不落写 | ✅ 既有 |
| 8 | LegalHold / 未结业务跳过 | ✅ 既有 |
| 9 | 双引擎覆盖 | 获批落地后按双引擎纪律执行 |
| 10 | 裸 QuerySet 绕开审批 | 缺口报告已警示；获批落地时补"仅经服务层"守卫断言 |

---

## 六、负责人待裁定清单

| # | 裁定点 |
|---|---|
| 1 | 三选一：方案 A（复用 export_pii/export_approve）/ 方案 B（新增 guests.retention_request + guests.retention_approve）/ 方案 C（移交 PMS3 或标记仅 dry-run） |
| 2 | 若 A：是否接受 export_pii/export_approve 语义外扩及其对现有持码岗位的影响面 |
| 3 | 若 B：两个新码的九岗位授权面（建议申请=店长，审批=值班经理/店长） |
| 4 | 数据保留年限与 Legal Hold 法律 owner（P3-G2-04 联动依赖，OWNER-05 现行规则保留） |
| 5 | 裁定后由负责人决定是否另立"正式权限实现任务书"（本材料不触发实现） |

---

## 七、边界声明

1. 本文件仅补齐候选材料；**不修改** `approval_registry.py`、`apps/identity/registry.py`、`retention.py`，不新增权限码或迁移；
2. NS-E-05 原始负责人裁定内容未做任何修改；本候选加入后 **NS-E-05 总体仍为 `APPROVED_WITH_PENDING`**；
3. 不代负责人批准；方案 A/B/C 的选择权与两个权限码的最终授权面均归负责人。
