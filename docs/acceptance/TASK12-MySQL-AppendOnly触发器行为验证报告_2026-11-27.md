# TASK-12 MySQL Append-Only 触发器行为验证报告（2026-11-27）

> 文档编号：PMS2-TASK12-APPENDONLY-VERIFY-001 ｜ 类型：ACCEPTANCE ｜ 状态：ACTIVE ｜ 版本域：PMS2.0 ｜ 业务模块：数据治理 / 部署运维与数据治理
> 任务编号：TASK-12 ｜ 交付物：DEL-21（本轮并行子任务补齐项之一）
> 执行日期：2026-11-27 ｜ 代码基线：main HEAD `0fa7086ea3ecc514dab4a521e860102b533b9a82`（`git rev-parse HEAD`，分支 `feature/pms2-final-docs`）
> 执行方式：在**共享开发实例（3306）**实测权限边界与 1419；在**本次新建的本地独立 MySQL 8.0.33 实例（3307，独立 datadir、具备 SUPER/TRIGGER）**执行触发器安装 + 真实行为级验证（Django 全量迁移后的真实 schema）。全程**未运行 MySQL 全量测试**（遵守 `max_connections=20` 纪律），**未修改任何 `.py`、SQL 定义文件或历史文档**；隔离实例已停机并删除 datadir。
> 任务依据：《PMS 2.0 最终验收补证、遗留事项整改与 PMS 3.0 启动准备任务书》PMS2-FINAL-ACCEPTANCE-REMEDIATION-003 V1.0 §TASK-12（第 451–475 行）
> 锚点：`apps/core/models.py`（AuditEvent 与 append-only 守卫）、`apps/core/sql/audit_immutability.sql`（触发器 SQL 定义）、`apps/billing/models.py`（LedgerEntry 等）
> 关联文档（只读，未修改）：`docs/PMS2.0_当前版本/07_部署运维与数据治理/PMS2 MySQL财务与审计不可变事实部署说明_2026-11-10.md`、`docs/共享规范/PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md`

---

## 一、结论摘要

### 1.1 七项逐项状态

| # | 验证项（任务书原文） | 状态 | 关键实测结论 |
|---|---|---|---|
| 1 | 正常 INSERT | `PASS` | 三张只追加表 + 现金变动表在**已装触发器**状态下 INSERT 全部放行；`billing_cashiershiftmovement` 的 BEFORE INSERT 守卫对「他人 OPEN 班次」以 **1644** 拒绝 |
| 2 | UPDATE 被拒绝 | `PASS` | 三张表 UPDATE 全部被 MySQL 触发器拒绝，错误码 **1644**，`MESSAGE_TEXT` 与 SQL 定义逐条一致 |
| 3 | DELETE 被拒绝 | `PASS` | 三张表 DELETE 全部被触发器拒绝，错误码 **1644** |
| 4 | 应用层绕过 Service 直接写入 | `PASS` | 绕过 Service 直写**原生 SQL**（`connection.cursor()`）仍被 1644 拦截；ORM 层 `save()/QuerySet.update()/delete()` 另有一道独立防线（`ValueError`/`ValidationError`）——**两者分别取证，ORM 层结论不用于冒充触发器结论** |
| 5 | 普通数据库账号写入 | `PASS_WITH_RESERVATIONS` | 两个普通账号实测通过；但**发现重大残余风险**：schema 级 `ALL PRIVILEGES` 运行账号**可 `DROP TRIGGER` 摘除保护**（实测触发器 7→6 后裸 SQL UPDATE 真实写穿），现部署说明 §3 的 `REVOKE UPDATE, DELETE` **未覆盖 `TRIGGER` 权限** |
| 6 | 触发器缺失时的检测和处理 | `PASS_WITH_RESERVATIONS` | **检测**机制存在且有效（`check_mysql_append_only_guards` 能报出缺失的 7 条，实测 `CommandError`）；**处理**存在缺口：`manage.py check` 在 0 触发器时仍输出「System check identified no issues」，**启动不阻断**；`scripts/e2e_full_chain.py` 遇 1419 会把断言降级为 API 契约面 |
| 7 | 生产启动及迁移安全边界 | `PASS_WITH_RESERVATIONS` | 边界已实测刻画：migration **不含**触发器 DDL；新库 `migrate` 后触发器数 = **0**；重复 `migrate` 不破坏已装触发器（7→7）；`install_mysql_append_only_guards` 在全新迁移库上可成功安装 7 条。残余：目标实例尚未执行，且无启动闸门、无 TRIGGER 权限收窄 |

### 1.2 总体判定

**总体判定：`PASS_WITH_RESERVATIONS`（触发器行为语义已获行为级证据；生产落地为 `PRODUCTION_PENDING`）**

1. 任务书原文要求「本轮优先使用本地独立 MySQL 测试实例，具备所需权限后执行」。据此，本次**新建了一个本地独立 MySQL 8.0.33 实例**（独立 datadir、`log_bin=1`、`log_bin_trust_function_creators=1`、`root@localhost` 具备 SUPER/TRIGGER），在其上跑 **Django 全量迁移生成真实 schema**，再安装仓库自带的触发器安装/校验命令，**实测**了 UPDATE/DELETE 拒绝、普通账号写入、触发器缺失检测、迁移边界等行为。**不是**用 API 层测试或 ORM 层守卫冒充触发器测试。
2. 共享开发实例（3306）**不具备**该能力：`hotelhotel@127.0.0.1` 无 SUPER、`log_bin=1`、`log_bin_trust_function_creators=0` → 建触发器实测报 **1419**，自提权被拒（**1227 / 1229**）。该实例上 7 项中的触发器行为面一律为 `WAITING_ENVIRONMENT`（未验证）。
3. **未验证不等于失败**：本报告所有 `PASS` 均只对「触发器语义」成立，**不构成生产实例已受保护的证据**。生产实例的安装、留档与应用账号拒绝验证仍为 `PRODUCTION_PENDING`（见 §五）。

