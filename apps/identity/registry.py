"""PMS2 跨 App RBAC 代码注册表（实施方案 §7.2-7.4）——单一权威定义。

本模块是《PMS2平台身份权限与业务全链路治理开发实施方案》§7 的**唯一权威**注册表：

  * MODULE_REGISTRY_V2   业务模块 → 跨 App 权限面（含 PLANNED/RETIRED 方向）
  * ROLE_REGISTRY        九角色目录（与 Group 一一对应的元数据层）
  * SPECIAL_PERMISSIONS  高危/敏感专项权限点（跨 App 全量）
  * validate_registry()  注册表 ↔ 数据库一致性校验（CI/体检可调用）

权威性声明：
  * hotelapp/views.py 的 MODULE_REGISTRY 已改为从本注册表派生（module_gate
    行为保持不变：food/order/park 仍隐藏）。
  * Phase 3 权限中心重构（§8）完成后：旧矩阵页白名单
    （permission_admin.MODULES/ACTION_PERMS/ALL_CODENAMES，41 点，N-1 缺口）
    已从代码路径上移除，授权面唯一权威为本注册表——读写走
    apps/identity/permission_center.py，allowed = ACTIVE 模块码 ∪
    SPECIAL_PERMISSIONS，PLANNED/RETIRED 模块码一律整体拒绝。
  * 本注册表只做"定义 + 校验"，不改 AuthorizationService 运行时逻辑，
    不新增数据库 migration。

数据来源（审计基线 main@ad13c28）：
  * docs/audit/phase0/11-permissions.md  263 权限点逐点清单
  * docs/audit/phase0/12-roles.md        九角色授权快照与漂移
  * docs/audit/phase0/20-findings-register.md  F-05/F-06/F-08/F-09 缺口

设计口径：
  * actions 的 6 个固定键：view/add/change/delete/approve/special；
    值为 "app_label.codename" 权限码或哨兵 NOT_APPLICABLE（special 为权限码
    元组或哨兵）。special 中的权限码必须同时登记在 SPECIAL_PERMISSIONS 且
    module 指回本模块（validate_registry 双向核对）。
  * extra_codes：同模块下第二套旧模型权限码（如餐饮订单 order_*），与
    actions 同等参与解析校验与 PLANNED 授权检查。
  * app_label = 模块主数据的 PMS2 应用域（非权限码所在 app）——权限码以
    actions/extra_codes 的完整 "app_label.codename" 为准，这才是跨 App
    注册表的价值：模块 → 多个 Django app 的权限面映射。
"""
from django.contrib.auth.models import Group, Permission

# ---------------------------------------------------------------------------
# 哨兵与常量
# ---------------------------------------------------------------------------

NOT_APPLICABLE = "not_applicable"

# 模块状态
MODULE_ACTIVE = "ACTIVE"      # 在营：路由可达、权限码有真实执行点
MODULE_PLANNED = "PLANNED"    # 预留：对接第三方/人员到位后启用（当前隐藏）
MODULE_RETIRED = "RETIRED"    # 退役方向：入口已 410/冻结，剩余只读面待 Phase 5 收口

# 角色状态
ROLE_ACTIVE = "ACTIVE"
ROLE_PLANNED = "PLANNED"

# 风险等级（从低到高）
RISK_LEVELS = ("low", "medium", "high")

# 高危/敏感专项权限点结构键（防御拼写漂移）
_SPECIAL_KEYS = frozenset({"key", "codename", "module", "risk",
                           "requires_reason", "requires_confirm", "note"})

# 旧 module_gate 门禁键 → 注册表模块 code（hotelapp/views.py 派生开关用）
LEGACY_MODULE_GATE_KEYS = {
    "food": "fnb",     # 餐饮管理（菜品）
    "order": "fnb",    # 餐饮订单（依赖菜品，同属餐饮模块）
    "park": "parking", # 停车场管理
}

# ---------------------------------------------------------------------------
# MODULE_REGISTRY_V2 —— 业务模块注册表（11 个模块）
# ---------------------------------------------------------------------------

