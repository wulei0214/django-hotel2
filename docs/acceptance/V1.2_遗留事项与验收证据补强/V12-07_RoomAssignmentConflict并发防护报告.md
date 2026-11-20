# V12-07 RoomAssignmentConflict 并发防护报告

| 项目 | 内容 |
|---|---|
| 任务编号 | V12-07（P1，库存/排房数据一致性） |
| 变更文件 | `apps/inventory/models.py`、`apps/inventory/reconciliation.py`、`apps/inventory/migrations/0011_room_conflict_open_key.py`、新增 `apps/inventory/test_room_conflict_concurrency.py` |
| 数据库 | SQLite（回归）+ **MySQL 8.0.33（并发权威）** |
| 结论 | **PASS（数据库级唯一性 + MySQL 并发验证）**；历史重复行处置见 §五 |

## 一、整改前的问题

`_scan_room_assignment_conflicts()` 是 Python 层"先查 OPEN 行、没有就 `create`"，
模型唯一约束是 `(run, room, stay_date)`——**按运行隔离**。两个扫描运行并发时：

1. 都查到"无 OPEN 行"；
2. 各自插入一条 OPEN 差异；
3. 同一真实冲突被报告两次，待办/MC-007 出现两份。

进程内锁无法覆盖多进程/多实例部署，必须由数据库仲裁。

## 二、设计（负责人批准边界内）

- **保持独立差异类型** `RoomAssignmentConflict`，不并入 `InventoryDiscrepancy`；
- **扫描只读**：不改 `ReservationRoom`、不改预订状态、不换房、不自动关闭；
- 复用既有 `InventoryReconciliationRun`、调度、幂等机制、MC-007 WorkItem 与通知。

实现要点：

| 机制 | 说明 |
|---|---|
| 可空唯一键 `open_key` | `OPEN` 时写 `f"{room_id}:{stay_date}"`；`DISMISSED` 置 `NULL`。MySQL 8 不支持条件唯一约束，沿用本仓库"可空唯一键 + `save()` 维护 + 迁移回填"口径 |
| 唯一约束 `uq_room_conflict_open_key` | 数据库层保证同一（物理房, 住宿日）最多一条有效差异；`NULL` 不参与唯一性，历史可保留多条 |
| `save()` 统一维护键位 | 扫描新建、原位更新、复核关闭、人工 dismiss 四条路径共用同一维护点；`update_fields` 自动补齐 `open_key` |
| 并发冲突回落 | `create` 包在 `transaction.atomic()` 内；撞唯一键 `IntegrityError` → 回查该（房, 日）OPEN 行并原位更新，首报运行归属不变 |
| 差异仍在不得自动关闭 | 复核 `review_room_conflict` 仍按判定式复核，冲突未消除即拒绝闭环 |

## 三、迁移与回填

`inventory/0011_room_conflict_open_key.py`：

1. `AddField(open_key)`；
2. `RunPython(backfill_open_key)`：按 `(room_id, stay_date, pk)` 顺序，**同组只给 pk 最小的一条**写键；
3. `AddConstraint(uq_room_conflict_open_key)`。

回填**不自动关闭/合并**已存在的重复 OPEN 行——"冲突持续存在不得自动关闭"是负责人裁定边界，
重复行须人工处置（见 §五）。反向回滚函数 `clear_open_key` 一并提供。

## 四、测试证据

| 用例 | 断言 | 环境 |
|---|---|---|
| `test_concurrent_scans_create_single_open_conflict` | 两线程同时扫描 → 该（房, 日）OPEN 差异 **恰好 1 条**，`conflict_count=2`，`open_key` 为复合键 | **MySQL only**（SQLite 无并发插入语义，显式 skip） |
| `test_sequential_rescan_does_not_duplicate` | 重复扫描不新增，且首报 `run_id` 不变（原位更新） | SQLite + MySQL |
| `test_dismissed_conflict_reopens_when_still_present` | 关闭后 `open_key` 为 `NULL`；冲突仍在 → 下次扫描重新建 OPEN（新行） | SQLite + MySQL |
| `test_scan_does_not_modify_assignment_rows` | 扫描前后 `ReservationRoom` 的 `assigned_room_id/check_in/check_out/reservation.status` 完全一致 | SQLite + MySQL |

| 字段 | 内容 |
|---|---|
| 命令 | `DJANGO_ENV=test DJANGO_DB_ENGINE=mysql python manage.py test apps.inventory.test_room_conflict_concurrency apps.inventory.test_room_conflict_detection --noinput` |
| 结果 | `Ran 3 tests ... OK`（含 MySQL 并发项）与既有 7 项检测用例全绿；MySQL 汇总批次 `Ran 20 tests ... OK` |
| 证据日志 | `.work/v12-mysql-targeted.log`（本机临时目录） |

## 五、遗留（如实登记）

1. **历史重复 OPEN 行**：若上线前已存在同（房, 日）多条 OPEN（并发缺陷产物），
   迁移只给最小 pk 写键、其余保持 `NULL`，**不会被自动关闭**。需人工逐条核对后
   `dismiss`（冲突已消除）或保留一条继续跟踪。当前 dev 库经检查无此情形。
2. **扫描中断恢复**：本轮验证了并发与重复触发；"扫描进程中途崩溃后重跑"未单独造用例，
   依赖"重复扫描幂等 + 唯一键"同一机制，属**未单独取证**。
3. **扫描与人工修复同时发生**：修复动作（换房/取消）在 `front_desk_services` 事务内完成，
   扫描为只读快照；两者并发时的差异滞后于修复是预期行为（下次扫描自动原位更新），
   本轮未做并发时序专项。

## 六、结论

并发防护从"Python 层先查后建"升级为"数据库唯一键仲裁 + 冲突回落原位更新"，
MySQL 并发用例证明同一真实冲突只产生一条有效差异，扫描仍严格只读。
**本任务判 PASS**，历史重复行与崩溃恢复两项按 §五 保留为待观察项。