---

## 二、环境事实（全部来自实际命令输出）

### 2.1 环境 A —— 共享开发实例（本机 3306，本次尝试触发器安装的失败环境）

```text
$ mysql -h 127.0.0.1 -P 3306 -u hotelhotel -e "SELECT VERSION() AS version, @@port AS port,
        @@log_bin AS log_bin, @@log_bin_trust_function_creators AS trust_fn_creators,
        @@max_connections AS max_connections, @@hostname AS hostname;"
version  port  log_bin  trust_fn_creators  max_connections  hostname
8.0.33   3306  1       0                   20               DESKTOP-V42JQB9

$ mysql ... -e "SHOW GRANTS FOR CURRENT_USER();"
Grants for hotelhotel@127.0.0.1
GRANT USAGE ON *.* TO `hotelhotel`@`127.0.0.1`
GRANT ALL PRIVILEGES ON `hotel`.* TO `hotelhotel`@`127.0.0.1`
GRANT ALL PRIVILEGES ON `test_hotel`.* TO `hotelhotel`@`127.0.0.1`
```

- 账号 `hotelhotel@127.0.0.1` 只有 `USAGE ON *.*` + 两个库的库级 `ALL PRIVILEGES`；**全局层面无 `SUPER`、无 `TRIGGER`、无 `SYSTEM_VARIABLES_ADMIN`**。
- `log_bin=1`（二进制日志开启）、`log_bin_trust_function_creators=0`、`max_connections=20`。
- 自提权尝试（实测原文）：

```text
$ mysql ... -e "SET GLOBAL log_bin_trust_function_creators=1;"
ERROR 1227 (42000) at line 1: Access denied; you need (at least one of) the SUPER or
SYSTEM_VARIABLES_ADMIN privilege(s) for this operation

$ mysql ... -e "SET SESSION log_bin_trust_function_creators=1;"
ERROR 1229 (HY000) at line 1: Variable 'log_bin_trust_function_creators' is a GLOBAL variable
and should be set with SET GLOBAL
```

- 触发器存在性探测：**全实例 0 条触发器**（`information_schema.TRIGGERS` 计数 0；`hotel` / `test_hotel` 两个库均为 0）。
- 表存在性（`hotel` 与 `test_hotel` 均含）：`core_auditevent`、`billing_ledgerentry`、`billing_cashiershiftmovement`。
- 本任务对该实例**只读**（仅 `DROP TRIGGER IF EXISTS` 空操作 + 失败的 `CREATE TRIGGER`），事后复核：`core_auditevent` 3733 行、`billing_ledgerentry` 68 行、`billing_cashiershiftmovement` 5 行，触发器 0 条 —— **无数据写入、无对象残留**。

### 2.2 环境 B —— 本地独立 MySQL 实例（本次为 TASK-12 新建，3307）

| 项 | 实测值 |
|---|---|
| 版本 | `8.0.33`（与本机开发实例同版本；`basedir=D:\MYSQL\mysql-8.0.33-winx64`） |
| 端口 / 角色 | `3307`；独立 datadir（系统临时目录），与 3306 共享实例**完全隔离**，不占用其 `max_connections` |
| `@@log_bin` | `1`（与生产同构：开启二进制日志） |
| `@@log_bin_trust_function_creators` | `1`（**DBA 侧一次性开关**，用于解除 1419；生产应保持 0，见 §六 R3） |
| `@@max_connections` | `20`（默认值，与 3306 无关） |
| 连接身份 | `CURRENT_USER()=root@localhost`，`SHOW GRANTS` 含 `SUPER`、`TRIGGER`、`SYSTEM_VARIABLES_ADMIN` 且 `WITH GRANT OPTION` |
| 验证库 | `pms2_task12`（主验证库）、`pms2_task12_migonly`（迁移边界专用库），均为本次新建 |
| schema 来源 | `python manage.py migrate --noinput` **真实全量迁移**生成（非手工造表） |
| 收尾 | 验证结束后已停机（3307 端口关闭）并删除 datadir；`git status` 确认仓库工作区除本报告外无本任务新增文件 |

> 说明：环境 B 的 `log_bin_trust_function_creators=1` 是**模拟 DBA 已授予建触发器权限**的标准做法（1419 报错原文自身即建议该变量）。第 5 项另有一段 `trust_function_creators=0` 的生产同构复测（见 §三.5）。

### 2.3 代码侧既有事实（现取现核）

| 事实 | 出处 |
|---|---|
| 触发器 SQL 定义（7 条） | `apps/core/sql/audit_immutability.sql:17-82` |
| SQL 文件首行约束：「不要把此脚本放进 Django migration」 | `apps/core/sql/audit_immutability.sql:2` |
| 安装命令（`DROP IF EXISTS` + `CREATE`，幂等） | `apps/core/management/commands/install_mysql_append_only_guards.py:74-77` |
| 校验命令（查 `information_schema.TRIGGERS`，逐条核对名称/表/事件/时机 + `SIGNAL SQLSTATE '45000'`） | `apps/core/management/commands/check_mysql_append_only_guards.py:26-57` |
| ORM 层第二道防线（`save()` 拒改已存在实例、`delete()` 一律拒、QuerySet 拦 `update/delete/bulk_*`） | `apps/core/models.py:56-69`、`:144-150`；`apps/billing/models.py:265-277`、`:422-436` |
| 「建议对应用账号 REVOKE UPDATE/DELETE」（**仅为建议，且未含 TRIGGER**） | `apps/core/models.py:77-78` |
| CI 只做「安装 + 定义存在」校验（**不校验行为**） | `.github/workflows/ci.yml:61-65` |
| E2E 遇 1419 时降级为 API 契约面断言 | `scripts/e2e_full_chain.py:394-415`、`:1108-1154` |
| 生产部署 runbook（含 REVOKE 示例与验收清单） | `docs/PMS2.0_当前版本/07_部署运维与数据治理/PMS2 MySQL财务与审计不可变事实部署说明_2026-11-10.md:22-54` |
| 历史台账已记「缺审计触发器行为级验证，CI 仅校验定义存在」 | `docs/共享规范/PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md:263` |

