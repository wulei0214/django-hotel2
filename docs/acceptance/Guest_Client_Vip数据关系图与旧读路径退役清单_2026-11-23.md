# Guest / Client / Vip 数据关系图与旧读路径退役清单（2026-11-23）

> 文档编号：AUDIT-T04-001 ｜ 类型：ACCEPTANCE ｜ 状态：ACTIVE ｜ 版本域：PMS2.0 ｜ 业务模块：Guest 数据治理
> 任务依据：任务书 V1.1 TASK-04 交付物中的「数据关系图」与「旧读路径和旧入口退役清单」两项。
> 性质：**只读审计产出**。按治理通知 §四.4（审计可继续）执行；**未修改任何代码**，TASK-04 的代码类开发仍处暂停。
> 复核基线：`origin/main` `3f729ed` 时点现取。
> 脚本与输出：`.work/task04/relation.py`、`.work/task04/relation.txt`

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| Guest 与 Client/Vip 的关系 | **两套独立主档，无数据库外键**；只能经 `LegacyRecordLink` 或 `Guest.legacy_vip_id` 关联 |
| 权威数据源 | **`apps.guests.Guest`**；Client/Vip 为历史兼容来源 |
| legacy 模型主键 | `Client.顾客id`、`Vip.会员编号`（**均非 `id`**） |
| 旧读路径（生产代码引用 legacy Client/Vip） | 仅 **2 处**，且都在归档模块的源模型映射表内 |
| 归档能力 | `ARCHIVE_SPECS` **6 项**（含 Client/Vip），代码与测试已具备 |
| 归档实施阻塞 | ①`privacy.retention_execute` 未注册（DEFECT-T02-3）；②M5-4 八项硬前置未全满足（含法律归档 owner 未定） |

---

## 1. 数据关系图

### 1.1 实体关系（文字版 ER）

```
┌─────────────────────┐          ┌──────────────────────────┐
│ hotelapp.Accommodation │        │ apps.guests.Guest        │
│  （历史住宿单）        │        │  （PMS2 权威主档）        │
│  PK: id               │        │  PK: id                  │
│  FK: 顾客id → Client   │        │  legacy_vip_id (CharField)│
│  FK: 客房编号 → Room   │        │  merged_into → Guest(PROTECT)│
│  FK: 客户编号 → Client │        │  merged_at               │
└──────────┬──────────┘          └────────────┬─────────────┘
           │                                   │
           │ 无 FK 相连（跨库/跨代）            │ 1:N
           ▼                                   ▼
┌─────────────────────┐          ┌──────────────────────────┐
│ hotelapp.Client      │          │ GuestPhone               │
│ （历史客人）          │          │ GuestDocument            │
│  PK: 顾客id           │          │ GuestDocumentIndex       │
│  身份证号（unique）    │          │ GuestMembership          │
│  外键: 无             │          │   └ MembershipLevelHistory│
└─────────────────────┘          │ RetentionPolicy          │
                                  │ LegalHold                │
┌─────────────────────┐          │ RetentionExecution       │
│ hotelapp.Vip         │          └──────────────────────────┘
│ （历史会员）          │
│  PK: 会员编号         │                    ▲
│  联系方式（unique）    │                    │ 映射桥
│  外键: 无             │          ┌─────────┴────────────────┐
└─────────────────────┘          │ apps.core.LegacyRecordLink│
                                  │  source_system="HOTELAPP_V1"│
                                  │  source_pk / target_model  │
                                  │  target_pk / match_method  │
                                  │  mapping_status:           │
                                  │   UNREVIEWED/MATCH_CANDIDATE│
                                  │   LINKED/CONFLICT/IGNORED  │
                                  └────────────────────────────┘
```

### 1.2 关键事实

