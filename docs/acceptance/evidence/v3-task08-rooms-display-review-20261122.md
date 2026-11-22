# TASK-08 房态与库存产品展示专项复核记录（V3.0 任务书第一批）

- **复核日期**: 2026-11-22 ｜ **基线**: main @ `1b882eb`（工作区现取现核）
- **任务依据**: 总任务书 V3.0 第七章 TASK-08 / §5.3 产品诊断 A-D
- **复核方式**: 只读代码核对（pages.py rooms 注册表 + room_status_list/库存 DTO + 冲突端点接线），不重开发
- **结论**: **10 项产品展示维度全部已满足**，无新增页面整改任务，补本证据即可关闭

## 一、10 维度核对（页面 `/manage/rooms/`）

| # | 维度 | 页面/接口落点 | 证据（file:line，现取现核） |
|---|---|---|---|
| 1 | 房间占用 | 「占用状态」列 occupancy_label | apps/api/pages.py:329（rooms.columns） |
| 2 | 清洁状态 | 「清洁状态」列 housekeeping_label | 同上 :331 |
| 3 | 维修状态 | 「维修状态」列 maintenance_label | 同上 :332 |
| 4 | 当前可分配 | 「当前可售」列 is_ready=is_sellable_now（VACANT+CLEAN+NORMAL 复合） | pages.py:333；apps/inventory/models.py:134 |
| 5 | 房型库存日历 | 二级区「房型每日库存」：总房量/冻结/已售/停用/可售/停售 | pages.py:322-334（secondary_sections.inventory） |
| 6 | Hold（冻结） | 库存行 held 列 | pages.py:330 |
| 7 | 已确认占用 | 库存行 sold 列 | pages.py:331 |
| 8 | 维修停房 | 库存行 out_of_service 列 + 房间行维修状态 | pages.py:332-333 |
| 9 | 人工停售 | 库存行 stop_sell 列 + 房价区「是否关房」closed 列 | pages.py:334/326 |
| 10 | 排房冲突 | `GET /inventory/assignment-conflicts` 只读端点 + 任务中心闭环入口（MC11-INV-02 已关闭 f104488） | apps/api/urls.py:307；pages.py:531-532 |

DTO 语义核验：room_status_list 逐房返回 occupancy/housekeeping/maintenance 三维独立状态 + is_ready 复合可售判定（apps/api/guest_center.py:577-625），未把多维权压进单一 Room.status 字段——符合宪法 §4.4 多维分离要求。

## 二、诊断 B/C/D 对应能力现状（不重复开发）

- **诊断 B（维修影响可售库存）**: resolve 不恢复库存、verify PASS 唯一恢复路径（f5aeea1 浏览器 E2E 9/9 含库存回位断言）；恢复守卫无活跃工单（4177439）。
- **诊断 C（排房竞争）**: assign_room 写入时重叠校验（front_desk_services.py:178-196）；本轮补 walk-in 自动排房跳过被活跃预订持有房间（V3.0-TASK-02 修复，见交付记录），两前台并发排最后一间房由行锁+open_key 唯一键仲裁（V12-07）。
- **诊断 D（库存差异修复）**: MC-004 五步差异工作流已接通（f104488），申请人自批拦截+执行前版本重算在 reconciliation.py；扫描串行化 108bd6c。

## 三、裁定

按任务书 TASK-08 验收路径「若当前页面已经满足，补充产品验收证据即可」：本记录即为补充证据，MC11 盘点表 §3 不新增缺口；「库存扫描异步化」维持 INV-06 P2 登记不变。