---

## 三、七项逐项验证过程与原始报错原文

以下命令输出均在环境 B（隔离实例，真实迁移 schema + 已装触发器）取得；标注「A/B」者为两条环境对照。

### 3.1 第 1 项：正常 INSERT —— `PASS`

**前置数据**：`Property id=2 code=TASK12`、`User id=5 task12_cashier`、`CashierShift id=1 status=OPEN cashier=5`。

```text
[ALLOWED ] 1a ORM AuditEvent.record() 追加审计事件 -> 返回 1
  -> core_auditevent 新行 id=1
[ALLOWED ] 1b 裸 SQL INSERT core_auditevent -> 返回 2
  -> core_auditevent 裸 SQL 行 id=2
[ALLOWED ] 1c(重做) 裸 SQL INSERT billing_ledgerentry（干净版） -> 4
  -> 落库确认: (4, 'CHARGE', Decimal('50.00'), 'CNY', datetime.date(2026, 11, 27), 'TASK12-LEDGER-5')
[ALLOWED ] 1d ORM CashierShiftMovement 追加（本人 OPEN 班次） -> 返回 1
  -> billing_cashiershiftmovement 行 id=1
[BLOCKED ] 1e 裸 SQL INSERT billing_cashiershiftmovement（他人 OPEN 班次，触发器应拒绝）
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 cash movement requires its own open cashier shift')
             MySQL 错误码=1644
             __cause__=pymysql.err.OperationalError | args=(1644, 'PMS2 cash movement requires its own open cashier shift')
```

结论：
- 只追加表在守卫生效状态下**必须允许追加**——三张表 INSERT 全部放行，符合 append-only 语义。
- `trg_billing_shiftmovement_open_shift_insert` **行为有效**：向「他人 OPEN 班次」插入现金变动被 1644 拒绝，与 `audit_immutability.sql:65-82` 定义一致。
- 夹具说明（取证可追溯）：`billing_ledgerentry` 的 `folio_id` 为悬空外键（隔离库内用同一会话 `SET FOREIGN_KEY_CHECKS=0` 造基准行，专注触发器行为，不影响触发器判定）；首次 1c 曾报 `1292 Incorrect date value: '1988'`，经定位为**本次验证脚本自身缺陷**（Python `%` 格式化把 `%s` 替换成未加引号的 `2026-11-27`，MySQL 按算术表达式求值为 `2026-11-27 = 1988`），改用参数绑定后成功。**该现象是测试脚手架问题，非产品缺陷。**

### 3.2 第 2 项：UPDATE 被拒绝 —— `PASS`

```text
[BLOCKED ] 2a UPDATE core_auditevent SET reason='tampered'
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 audit events are append-only')
             MySQL 错误码=1644
[BLOCKED ] 2b UPDATE billing_ledgerentry SET description='tampered'
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 ledger entries are immutable; post a reversal')
             MySQL 错误码=1644
[BLOCKED ] 2c UPDATE billing_cashiershiftmovement SET amount=999
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 cashier shift movements are append-only')
             MySQL 错误码=1644
```

（3b 的重做版本 C2 同：`args=(1644, 'PMS2 ledger entries are immutable; post a reversal')`。）

结论：三张表 UPDATE 均由 **MySQL BEFORE UPDATE 触发器**拒绝，错误码 **1644**、`MESSAGE_TEXT` 与 `audit_immutability.sql:17-63` 逐条一致。**这是数据库层行为级验证，非 API 层断言。**

### 3.3 第 3 项：DELETE 被拒绝 —— `PASS`

```text
[BLOCKED ] 3a DELETE FROM core_auditevent WHERE id=...
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 audit events cannot be deleted')
             MySQL 错误码=1644
[BLOCKED ] 3b DELETE FROM billing_ledgerentry WHERE id=...
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 ledger entries cannot be deleted')
             MySQL 错误码=1644
[BLOCKED ] 3c DELETE FROM billing_cashiershiftmovement WHERE id=...
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 cashier shift movements cannot be deleted')
             MySQL 错误码=1644
```

（重做版本 C3：`args=(1644, 'PMS2 ledger entries cannot be deleted')`；随后查询该行仍在库：`-> 该行仍在库: 1`。）

### 3.4 第 4 项：应用层绕过 Service 直接写入 —— `PASS`

**第一段：ORM 层第二道防线（独立于触发器，如实描述，不用于冒充触发器结论）**

```text
[BLOCKED ] 4a ORM model.save() 改已存在 AuditEvent
          -> builtins.ValueError | args=('审计事件不可修改（只追加）',)
[BLOCKED ] 4b ORM AuditEvent.objects.filter(...).update()
          -> django.core.exceptions.ValidationError | args=('审计事件不可批量修改。', None, None)
[BLOCKED ] 4c ORM AuditEvent.objects.filter(...).delete()
          -> django.core.exceptions.ValidationError | args=('审计事件不可批量删除。', None, None)
[BLOCKED ] 4d ORM LedgerEntry.save() 改已存在凭证
          -> builtins.ValueError | args=('账本凭证不可变，禁止修改已存在的账务凭证；错账请创建红字冲销凭证。',)
[BLOCKED ] 4e ORM LedgerEntry.objects.filter(...).update()
          -> django.core.exceptions.ValidationError | args=('LedgerEntry 不允许直接修改；请创建红字冲销凭证。', None, None)
[BLOCKED ] 4f ORM LedgerEntry.objects.filter(...).delete()
          -> django.core.exceptions.ValidationError | args=('LedgerEntry 不允许批量物理删除；请创建红字冲销凭证。', None, None)
```