MODULE_REGISTRY_V2 = {
    # -- 在营业务模块（7 个） -------------------------------------------------
    "accommodation": {
        "code": "accommodation",
        "name_zh": "预订与入住",
        "app_label": "reservations",
        "status": MODULE_ACTIVE,
        "route_prefix": "/checkin",
        "landing_url": "/checkin/",
        "risk_level": "high",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.view_accommodation",
            "add": "hotelapp.add_accommodation",
            "change": "hotelapp.change_accommodation",
            # 删除列在矩阵页语义 = 冲账/红字（0014 FINANCE_REVERSE_PERMS），
            # 物理删除无执行点（11-permissions §3.2），保留授权作为冲账执行权
            "delete": "hotelapp.delete_accommodation",
            "approve": NOT_APPLICABLE,
            # P0-04：reverse_checkout 无真实执行点（F-08），从授权面移除；
            # no_show_handle 保留为专项权限（front-desk no-show 在用）
            "special": ("hotelapp.no_show_handle",),
        },
        "extra_codes": (),
        "notes": "PMS2 前台工作台 22 条 API 全部门禁在本模块权限码上；"
                 "数据模型 apps/reservations（Reservation/Stay）。",
    },
    "guests": {
        "code": "guests",
        "name_zh": "客人",
        "app_label": "guests",
        "status": MODULE_ACTIVE,
        "route_prefix": "/manage/guests",
        "landing_url": "/manage/guests/",
        "risk_level": "high",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.view_client",
            "add": "hotelapp.add_client",
            "change": "hotelapp.change_client",
            "delete": "hotelapp.delete_client",
            "approve": NOT_APPLICABLE,
            "special": ("hotelapp.guest_document_reveal",
                        "hotelapp.guest_phone_reveal"),
        },
        "extra_codes": (),
        "notes": "运行时门禁用 hotelapp.*_client 码（11-permissions §3.1）；"
                 "guests 应用自身 CRUD 码 20 个为仅定义未执行（§4 设计预期），"
                 "孤儿点 guests.document_reveal 见 F-07/P-C-1，不登记为本模块权限。",
    },
    "rooms": {
        "code": "rooms",
        "name_zh": "房态与库存",
        "app_label": "inventory",
        "status": MODULE_ACTIVE,
        "route_prefix": "/manage/rooms",
        "landing_url": "/manage/rooms/",
        "risk_level": "medium",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.view_room",
            "add": "hotelapp.add_room",
            "change": "hotelapp.change_room",
            "delete": "hotelapp.delete_room",
            "approve": NOT_APPLICABLE,
            # P0-04：room_release_ready 无真实执行点（F-08），从授权面移除
            "special": ("inventory.manage_rates",),
        },
        "extra_codes": (),
        "notes": "房态图/房型/房价日历 API 门禁 view_room；调价走 "
                 "inventory.manage_rates（F-05：OpenAPI 误标 change_room）；"
                 "净房放行 room_release_ready 唯一检查点已随 410 退役（F-08）。",
    },
    "memberships": {
        "code": "memberships",
        "name_zh": "会员",
        "app_label": "guests",
        "status": MODULE_ACTIVE,
        "route_prefix": "/manage/memberships",
        "landing_url": "/manage/memberships/",
        "risk_level": "medium",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.view_vip",
            "add": "hotelapp.add_vip",
            "change": "hotelapp.change_vip",
            "delete": "hotelapp.delete_vip",
            "approve": NOT_APPLICABLE,
            "special": NOT_APPLICABLE,
        },
        "extra_codes": (),
        "notes": "会员等级变更 API 门禁 view/add/change_vip；PMS2 数据在 "
                 "guests.GuestMembership，历史数据在 hotelapp.Vip。",
    },
    "staff": {
        "code": "staff",
        "name_zh": "员工",
        "app_label": "identity",
        "status": MODULE_ACTIVE,
        "route_prefix": "/manage/staff",
        "landing_url": "/manage/staff/",
        "risk_level": "high",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.view_staff",
            "add": "hotelapp.add_staff",
            "change": "hotelapp.change_staff",
            "delete": "hotelapp.delete_staff",
            "approve": NOT_APPLICABLE,
            "special": ("identity.staff_manage", "hotelapp.export_pii",
                        "hotelapp.export_approve"),
        },
        "extra_codes": (),
        "notes": "员工与账号工作台（8 视图 + staff API）门禁 identity.staff_manage；"
                 "敏感薪酬导出审批走 hotelapp.export_approve（F-06：原 _has_role 硬编码已迁入权限码）"
                 "_has_role，待 Phase 2/3 迁移到权限码）。",
    },
    "finance": {
        "code": "finance",
        "name_zh": "财务",
        "app_label": "billing",
        "status": MODULE_ACTIVE,
        "route_prefix": "/finance",
        "landing_url": "/finance/",
        "risk_level": "high",
        "property_scoped": True,
        "actions": {
            # 财务页入口/流水读均查 finance_access（页读同码）
            "view": "hotelapp.finance_access",
            "add": NOT_APPLICABLE,   # 账务经服务层写入，无独立 CRUD 码
            "change": NOT_APPLICABLE,
            "delete": NOT_APPLICABLE,  # 冲账语义归 accommodation/order 的 delete 列
            "approve": "billing.refund_approve",
            "special": ("hotelapp.finance_access",
                        "billing.refund_request", "billing.refund_approve"),
        },
        "extra_codes": (),
        "notes": "跨 App 模块：入口码 hotelapp.finance_access，退款双权 "
                 "billing.refund_request/refund_approve 分离（SoD），"
                 "夜审复核 core.breakglass_review 归 night_audit 模块。",
    },
    "night_audit": {
        "code": "night_audit",
        "name_zh": "夜审",
        "app_label": "core",
        "status": MODULE_ACTIVE,
        "route_prefix": "/nightaudit",
        "landing_url": "/nightaudit/",
        "risk_level": "high",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.night_audit_execute",  # /nightaudit/ 页 GET 同码门禁
            "add": NOT_APPLICABLE,
            "change": NOT_APPLICABLE,
            "delete": NOT_APPLICABLE,
            "approve": "hotelapp.business_date_reopen",  # 重开营业日申请/批准
            "special": ("hotelapp.night_audit_execute",
                        "hotelapp.business_date_reopen",
                        "core.breakglass_review"),
        },
        "extra_codes": (),
        "notes": "夜审 preview/run 与营业日重开/break-glass 共 7 条 API；"
                 "break-glass 复核独立权限 core.breakglass_review。",
    },
    # -- 预留模块（对接第三方，module_gate 隐藏，2 个） ------------------------
    "fnb": {
        "code": "fnb",
        "name_zh": "餐饮",
        "app_label": "hotelapp",
        "status": MODULE_PLANNED,
        "route_prefix": "/show_food",
        "landing_url": "",
        "risk_level": "low",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.view_food",
            "add": "hotelapp.add_food",
            "change": "hotelapp.change_food",
            "delete": "hotelapp.delete_food",
            "approve": NOT_APPLICABLE,
            "special": NOT_APPLICABLE,
        },
        "extra_codes": ("hotelapp.view_order", "hotelapp.add_order",
                        "hotelapp.change_order", "hotelapp.delete_order"),
        "notes": "module_gate 键 food/order；菜品+订单两套旧模型权限码同属本模块；"
                 "入口隐藏但历史权限授予仍在（见 validate_registry 的 PLANNED "
                 "授权基线告警，Phase 3 矩阵迁移时回收）。",
    },
    "parking": {
        "code": "parking",
        "name_zh": "停车",
        "app_label": "hotelapp",
        "status": MODULE_PLANNED,
        "route_prefix": "/show_park",
        "landing_url": "",
        "risk_level": "low",
        "property_scoped": True,
        "actions": {
            "view": "hotelapp.view_park",
            "add": "hotelapp.add_park",
            "change": "hotelapp.change_park",
            "delete": "hotelapp.delete_park",
            "approve": NOT_APPLICABLE,
            "special": NOT_APPLICABLE,
        },
        "extra_codes": (),
        "notes": "module_gate 键 park；对接停车第三方后启用。",
    },
    # -- 退役方向模块（1 个） --------------------------------------------------
    "legacy": {
        "code": "legacy",
        "name_zh": "历史只读",
        "app_label": "hotelapp",
        "status": MODULE_RETIRED,
        "route_prefix": "/show_",
        "landing_url": "",
        "risk_level": "low",
        "property_scoped": True,
        "actions": {
            "view": NOT_APPLICABLE,   # 旧只读页复用各在营模块的 view_* 码
            "add": NOT_APPLICABLE,
            "change": NOT_APPLICABLE,
            "delete": NOT_APPLICABLE,
            "approve": NOT_APPLICABLE,
            "special": NOT_APPLICABLE,
        },
        "extra_codes": (),
        "notes": "35 条旧写入口已 410（10-routes §E），15 条旧只读路由仍以各模块 "
                 "view_* 码放行待 Phase 5 退役；旧 /export/staff/ 通道已关。"
                 "不登记独立权限码——授权面归属各在营模块，避免双记。",
    },
    # -- 平台模块（1 个） ------------------------------------------------------
    "platform": {
        "code": "platform",
        "name_zh": "权限管理（平台）",
        "app_label": "identity",
        "status": MODULE_ACTIVE,
        "route_prefix": "/platform",
        "landing_url": "/platform/permissions/",
        "risk_level": "high",
        "property_scoped": False,   # 平台域不吃物业任职链
        "actions": {
            "view": NOT_APPLICABLE,  # 矩阵页 superuser-only（三项不变量），无权限码
            "add": NOT_APPLICABLE,
            "change": NOT_APPLICABLE,
            "delete": NOT_APPLICABLE,
            "approve": NOT_APPLICABLE,
            "special": NOT_APPLICABLE,
        },
        "extra_codes": (),
        "notes": "/platform/permissions/ 四区矩阵页 + Django Admin（is_staff+模型权限）；"
                 "Phase 3 起矩阵读写走 permission_center JSON API，授权面由本注册表派生"
                 "（旧 41 点白名单已移除，N-1 修复）。",
    },
}