| 项 | 事实 | 来源 |
|---|---|---|
| legacy 模型清单 | `Accommodation`、`Client`、`Food`、`Order`、`Park`、`FinanceRecord`、`NightAudit`、`Folio`、`Room`、`Staff`、`Vip`（共 11 个） | `hotelapp/models.py` |
| `Client` 主键 | **`顾客id`**（`AutoField(primary_key=True)`） | 同上 |
| `Vip` 主键 | **`会员编号`**（`AutoField(primary_key=True)`） | 同上 |
| `Client` / `Vip` 外键 | **均无**（不与 Guest 或其他新模型相连） | 同上 |
| Guest 侧历史关联字段 | `legacy_vip_id`（CharField，无写入路径） | `apps/guests/models.py` |
| Guest 合并字段 | `merged_into`（FK→Guest, PROTECT）、`merged_at` | 同上 |
| 映射桥 | `LegacyRecordLink`：`source_system` 默认 `HOTELAPP_V1`，含 `source_pk`/`target_model`/`target_pk`/`match_method`/`mapping_status` | `apps/core/models.py` |

> **注**：`Client` 与 `Vip` 之间也**没有**外键——两者在 legacy 侧是并列的独立表。`Vip` 与 `Client` 的关联在历史上靠业务约定（如"会员编号"字段互指），**代码层无约束**。

### 1.3 关系结论

1. **Guest 是 PMS2 唯一权威主档**；Client/Vip 是历史兼容来源，不是第二权威；
2. 两者之间**没有数据库级关联**，任何"Client 就是 Guest"的推断都必须经 `LegacyRecordLink` 显式登记（且需人工核验状态为 `LINKED`）；
3. `Guest.legacy_vip_id` 存在但**无写入路径**（MC11-MB-06 已登记），故当前无法经此字段反查 Vip；
4. **禁止仅凭姓名或手机号相同自动合并 Guest**（任务书 TASK-04 特别约束）——代码层无自动合并客户端，符合要求。

---

## 2. 旧读路径与旧入口退役清单

### 2.1 生产代码对 legacy Client/Vip 的引用（全量）

| # | 位置 | 性质 | 处置 |
|---|---|---|---|
| 1 | `apps/billing/legacy_archive.py:224` `"Client": legacy_models.Client` | 归档源模型映射表 | **保留**（归档必需，只读源） |
| 2 | `apps/billing/legacy_archive.py:225` `"Vip": legacy_models.Vip` | 同上 | **保留** |

**其余引用全部在测试文件**（`test_legacy_archive*`、`test_legacy_archive_query`、`test_legacy_restore_drill`、`test_legacy_equivalence` 等），属合法的历史等价性/归档测试需要。

→ **结论：apps/ 中不存在对 legacy Client/Vip 的"旧读业务路径"**（无业务 API/页面直接查这两张表）。唯一接触点是归档模块的只读源映射。

### 2.2 旧入口（HTTP 层）退役状态

| 入口 | 状态 | 依据 |
|---|---|---|
| `/walk-in`（旧 POST） | **已 410 退役** | `apps/api/front_desk.py` 恒返回 410 Gone（R2-04/DEC-WI-01） |
| `/front-desk/walk-ins`（新） | 活跃 | 标准入口 |
| `/checkin/legacy/`（旧页面） | **仍可达（只读）** | `hotel/urls.py`；`views.checkin_manage` 为只读页（`@staff_required` + `view_accommodation`） |
| 旧 CRUD 页面（`insert_client`/`delete_client`/`update_client` 等） | **已 410 退役** | 视图体直接 `return HttpResponse(..., status=410)` |

> 详尽的 66 条旧页面路由分类见 `docs/acceptance/架构旧路径收口审计报告_2026-11-23.md`（31 已退役 / 2 仍写库 / 27 只读 / 6 其他）。

### 2.3 退役清单（按处置分类）