对应代码：`apps/core/models.py:59-69`（QuerySet 守卫）、`:144-150`（`save`/`delete`）；`apps/billing/models.py:266-277`、`:422-436`。

**第二段：绕过 Service 直写原生 SQL（绕过全部 ORM 守卫）——DB 触发器独立兜底**

```text
[BLOCKED ] 4g 绕过 Service 裸 SQL UPDATE core_auditevent
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 audit events are append-only')
[BLOCKED ] 4h 绕过 Service 裸 SQL DELETE core_auditevent
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 audit events cannot be deleted')
[BLOCKED ] 4i 绕过 Service 裸 SQL UPDATE billing_ledgerentry
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 ledger entries are immutable; post a reversal')
[BLOCKED ] 4j 绕过 Service 裸 SQL DELETE billing_ledgerentry
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 ledger entries cannot be deleted')
[BLOCKED ] 4k 绕过 Service 裸 SQL UPDATE billing_cashiershiftmovement
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 cashier shift movements are append-only')
[BLOCKED ] 4l 绕过 Service 裸 SQL DELETE billing_cashiershiftmovement
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 cashier shift movements cannot be deleted')
```

结论：即使**完全绕过 Service 与 ORM 守卫**直发原生 SQL，数据库层仍以 1644 拒绝 —— 证明触发器是**独立于应用层的第三道防线**，而非 API/ORM 层断言的替身。

### 3.5 第 5 项：普通数据库账号写入 —— `PASS_WITH_RESERVATIONS`

在环境 B 另建两个**非 DBA** 账号（口令为一次性凭据，实例已销毁，未入库）：

```text
SHOW GRANTS 'pms2_app'@'127.0.0.1':
   GRANT USAGE ON *.* TO `pms2_app`@`127.0.0.1`
   GRANT ALL PRIVILEGES ON `pms2_task12`.* TO `pms2_app`@`127.0.0.1`
SHOW GRANTS 'pms2_limited'@'127.0.0.1':
   GRANT USAGE ON *.* TO `pms2_limited`@`127.0.0.1`
   GRANT SELECT, INSERT ON `pms2_task12`.* TO `pms2_limited`@`127.0.0.1`
```

行为实测：

```text
[BLOCKED ] 5a pms2_app（库级 ALL PRIVILEGES，无 SUPER/TRIGGER）UPDATE core_auditevent
          -> pymysql.err.OperationalError | args=(1644, 'PMS2 audit events are append-only')
[BLOCKED ] 5b pms2_app DELETE core_auditevent
          -> pymysql.err.OperationalError | args=(1644, 'PMS2 audit events cannot be deleted')
[BLOCKED ] 5c pms2_app UPDATE billing_ledgerentry
          -> pymysql.err.OperationalError | args=(1644, 'PMS2 ledger entries are immutable; post a reversal')
[BLOCKED ] 5d pms2_app DELETE billing_ledgerentry
          -> pymysql.err.OperationalError | args=(1644, 'PMS2 ledger entries cannot be deleted')
[ALLOWED ] 5e pms2_app INSERT core_auditevent（追加必须放行） -> 返回 'committed'
[BLOCKED ] 5f pms2_limited（仅 SELECT,INSERT）UPDATE core_auditevent
          -> pymysql.err.OperationalError | args=(1142, "UPDATE command denied to user 'pms2_limited'@'localhost' for table 'core_auditevent'")
             MySQL 错误码=1142
[ALLOWED ] 5g pms2_limited INSERT core_auditevent（追加必须放行） -> 返回 'committed'
```

结论（正面）：
- **有 DML 权限的普通账号**：UPDATE/DELETE 被**触发器**以 1644 拒绝，INSERT 放行 —— 触发器对普通运行账号同样有效。
- **最小权限账号（仅 SELECT,INSERT）**：UPDATE 被**授权层**以 1142 拒绝，INSERT 放行 —— 与部署说明 §2 第 5 条「用应用账号分别尝试 UPDATE/DELETE，必须被权限或触发器拒绝」完全一致。

**残余风险（本次新发现，必须写入生产边界）**：

```text
# 生产同构配置（log_bin=1, log_bin_trust_function_creators=0）下，pms2_app 的权限探测
[ALLOWED ] pms2_app DROP TRIGGER trg_core_auditevent_no_update —— 未被拒绝
DROP 后触发器数量 = 6
[ALLOWED ] pms2_app UPDATE core_auditevent id=2 未被拒绝（affected=1）
  写入后实际行内容: (2, 'guard-dropped-bypass') （原 reason='raw insert'）
```

即：库级 `ALL PRIVILEGES` 隐含 **`TRIGGER` 权限**，`DROP TRIGGER` **不需要 SUPER、不受 binary log 限制** → 运行账号可以**静默摘除 append-only 守卫**，随后 UPDATE/DELETE 直接写穿。
另在 `log_bin_trust_function_creators=1` 的环境下，同一普通账号甚至可以 `CREATE TRIGGER` **伪造一个宽松触发器**顶替守卫（实测成功建出 `trg_task12_probe`，守卫列表被替换）。

推论与对照：现部署说明 §3（`:34-41`）只给出 `REVOKE UPDATE, DELETE`，**未撤销 `TRIGGER`**；`apps/core/models.py:77-78` 也只把「REVOKE UPDATE/DELETE」写成「建议」。本机共享实例的运行账号形态（`GRANT ALL PRIVILEGES ON hotel.*`）与实测 `pms2_app` 完全一致 —— **该风险在当前账号形态下是现实存在的**。补救建议见 §六 R2。

### 3.6 第 6 项：触发器缺失时的检测和处理 —— `PASS_WITH_RESERVATIONS`

**6.1 代码里是否有检测机制 —— 有，共 3 处（现取现核）**