# ---------------------------------------------------------------------------
# ROLE_REGISTRY —— 九角色目录（与 apps/identity/roles.py BUSINESS_ROLE_NAMES 对齐）
# ---------------------------------------------------------------------------

ROLE_REGISTRY = (
    {
        "key": "front_desk", "name": "前台", "group_name": "前台",
        "status": ROLE_ACTIVE, "editable": True, "builtin": True,
        "risk_level": "medium",
        "description": "预订/入住/退房/客人档案/退款申请；持 20 项授权",
    },
    {
        "key": "duty_manager", "name": "值班经理", "group_name": "值班经理",
        "status": ROLE_ACTIVE, "editable": True, "builtin": True,
        "risk_level": "high",
        "description": "当日管理者：夜审执行/净房放行/未到处理/退款审批/"
                       "break-glass；持 31 项授权",
    },
    {
        "key": "finance", "name": "财务", "group_name": "财务",
        "status": ROLE_ACTIVE, "editable": True, "builtin": True,
        "risk_level": "high",
        "description": "财务页/敏感导出申请/退款申请/冲账执行权（delete 列）；"
                       "持 13 项授权",
    },
    {
        "key": "owner", "name": "店长", "group_name": "店长",
        "status": ROLE_ACTIVE, "editable": True, "builtin": True,
        "risk_level": "high",
        "description": "全模块管理者 + 员工账号管理 identity.staff_manage + "
                       "调价 inventory.manage_rates；持 39 项授权",
    },
    {
        "key": "housekeeping", "name": "客房服务员", "group_name": "客房服务员",
        "status": ROLE_ACTIVE, "editable": True, "builtin": True,
        "risk_level": "low",
        "description": "清洁链流转（待清洁→清洁中→待检查）；净房放行不在内；"
                       "持 2 项授权",
    },
    {
        "key": "night_auditor", "name": "夜审员", "group_name": "夜审员",
        "status": ROLE_PLANNED, "editable": True, "builtin": True,
        "risk_level": "high",
        "description": "预留空组（0014 决策 5）：夜审由值班经理/店长兼岗，"
                       "人员到位后在矩阵页授权",
    },
    {
        "key": "fnb_waiter", "name": "餐饮服务员", "group_name": "餐饮服务员",
        "status": ROLE_PLANNED, "editable": True, "builtin": True,
        "risk_level": "low",
        "description": "预留空组：餐饮模块（fnb）启用时随矩阵页授权",
    },
    {
        "key": "fnb_supervisor", "name": "餐饮主管", "group_name": "餐饮主管",
        "status": ROLE_PLANNED, "editable": True, "builtin": True,
        "risk_level": "medium",
        "description": "预留空组：餐饮模块启用时的管理者角色",
    },
    {
        "key": "parking_guard", "name": "停车安保", "group_name": "停车安保",
        "status": ROLE_PLANNED, "editable": True, "builtin": True,
        "risk_level": "low",
        "description": "预留空组：停车模块（parking）启用时授权",
    },
)