| 类别 | 条目 | 处置建议 | 需批准 |
|---|---|---|---|
| **立即可保留** | 归档源映射（2 处） | 保留，随归档功能长期存在 | 否 |
| **已退役、待观察期结束** | `/walk-in` 旧 POST（410） | 观察期后可将契约测试转退役行为测试 | 是（退役节奏） |
| **只读在线、待流量统计** | `/checkin/legacy/`（旧页面） | 需生产流量统计后定退役窗口（与 MC11-RES-07 同批） | 是（`PRODUCTION_PENDING`） |
| **不可退役（历史关联）** | `hotelapp.Client` / `Vip` **模型本身** | 仍有历史业务关联（Accommodation 外键指向 Client），**不得删除** | 是（需数据验证） |
| **待迁移** | `Guest.legacy_vip_id` 无写入路径 | 需 Vip→会员迁移方案（MC11-MB-06） | 是 |

---

## 3. 归档实施的真实阻塞（承接 M5-4 八项硬前置）

M5-4 明文规定归档实施有 **8 项硬前置**。逐项核对当前状态：

| # | 前置条件 | 状态 | 证据 |
|---|---|---|---|
| 1 | client/vip/accommodation 存量盘点 | ⏸ 需生产数据 | dev 库值全 0，不能作为依据 |
| 2 | 归档必要性及映射流程测试 | ✅ 已具备 | `test_legacy_archive*` / `test_legacy_restore_drill` |
| 3 | 加密（Fernet+HMAC）、RetentionPolicy、LegalHold | ✅ 已实现 | `name_pii.py` / `retention.py` |
| 3b | **受控揭示未实现前不得开启** | ⚠ **未满足** | `reveal_guest_name` 已按治理拦截（NS-E-05 未批准） |
| 4 | 归档后校验（checksum/摘要/条数） | ✅ 已具备 | `legacy_checksum.py`、`verify_archive_run` |
| 5 | 备份恢复演练 | ⏸ `PRODUCTION_PENDING` | 需生产环境 |
| 6 | 查询等价性验证与批次凭证 | ✅ 已具备 | `test_legacy_equivalence.py` |
| 7 | D6 旧路径切换与观察 | ⏸ 未开始 | 依赖生产流量 |
| 8 | **数据处理删除（L4 专项）** | ⏸ **需负责人批文** | 开发方无此权限 |

**另有一项跨任务阻塞**：`privacy.retention_execute` 未注册到 `ApprovalActionRegistry`（DEFECT-T02-3），使清理审批链路无法正规使用——这直接影响前置第 3 项的可运营性。

---

## 4. 与既有报告的一致性

| 报告 | 关系 |
|---|---|
| `架构旧路径收口审计报告_2026-11-23.md` | 本报告 §2.2 引用其 66 条路由分类，数字一致 |
| `MC11开放事项逐项复核报告_2026-11-23.md` | MB-06（Vip→会员迁移）、RES-07（旧入口退役）均 `PRODUCTION_PENDING`，一致 |
| `TASK02数据清理服务缺陷复核报告_2026-11-23.md` | DEFECT-T02-3 阻塞清理审批，一致 |
| `DEFECT-T02-3澄清与核实_2026-11-23.md` | 同上 |

---

## 5. 本会话边界

- **未修改** `hotelapp/` 任何文件（旧模型只读）；
- **未执行**任何数据迁移、归档或删除；
- **未新增**迁移或权限码；
- 仅产出只读关系图与退役清单（TASK-04 交付物中不涉及代码的两项）。

> **TASK-04 代码类工作仍处暂停**（治理通知 §四.3）。本报告补齐的是「文档/审计」交付物，不是代码开发。

---

## 6. 交付物

| 内容 | 路径 |
|---|---|
| 本报告（关系图 + 退役清单） | `docs/acceptance/Guest_Client_Vip数据关系图与旧读路径退役清单_2026-11-23.md` |
| 关系盘点脚本 | `.work/task04/relation.py` |
| 原始输出 | `.work/task04/relation.txt` |
| 架构路由分类（上游） | `docs/acceptance/架构旧路径收口审计报告_2026-11-23.md` |
| M5-4 归档 spec（上游） | `docs/PMS2.0_当前版本/04_开发实施记录/架构治理模块五批次M5-4归档spec设计与退役移交_2026-11-19.md` |