| 机制 | 出处 | 作用 |
|---|---|---|
| `check_mysql_append_only_guards` | `apps/core/management/commands/check_mysql_append_only_guards.py:26-57` | 查 `information_schema.TRIGGERS`，逐条核对名称/表/事件/时机 + `SIGNAL SQLSTATE '45000'` 字符串；第 7 条另核对 `FOR UPDATE`/`NEW.SHIFT_ID`/`NEW.CREATED_BY_ID`/`'OPEN'`；缺失或不符即 `CommandError` |
| `install_mysql_append_only_guards` | `apps/core/management/commands/install_mysql_append_only_guards.py:74-77` | `DROP TRIGGER IF EXISTS` + `CREATE`，幂等重装 |
| `check_legacy_batch_guards` / `check_legacy_map_guards` | `apps/billing/management/commands/check_legacy_batch_guards.py:43`、`check_legacy_map_guards.py:29` | 同型检测（阶段 D 批次守卫） |

**6.2 检测有效性（实测）**

```text
--- 6a：DROP 一条触发器后运行仓库自带校验命令 ---
  已执行 DROP TRIGGER IF EXISTS trg_core_auditevent_no_update
[BLOCKED ] 6b `python manage.py check_mysql_append_only_guards`（缺失时应报错）
          -> django.core.management.base.CommandError
             args=('MySQL 只追加事实保护未通过；缺失触发器：trg_core_auditevent_no_update',)
```

全部 7 条被摘除时：

```text
  全部 DROP 后触发器数量=0
[BLOCKED ] E1 全缺失时 `check_mysql_append_only_guards`（应列出 7 条缺失）
          -> django.core.management.base.CommandError
             args=('MySQL 只追加事实保护未通过；缺失触发器：trg_billing_ledgerentry_no_delete,
             trg_billing_ledgerentry_no_update, trg_billing_shiftmovement_no_delete,
             trg_billing_shiftmovement_no_update, trg_billing_shiftmovement_open_shift_insert,
             trg_core_auditevent_no_delete, trg_core_auditevent_no_update',)
```

**6.3 触发器缺失后的防线降级（实测，决定「处理」是否成立）**

```text
[ALLOWED ] 6c 触发器缺失后绕过 Service 裸 SQL UPDATE core_auditevent（防线降级实证） -> 返回 1
  6c 之后该行实际内容: (1, 'trigger-missing-write')
[BLOCKED ] 6d 触发器缺失后绕过 Service 裸 SQL DELETE billing_ledgerentry
          -> django.db.utils.OperationalError | args=(1644, 'PMS2 ledger entries cannot be deleted')
     （该表触发器未摘除，故仍被拒；对照组）
```

全部 7 条摘除后（最强降级实证）：

```text
[ALLOWED ] E3 全缺失时绕过 Service 裸 SQL UPDATE core_auditevent（防线降级实证） -> 1
  E3 之后 core_auditevent id=1 内容: (1, 'no-trigger-at-all')
[ALLOWED ] E4 全缺失时绕过 Service 裸 SQL DELETE core_auditevent -> 1
  E4 之后 core_auditevent id=1 剩余行数: 0        ← 审计行被真实物理删除
[ALLOWED ] E5 重新安装全部触发器 `install_mysql_append_only_guards` -> None
[ALLOWED ] E6 重装后 `check_mysql_append_only_guards` -> None
  重装后触发器数量=7
```

**6.4 「处理」链路的三个缺口（实测）**

```text
[ALLOWED ] E2 全缺失时 `python manage.py check`（观察是否阻断启动） -> None
# 对应控制台原文：System check identified no issues (0 silenced).
```
1. **启动不阻断**：触发器 0 条时 `manage.py check` 仍报「no issues」；`apps/core/apps.py`（全文 8 行）**无 `ready()` 钩子**，源码扫描 `hotel/`、`hotelapp/`、`apps/core/apps.py` 对 `check_mysql_append_only_guards|install_mysql_append_only_guards` 的命中数为 **0** → 生产启动**不会**因守卫缺失而失败。
2. **E2E 降级冒充风险**：`scripts/e2e_full_chain.py:394-415` 在安装失败时把 F5 断言降级为「API 只追加契约面」（`:1147-1154`）；`:1108-1114` 中那条真正的 DB 层强断言分支（UPDATE/DELETE 必须被触发器拒绝）**在本机从未执行过**（1419 使其永远走不到）。按任务书「不得用 API 层测试冒充触发器测试」，该降级结果**不得计入触发器验收结论**。
3. **CI 只验定义不验行为**：`.github/workflows/ci.yml:61-65` 在测试套件之后 install + check，而 `check` 只做定义比对 → CI 绿灯**不等于**守卫行为有效。与台账 `PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md:263` 的历史结论一致。

**6.5 补充**：`apps/core/sql/audit_immutability.sql` 的 DBA 脚本路径在环境 B 上**端到端成功**（mysql 客户端执行整份文件，7 条触发器全部建成，退出码 0），历史文档 `55-four-domain-verification_2026-11-10.md:69,83` 记录的 G-01（`END$` 缺一个 `$`）在当前基线**不可复现**（`audit_immutability.sql:63`、`:82` 现均为 `END$$`）。

### 3.7 第 7 项：生产启动及迁移安全边界 —— `PASS_WITH_RESERVATIONS`

```text
--- 7a：触发器已装状态下重复迁移 ---
[ALLOWED ] 7a `python manage.py migrate --noinput`（触发器已装状态下重复迁移） -> None
[ALLOWED ] 7b 迁移后 `check_mysql_append_only_guards` -> None
  迁移后触发器数量=7
--- 7c：migration 目录中是否含触发器 DDL ---
  扫描 migrations/ 目录匹配 CREATE TRIGGER|audit_immutability|append_only_guards
  命中 0 条：（无 —— migration 不含触发器 DDL）
--- 7d：生产启动是否自动校验/安装触发器 ---
  扫描结果命中 0 条：（无 —— 启动路径不自动校验触发器）
```