# ---------------------------------------------------------------------------
# SPECIAL_PERMISSIONS —— 高危/敏感专项权限点（14 个，跨 App 全量）
# ---------------------------------------------------------------------------
# 口径：矩阵页 ACTION_PERMS 9 点 + 矩阵外 5 点（F-09：billing.refund_*、
# core.breakglass_review、identity.staff_manage、inventory.manage_rates）。
# key "reverse_entry"（红字冲销/反结账）解析到现实存在的唯一权限码
# hotelapp.reverse_checkout——F-08：其唯一检查点已随 410 退役，PMS2 红字冲销
# （LedgerEntryService.reverse_entry，审计 action_code=billing.reverse_entry）
# 暂无独立权限码，待 Phase 4 补 API 时以本点授权。

SPECIAL_PERMISSIONS = (
    {
        "key": "guest_document_reveal",
        "codename": "hotelapp.guest_document_reveal",
        "module": "guests", "risk": "high",
        "requires_reason": True, "requires_confirm": True,
        "note": "证件完整值揭示：三层复核（装饰器/服务层/API），每次落 AuditEvent",
    },
    {
        "key": "guest_phone_reveal",
        "codename": "hotelapp.guest_phone_reveal",
        "module": "guests", "risk": "high",
        "requires_reason": True, "requires_confirm": True,
        "note": "电话完整值揭示：同上双权分离，原因必填并留审计",
    },
    {
        "key": "refund_request",
        "codename": "billing.refund_request",
        "module": "finance", "risk": "medium",
        "requires_reason": True, "requires_confirm": False,
        "note": "退款申请（前台/财务）；与审批权 SoD 分离",
    },
    {
        "key": "refund_approve",
        "codename": "billing.refund_approve",
        "module": "finance", "risk": "high",
        "requires_reason": True, "requires_confirm": False,
        "note": "退款审批（值班经理/店长）；approval 通用门 _check_approver",
    },
    # P0-04 清理（缺口登记册 F-08）：hotelapp.reverse_checkout 与
    # hotelapp.room_release_ready 唯一检查点均已随旧路由 410 退役，
    # 属"权限点存在但无执行点"，从 SPECIAL_PERMISSIONS 授权面移除。
    # 权限点本体保留（历史组授权由下一次矩阵保存显式回收）；
    # Phase 4 front-desk 补 API 时以新权限码重新登记，不复活旧码。
    {
        "key": "no_show_handle",
        "codename": "hotelapp.no_show_handle",
        "module": "accommodation", "risk": "medium",
        "requires_reason": False, "requires_confirm": True,
        "note": "未到订单处理并释放房间（值班经理/店长）",
    },
    {
        "key": "finance_access",
        "codename": "hotelapp.finance_access",
        "module": "finance", "risk": "medium",
        "requires_reason": False, "requires_confirm": False,
        "note": "财务页入口/流水读（财务/值班经理/店长）",
    },
    {
        "key": "night_audit_execute",
        "codename": "hotelapp.night_audit_execute",
        "module": "night_audit", "risk": "high",
        "requires_reason": False, "requires_confirm": True,
        "note": "营业日结：未到处理、房费过租、日结记录",
    },
    {
        "key": "business_date_reopen",
        "codename": "hotelapp.business_date_reopen",
        "module": "night_audit", "risk": "high",
        "requires_reason": True, "requires_confirm": True,
        "note": "重开营业日申请/批准 + break-glass 申请/执行（附二次密码验证）",
    },
    {
        "key": "breakglass_review",
        "codename": "core.breakglass_review",
        "module": "night_audit", "risk": "high",
        "requires_reason": True, "requires_confirm": False,
        "note": "break-glass 事后复核（值班经理/店长）；矩阵外点（F-09）",
    },
    {
        "key": "export_approve",
        "codename": "hotelapp.export_approve",
        "module": "staff", "risk": "high",
        "requires_reason": True, "requires_confirm": True,
        "note": "敏感导出审批（F-06：原 _has_role 硬编码迁入权限码，值班经理/店长语义不变）",
    },
    {
        "key": "export_pii",
        "codename": "hotelapp.export_pii",
        "module": "staff", "risk": "high",
        "requires_reason": True, "requires_confirm": False,
        "note": "敏感数据导出申请（身份证/薪酬/联系方式）；F-06：审批/下载侧仍 "
                "_has_role 硬编码，待迁移到权限码",
    },
    {
        "key": "manage_rates",
        "codename": "inventory.manage_rates",
        "module": "rooms", "risk": "medium",
        "requires_reason": False, "requires_confirm": False,
        "note": "每日房价调整（店长）；矩阵外点（F-09）；OpenAPI 文档误标 "
                "change_room（F-05）",
    },
    {
        "key": "staff_manage",
        "codename": "identity.staff_manage",
        "module": "staff", "risk": "high",
        "requires_reason": False, "requires_confirm": False,
        "note": "员工与账号工作台全门禁（店长）；矩阵外点（F-09）",
    },
)