**全新库上的迁移边界（独立进程，独立连接，实测）**：

```text
===== 1) 全新库只跑 migrate =====   （新建库 pms2_task12_migonly）
  Applying ... OK      （全量迁移完成）
===== 2) 迁移后该库触发器数量 =====
0
===== 3) 迁移后 check_mysql_append_only_guards =====
python : CommandError: MySQL 只追加事实保护未通过；缺失触发器：trg_billing_ledgerentry_no_delete,
trg_billing_ledgerentry_no_update, trg_billing_shiftmovement_no_delete,
trg_billing_shiftmovement_no_update, trg_billing_shiftmovement_open_shift_insert,
trg_core_auditevent_no_delete, trg_core_auditevent_no_update
GUARD_CHECK_EXIT=1
===== 4) 迁移后 manage.py check =====
System check identified no issues (0 silenced).
SYSTEM_CHECK_EXIT=0
===== 5) 全新库上 install_mysql_append_only_guards =====
已安装 6 条只追加触发器及 1 条现金变动 OPEN 班次插入保护触发器
MySQL financial guard 通过：6 条只追加触发器和 1 条现金变动班次保护触发器全部存在
7
```

边界结论：
1. **migration 不建触发器**（源码 0 命中 + 新库 migrate 后计数 0），与 `audit_immutability.sql:2` 的设计声明一致。
2. **迁移不会破坏已装触发器**（已装 7 条状态下重复 migrate → 仍为 7 条）。
3. **迁移后必须单独执行 install + check**，否则新环境处于「0 守卫」状态；`check` 会以退出码 1 报错（可作为流水线闸门）。
4. **启动无闸门**：`manage.py check` 在 0 守卫时仍绿；`apps/core/apps.py` 无 `ready()`。
5. **DBA 脚本路径与命令路径均可用**：SQL 文件与 `install_mysql_append_only_guards` 在具备权限的实例上都能建成 7 条。
6. **共享开发实例（3306）上仍是 0 守卫**，且当前账号**无法**安装（1419）→ 该实例上生产形态的守卫**尚未就位**。

---

## 四、证据索引（命令 + 原始输出出处）

| 编号 | 命令 / 动作 | 结果 |
|---|---|---|
| E-01 | `mysql --version` | `D:\MYSQL\mysql-8.0.33-winx64\bin\mysql.exe  Ver 8.0.33 for Win64 on x86_64 (MySQL Community Server - GPL)` |
| E-02 | 3306 `SELECT VERSION(),@@port,@@log_bin,@@log_bin_trust_function_creators,@@max_connections,@@hostname` | `8.0.33 / 3306 / 1 / 0 / 20 / DESKTOP-V42JQB9` |
| E-03 | 3306 `SHOW GRANTS FOR CURRENT_USER()` | `USAGE ON *.*` + `ALL ON hotel.*` + `ALL ON test_hotel.*`（无 SUPER） |
| E-04 | 3306 `SET GLOBAL log_bin_trust_function_creators=1` | `ERROR 1227 (42000) ... Access denied; you need (at least one of) the SUPER or SYSTEM_VARIABLES_ADMIN privilege(s)` |
| E-05 | 3306 `SET SESSION log_bin_trust_function_creators=1` | `ERROR 1229 (HY000) ... Variable 'log_bin_trust_function_creators' is a GLOBAL variable and should be set with SET GLOBAL` |
| E-06 | 3306 全实例 `information_schema.TRIGGERS` 计数 | `0`（`hotel`、`test_hotel` 均无触发器） |
| E-07 | 3306 执行 `apps/core/sql/audit_immutability.sql` | 7× `Note (Code 1360): Trigger does not exist` → `ERROR 1419 (HY000) at line 17: You do not have the SUPER privilege and binary logging is enabled (you *might* want to use the less safe log_bin_trust_function_creators variable)`，退出码 1 |
| E-08 | 隔离实例启动参数与变量实测 | `8.0.33 / 3307 / log_bin=1 / trust_function_creators=1 / root@localhost WITH GRANT OPTION` |
| E-09 | 隔离实例 `python manage.py migrate --noinput` | 退出码 0，全量迁移完成 |
| E-10 | 隔离实例 `python manage.py install_mysql_append_only_guards` | `已安装 6 条只追加触发器及 1 条现金变动 OPEN 班次插入保护触发器` |
| E-11 | 隔离实例 `python manage.py check_mysql_append_only_guards` | `MySQL financial guard 通过：6 条只追加触发器和 1 条现金变动班次保护触发器全部存在` |
| E-12 | 隔离实例 `information_schema.TRIGGERS` 全量列出 | 7 条，表/事件/时机与 `audit_immutability.sql` 定义逐条一致 |
| E-13 | 隔离实例执行 `apps/core/sql/audit_immutability.sql`（DBA 脚本路径） | 退出码 0，7 条触发器全部建成 |
| E-14 | INSERT / UPDATE / DELETE / 绕过 Service / 普通账号 / 缺失检测 / 迁移边界 全量断言 | 见 §三 各节「原始报错原文」逐条 |
| E-15 | 3306 事后复核 | `triggers=0`；`core_auditevent=3733`、`billing_ledgerentry=68`、`billing_cashiershiftmovement=5` —— 与本任务开始时一致，无写入、无残留 |
| E-16 | 隔离实例收尾 | 进程已终止，`port3307_open=False`，datadir 已删除；`git status --porcelain` 无本任务新增文件（除本报告） |

> 证据脚本为一次性临时脚本，置于系统临时目录，验证后随 datadir 一并删除，未进入仓库（遵守 `AGENTS.md` §交付前检查「临时验证脚本…在交付前删除或转为正式工具」）。

---

## 五、未完成与待裁定事项

### 5.1 未验证项清单（附解除条件）

| # | 未验证项 | 状态 | 解除条件 / 复核方法 |
|---|---|---|---|
| U-1 | **生产 / 目标实例**上的触发器安装、应用账号 UPDATE/DELETE 拒绝验证与留档 | `PRODUCTION_PENDING` | DBA/运维在目标库按部署说明 §2 执行：备份 → `install_mysql_append_only_guards`（或执行 `audit_immutability.sql`）→ `check_mysql_append_only_guards` → 用**应用运行账号**逐表试 UPDATE/DELETE 必须被拒、INSERT 必须成功 → 留存输出与截图 |
| U-2 | 生产库备份恢复演练（部署说明 §4 验收项「备份恢复演练通过」） | `PRODUCTION_PENDING` | 在预发/演练环境完成备份 → 恢复 → 守卫仍在且行为有效 |
| U-3 | 目标实例上**运行账号的实际授权形态**（是否与本机 `hotelhotel` 一样为库级 `ALL PRIVILEGES`） | 未核实（无生产访问权限） | DBA 执行 `SHOW GRANTS FOR '<运行账号>'@'<host>'`；若含库级 `ALL` → 按 §六 R2 收窄 |
| U-4 | 目标实例 `log_bin_trust_function_creators` 的实际取值 | 未核实 | DBA 执行 `SHOW VARIABLES LIKE 'log_bin_trust_function_creators'`；生产应为 `0` |
| U-5 | `billing_cashiershiftmovement` 的 INSERT 守卫在「**已关班（CLOSED）班次**」场景下的拒绝行为 | 未验证 | 本次仅验证「他人 OPEN 班次被拒（1644）」与「本人 OPEN 班次放行」；CLOSED 场景需在具备权限的实例上建 CLOSED 班次后 INSERT 复核（定义侧已含 `v_shift_status <> 'OPEN'` 判定，`audit_immutability.sql:76-81`） |
| U-6 | 部署说明 §3 `REVOKE UPDATE, DELETE` 在目标实例上的实际效果 | 未验证 | 需 DBA 在目标库执行 REVOKE 后，用运行账号重试 UPDATE/DELETE 并留存输出 |
| U-7 | 共享开发实例（3306）自身的守卫安装 | `WAITING_ENVIRONMENT` | 需 DBA 授予 `SUPER` 或 `log_bin_trust_function_creators=1` + `TRIGGER` 权限后重跑 E-07；**不要**为跑测试而擅自提权本机共享账号 |
| U-8 | 两条 legacy 守卫（`check_legacy_batch_guards` / `check_legacy_map_guards`）的行为级验证 | 不在 TASK-12 范围 | 任务书验证对象仅限 AuditEvent / LedgerEntry / 现金流不可变记录；legacy 守卫属阶段 D 批次对象，另行处理 |

### 5.2 待裁定事项

| # | 事项 | 状态 | 说明 |
|---|---|---|---|
| W-1 | 是否按 §六 R2 收窄运行账号权限（撤销 `TRIGGER` / 取消库级 `ALL PRIVILEGES`） | `WAITING_OWNER` | 属生产权限变更，影响运维迁移路径（需保留迁移账号 DDL 权限），本任务只做验证与建议，**未实施任何授权变更** |
| W-2 | 是否把 `check_mysql_append_only_guards` 接入生产启动闸门 / `CoreConfig.ready()` | `WAITING_OWNER` | 属代码变更（`.py`），本任务不改代码 |
| W-3 | `scripts/e2e_full_chain.py` 的 1419 降级策略是否改为 fail-closed | `WAITING_OWNER` | 现状降级会产出「API 契约面通过」的结论；按任务书口径该结论不可计入触发器验收 |
| W-4 | CI 是否新增一条「应用账号 UPDATE/DELETE 必须被拒」的真实行为断言 | `WAITING_OWNER` | 属 CI 配置变更 |
| W-5 | 历史结论失效说明 | 登记 | `docs/共享规范/PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md:263` 与 `docs/acceptance/ARCH-11审批历史与附件待裁定材料_2026-11-27.md:328` 中「数据库层行为级验证 = `WAITING_ENVIRONMENT`」的表述，**其「本机环境无法验证」部分仍然成立**（3306 依然无 SUPER、依然 1419）；但**「因此未获任何行为级证据」的含义已被本报告的隔离实例实测所取代** —— 触发器语义已有行为级证据，剩余缺口是生产实例落地与启动闸门（U-1/U-2/W-1/W-2）。旧文档不修改，差异在此登记 |

---

## 六、生产启动及迁移安全边界建议

> 均为**建议**，未在本次实施（未改代码、未改权限、未改 SQL 定义文件）。

**R1（安装与留档，顺序不可颠倒）**
目标库部署按 `docs/PMS2.0_当前版本/07_部署运维与数据治理/PMS2 MySQL财务与审计不可变事实部署说明_2026-11-10.md:22-28` 执行：备份 → DBA 执行 `apps/core/sql/audit_immutability.sql` 或 `python manage.py install_mysql_append_only_guards` → `python manage.py check_mysql_append_only_guards`（须 0 退出）→ **用应用运行账号**逐表试 UPDATE/DELETE（必须被拒）与 INSERT（必须成功）→ 留存全部输出。禁止以「CI 绿灯」或「SQL 文件已入库」认定目标环境已受保护。

**R2（新增，针对本次实测的高危缺口）：必须同时撤销运行账号的 `TRIGGER` 权限**
实测证明库级 `ALL PRIVILEGES` 蕴含 `TRIGGER` 权限，`DROP TRIGGER` 不需要 SUPER、不受 binlog 限制，运行账号可静默摘除守卫（守卫 7→6 后 UPDATE 真实写穿、`reason` 由 `'raw insert'` 变为 `'guard-dropped-bypass'`）。因此部署说明 §3 的 REVOKE 清单需补齐：