# ---------------------------------------------------------------------------
# 派生视图与查询辅助
# ---------------------------------------------------------------------------

def module_gate_open(gate_key):
    """module_gate 开关语义：门禁键对应模块是否开放。

    与旧 hotelapp.views.MODULE_REGISTRY.get(key, True) 行为完全一致：
    未登记的键默认开放；登记键按注册表模块状态判定（仅 ACTIVE 开放）。
    """
    module_code = LEGACY_MODULE_GATE_KEYS.get(gate_key)
    if module_code is None:
        return True
    module = MODULE_REGISTRY_V2.get(module_code)
    return module is not None and module["status"] == MODULE_ACTIVE


def legacy_module_gate_keys():
    """返回旧 module_gate 门禁键元组（hotelapp.views 派生 MODULE_REGISTRY 用）。"""
    return tuple(LEGACY_MODULE_GATE_KEYS)


def get_module(code):
    return MODULE_REGISTRY_V2.get(code)


def get_special(key):
    """按 key 或完整权限码查专项权限点。"""
    for item in SPECIAL_PERMISSIONS:
        if item["key"] == key or item["codename"] == key:
            return item
    return None


def special_codenames_by_module(module_code):
    """某模块登记的专项权限码元组（供矩阵/UI 汇总展示）。"""
    return tuple(item["codename"] for item in SPECIAL_PERMISSIONS
                 if item["module"] == module_code)


def iter_module_permission_codes(module):
    """展开模块登记的全部权限码（actions 6 键 + extra_codes，去除哨兵）。"""
    codes = []
    for value in module["actions"].values():
        if value == NOT_APPLICABLE:
            continue
        if isinstance(value, tuple):
            codes.extend(value)
        else:
            codes.append(value)
    codes.extend(module.get("extra_codes", ()))
    return codes


def permission_exists(code):
    """权限码（app_label.codename）能否解析到 Django Permission 表。"""
    try:
        app_label, codename = code.split(".", 1)
    except (AttributeError, ValueError):
        return False
    return Permission.objects.filter(
        content_type__app_label=app_label, codename=codename).exists()


def groups_holding(code):
    """持有某权限码的组名列表。"""
    app_label, _, codename = code.partition(".")
    return list(Group.objects.filter(
        permissions__content_type__app_label=app_label,
        permissions__codename=codename).values_list("name", flat=True))


# ---------------------------------------------------------------------------
# 一致性校验
# ---------------------------------------------------------------------------

def validate_registry():
    """注册表 ↔ 权限数据库一致性校验，返回发现清单（不抛异常）。

    发现项结构：{"level": "ERROR"|"WARNING"|"INFO", "code": <机读码>,
                 "message": <人读说明>}。ERROR 清单为空 = 注册表可发布。

    校验项：
      E1 重复 module code / role key / role group_name / special key / codename
      E2 ACTIVE 模块的权限码（actions+extra_codes）必须解析到真实 Permission
      E3 SPECIAL_PERMISSIONS 每项必须解析到真实 Permission（全量，无状态豁免）
      E4 special 元组 ⊆ SPECIAL_PERMISSIONS 且 module 指回本模块；
         SPECIAL_PERMISSIONS.module 必须是已登记模块
      E5 ROLE_REGISTRY 与数据库 Group 一一对应（缺组 = ERROR）
      W1 PLANNED 模块的权限码被任何组持有（授权基线告警，Phase 3 回收）
      W2 PLANNED 角色组持有任何权限
      W3 数据库存在注册表之外的同名业务组
      W4 PLANNED/RETIRED 模块引用了尚不存在的权限码（预留允许，提示即可）
      I1 未被注册表覆盖的权限点清单（Django 内建/PMS2 CRUD/孤儿点）
    """
    findings = []

    def add(level, code, message):
        findings.append({"level": level, "code": code, "message": message})

    # ---- E1 重复 code ----
    seen = {}
    for module in MODULE_REGISTRY_V2.values():
        code = module["code"]
        if code in seen:
            add("ERROR", "duplicate_module_code",
                f"模块 code 重复：{code}")
        seen[code] = True
    role_keys, role_groups = set(), set()
    for role in ROLE_REGISTRY:
        if role["key"] in role_keys:
            add("ERROR", "duplicate_role_key", f"角色 key 重复：{role['key']}")
        role_keys.add(role["key"])
        if role["group_name"] in role_groups:
            add("ERROR", "duplicate_role_group_name",
                f"角色 group_name 重复：{role['group_name']}")
        role_groups.add(role["group_name"])
    special_keys, special_codenames = set(), set()
    for item in SPECIAL_PERMISSIONS:
        if item["key"] in special_keys:
            add("ERROR", "duplicate_special_key", f"专项权限 key 重复：{item['key']}")
        special_keys.add(item["key"])
        if item["codename"] in special_codenames:
            add("ERROR", "duplicate_special_codename",
                f"专项权限 codename 重复：{item['codename']}")
        special_codenames.add(item["codename"])
        unknown_keys = set(item) - _SPECIAL_KEYS
        if unknown_keys:
            add("ERROR", "special_unknown_field",
                f"专项权限 {item['key']} 含未声明字段：{sorted(unknown_keys)}")

    # ---- E2/W4 模块权限码解析 ----
    # 同一模块内 actions 与 special 元组引用同一权限码是预期（如 approve 列与
    # 专项登记同码）；跨模块重复登记才构成治理歧义
    mapped_codes = {}   # code -> module code
    for module in MODULE_REGISTRY_V2.values():
        status = module.get("status")
        for code in iter_module_permission_codes(module):
            owner = mapped_codes.get(code)
            if owner is not None and owner != module["code"]:
                add("ERROR", "duplicate_permission_reference",
                    f"权限码被多个模块重复登记：{code}（{owner} 与 {module['code']}）")
            mapped_codes.setdefault(code, module["code"])
            if permission_exists(code):
                continue
            if status == MODULE_ACTIVE:
                add("ERROR", "unknown_permission_code",
                    f"ACTIVE 模块 {module['code']} 引用了不存在的权限码：{code}")
            else:
                add("WARNING", "planned_code_not_yet_defined",
                    f"{status} 模块 {module['code']} 引用的权限码尚不存在：{code}")

    # ---- E3/E4 专项权限 ----
    for item in SPECIAL_PERMISSIONS:
        code = item["codename"]
        if not permission_exists(code):
            add("ERROR", "unknown_special_permission",
                f"专项权限 {item['key']}（{code}）未解析到真实 Permission")
        if item["module"] not in MODULE_REGISTRY_V2:
            add("ERROR", "special_unknown_module",
                f"专项权限 {item['key']} 指向未登记模块：{item['module']}")
        if item["risk"] not in RISK_LEVELS:
            add("ERROR", "special_bad_risk",
                f"专项权限 {item['key']} 风险等级非法：{item['risk']}")
        if item["module"] in MODULE_REGISTRY_V2:
            actions = MODULE_REGISTRY_V2[item["module"]]["actions"]
            special_tuple = actions.get("special")
            if not (isinstance(special_tuple, tuple) and item["codename"] in special_tuple):
                add("ERROR", "special_not_in_module_tuple",
                    f"专项权限 {item['key']}（{code}）未列入模块 "
                    f"{item['module']} 的 actions.special 元组")
    for module in MODULE_REGISTRY_V2.values():
        special_tuple = module["actions"].get("special")
        if special_tuple == NOT_APPLICABLE:
            continue
        if not isinstance(special_tuple, tuple):
            add("ERROR", "special_tuple_type",
                f"模块 {module['code']} 的 special 必须是权限码元组或哨兵")
            continue
        for code in special_tuple:
            item = get_special(code)
            if item is None:
                add("ERROR", "special_tuple_unregistered",
                    f"模块 {module['code']} 的 special 引用了未登记专项权限：{code}")
            elif item["module"] != module["code"]:
                add("ERROR", "special_tuple_module_mismatch",
                    f"模块 {module['code']} 的 special 引用了归属 "
                    f"{item['module']} 的专项权限：{code}")

    # ---- E5/W2/W3 角色与 Group 一一对应 ----
    db_groups = {g.name: g for g in Group.objects.filter(
        name__in=[r["group_name"] for r in ROLE_REGISTRY])}
    for role in ROLE_REGISTRY:
        group = db_groups.get(role["group_name"])
        if group is None:
            add("ERROR", "missing_role_group",
                f"角色 {role['name']}（{role['group_name']}）在数据库无对应 Group")
            continue
        if role["status"] == ROLE_PLANNED and group.permissions.exists():
            add("WARNING", "planned_role_has_permissions",
                f"预留角色 {role['name']} 意外持有 {group.permissions.count()} 项权限")
    extra = (Group.objects.exclude(name__in=list(db_groups))
             .values_list("name", flat=True))
    for name in extra:
        add("WARNING", "unregistered_group",
            f"数据库存在注册表之外的组：{name}（测试组或历史遗留）")

    # ---- W1 PLANNED 模块授权基线 ----
    for module in MODULE_REGISTRY_V2.values():
        if module["status"] != MODULE_PLANNED:
            continue
        for code in iter_module_permission_codes(module):
            holders = groups_holding(code)
            if holders:
                add("WARNING", "planned_module_granted",
                    f"PLANNED 模块 {module['code']} 的权限码 {code} 仍被组持有："
                    f"{'、'.join(holders)}（历史授予基线，Phase 3 矩阵迁移时回收）")

    # ---- I1 未映射权限点（完整性盘点，INFO 不阻塞） ----
    mapped_codenames = {(code.split(".", 1)[0], code.split(".", 1)[1])
                        for code in mapped_codes if "." in code}
    unmapped = []
    for perm in Permission.objects.select_related("content_type"):
        pair = (perm.content_type.app_label, perm.codename)
        if pair not in mapped_codenames:
            unmapped.append(f"{pair[0]}.{pair[1]}")
    if unmapped:
        add("INFO", "unmapped_permissions",
            f"{len(unmapped)} 个权限点未纳入注册表（Django 内建/PMS2 CRUD 仅定义"
            f"未执行/孤儿点，政策见 25-rbac-registry.md）：{'、'.join(sorted(unmapped))}")

    return findings


def registry_errors(findings):
    """从 validate_registry() 结果中取 ERROR 级发现。"""
    return [f for f in findings if f["level"] == "ERROR"]


def registry_warnings(findings):
    """从 validate_registry() 结果中取 WARNING 级发现。"""
    return [f for f in findings if f["level"] == "WARNING"]