```sql
-- 部署说明 §3 既有三条之外，补充（由 DBA 单独执行）
REVOKE TRIGGER ON `PMS_DATABASE`.* FROM 'pms_runtime'@'%';
-- 并避免给运行账号库级 ALL PRIVILEGES；如需保留 DDL 能力，另设独立迁移账号
```

配套：把「运行账号能否 `DROP TRIGGER`」纳入部署验收项（可用 `SHOW GRANTS` 与一次可回滚的 DROP 试探判定）。

**R3（禁止把 `log_bin_trust_function_creators` 置 1）**
该开关一旦为 1，普通 schema 权限账号即可 `CREATE TRIGGER` 伪造宽松触发器顶替守卫（本次实测成功建出 `trg_task12_probe`）。生产应保持 `0`，由 DBA 一次性安装；确需临时开启时必须：限定时间窗 → 安装完成即恢复 `0` → 用 `check_mysql_append_only_guards` 复核 7 条定义 → 核对 `information_schema.TRIGGERS` 中无多余触发器。

**R4（启动闸门：缺守卫即拒绝启动）**
现状 `manage.py check` 在 0 守卫时仍输出「System check identified no issues」（实测），`apps/core/apps.py` 无 `ready()` → 生产启动不会因守卫缺失失败。建议二选一或并用：① 在部署流水线 / 容器 entrypoint 的应用启动前置步骤强制 `python manage.py check_mysql_append_only_guards`，非 0 退出即中止发布；② 在 `CoreConfig.ready()` 接入同一命令（需 W-2 裁定）。仅依赖运维手册「记得执行」不构成闸门。

**R5（迁移边界固化）**
保持「触发器不进 migration」（已核实 migrations 0 命中）。在迁移流水线中固化为**三段式**：`migrate` → `install_mysql_append_only_guards` → `check_mysql_append_only_guards`（必须 0 退出）；新环境迁移后触发器数 = 0 已实测，缺第 2、3 步即裸奔。迁移账号与运行账号分离（部署说明 §2 第 4 条 / `:43`）。

**R6（CI 从「定义存在」升级为「行为有效」）**
`.github/workflows/ci.yml:61-65` 目前只做定义比对。建议在 CI 的 MySQL 上追加一条真实断言：以**无 SUPER 的应用型账号**执行 UPDATE/DELETE 必须失败且错误码为 1644、INSERT 必须成功 —— 把 CI 绿灯从「触发器被创建」升级为「触发器真的会拒绝写入」。同时保留现有 `check` 作为定义漂移防线。

**R7（持续巡检与漂移告警）**
把 `check_mysql_append_only_guards` 做成定时任务并对非 0 退出告警。实测显示：触发器被摘除后，`manage.py check` 不会发现、API 层也不会发现，只有裸 SQL 写穿（第 6 项 E3/E4：审计行被真实物理删除）才会暴露 —— 巡检是必要的第二道发现机制。

**R8（E2E 降级必须显式留痕并禁止计分）**
`scripts/e2e_full_chain.py:394-415` 在 1419 时降级为 API 契约面断言。该降级应：① 在报告中显式标注「DB 层强断言未执行，原因 1419」；② **不得**计入触发器验收结论（任务书明令「不得用 API 层测试冒充触发器测试」）；③ 建议改为 fail-closed 或在 CI 的 root MySQL 上强制走 DB 层分支（现该分支在 CI 可执行，因 CI 以 root 安装触发器）。

---

## 七、下一步责任归属

| # | 事项 | 建议责任方 | 前置条件 | 状态 |
|---|---|---|---|---|
| N-1 | 目标/生产库安装 7 条触发器 + `check` 留档 + 应用账号 UPDATE/DELETE 拒绝验证（U-1） | DBA / 运维 | 备份完成；迁移/安装账号具备 `CREATE TRIGGER`（SUPER 或 `trust_function_creators` 窗口） | `PRODUCTION_PENDING` |
| N-2 | 运行账号权限收窄：补 `REVOKE TRIGGER`、取消库级 `ALL PRIVILEGES`（R2 / W-1） | DBA + 负责人裁定 | W-1 获批；确认迁移账号已单独保管 | `WAITING_OWNER` |
| N-3 | 备份恢复演练并复验守卫仍有效（U-2） | 运维 | N-1 完成 | `PRODUCTION_PENDING` |
| N-4 | 启动闸门接入（`check_mysql_append_only_guards` 进流水线/entrypoint，或 `CoreConfig.ready()`）（R4 / W-2） | 开发（代码）+ 运维（流水线） | W-2 获批 | `WAITING_OWNER` |
| N-5 | CI 增补真实行为断言（R6 / W-4） | 开发 | W-4 获批；CI MySQL 已有 root 安装能力 | `WAITING_OWNER` |
| N-6 | E2E 降级策略改为 fail-closed 或标注（R8 / W-3） | 开发 | W-3 获批 | `WAITING_OWNER` |
| N-7 | 定时巡检与漂移告警（R7） | 运维 | N-1 完成 | `DEFERRED_TO_PMS3`（可先由 N-1 的手工巡检替代） |
| N-8 | 补验 CLOSED 班次现金变动被拒（U-5） | 开发 / 验收 | 具备权限的 MySQL 实例可用（可复用本报告的隔离实例搭建方式） | `NOT_STARTED` |
| N-9 | 在 `docs/共享规范/PMS全项目历史需求追踪与未完成任务总台账_2026-11-22.md:263` 等处登记本轮结论差异（W-5） | 主控会话 | 本报告合入 | `NOT_STARTED`（台账由主控维护） |

---

> 本报告所有错误码、版本号、权限输出、行数与触发器计数均来自上文逐条列出的实际命令输出；未核实项一律标注「未核实」并给出复核方法。本任务未修改任何 `.py` 代码、SQL 定义文件与历史文档，未执行 `git` 写操作，未运行 MySQL 全量测试。
