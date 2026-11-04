"""权限管理中心 API（方案 §8.1-8.3）：/platform/api/permissions/。

Phase 3 重构后的角色权限矩阵唯一读写通道，替代旧 permission_admin.py 的
form 白名单保存（N-1 缺口：旧 ALL_CODENAMES 含 PLANNED 模块 food/order/park
12 点，服务端可伪造授权——本模块从代码路径上移除该白名单）。

设计口径（§8.3 九步保存流程）：
  * allowed 授权面 = 注册表 ACTIVE 模块全部权限码 ∪ SPECIAL_PERMISSIONS
    （PLANNED/RETIRED 模块码一律拒绝，伪造授权整体 409 + security 审计，
    绝不静默过滤）。
  * version = 组当前权限码全集（排序后）的 sha256 前 16 位内容 hash——
    乐观锁并发保护，无需 migration（§8.5 优先无表方案）。
  * 高危变更（diff 触及任一 SPECIAL_PERMISSIONS 码）必须携带 confirm_token：
    TimestampSigner 签名，salt 绑定 group_id+diff_hash+user.pk，有效期 10
    分钟，不落库不存明文；缺失/过期/diff 不匹配 → 403。
  * 保存与审计同事务：Group.permissions 变更与 AuditEvent(
    permissions.matrix_saved) 在同一 transaction.atomic 内提交（§8.3.9）。
  * 非注册表管辖权限（Django Admin 手工授予的 auth.* 等注册表未登记码）
    原样保留；历史持有的 PLANNED 模块码随保存显式回收（出现在 removed
    diff 与审计 after 中，非静默）。
  * 不改 AuthorizationService 运行时逻辑：矩阵保存只写 Group.permissions，
    PMS 业务权限仍以员工任职和岗位授权为准（§8.1）。
"""
import hashlib
import json

from django.contrib.auth.models import Group, Permission
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.models import AuditEvent

from .models import StaffRoleAssignment
from .platform import platform_superadmin_required
from .registry import (
    MODULE_ACTIVE,
    MODULE_REGISTRY_V2,
    NOT_APPLICABLE,
    ROLE_REGISTRY,
    SPECIAL_PERMISSIONS,
    iter_module_permission_codes,
)

# 高危确认 token 有效期（秒，§8.3.f：10 分钟）
CONFIRM_TOKEN_MAX_AGE = 600

# token 签名盐前缀（实际 salt 再绑定 group_id+diff_hash+user.pk）
_CONFIRM_SALT_PREFIX = "pms2.permission_center.confirm."

# 矩阵动作列（6 固定键中的 5 个非专项键；special 列以 SPECIAL_PERMISSIONS 呈现）
MATRIX_ACTIONS = ("view", "add", "change", "delete", "approve")
ACTION_LABELS = {
    "view": "查看", "add": "新增", "change": "修改",
    "delete": "删除", "approve": "审批", "special": "专项",
}

# 专项权限展示名（纯展示用途，不参与任何授权判定）
SPECIAL_LABELS = {
    "guest_document_reveal": "揭示客人证件完整值",
    "guest_phone_reveal": "揭示客人电话完整值",
    "refund_request": "退款申请",
    "refund_approve": "退款审批",
    "reverse_entry": "反结账/红字冲销",
    "room_release_ready": "净房放行",
    "no_show_handle": "未到订单处理",
    "finance_access": "财务管理入口",
    "night_audit_execute": "执行夜审",
    "business_date_reopen": "重开营业日",
    "breakglass_review": "break-glass 事后复核",
    "export_pii": "导出敏感数据",
    "manage_rates": "每日房价调整",
    "staff_manage": "员工与账号管理",
}

# §8.1 页面固定文案（页面必须原文呈现）
PLATFORM_SCOPE_NOTICE = (
    "平台超管拥有平台域管理权限；PMS 业务权限仍以员工任职和岗位授权为准"
)


# ---------------------------------------------------------------------------
# 注册表派生授权面（N-1 修复：allowed 只来自注册表，不再有手工白名单）
# ---------------------------------------------------------------------------

def active_module_codes():
    """ACTIVE 模块登记的全部权限码（去重排序元组）。"""
    codes = set()
    for module in MODULE_REGISTRY_V2.values():
        if module["status"] == MODULE_ACTIVE:
            codes.update(iter_module_permission_codes(module))
    return codes


def allowed_codenames():
    """可授予白名单 = ACTIVE 模块全部权限码 ∪ SPECIAL_PERMISSIONS 码。"""
    return active_module_codes() | {s["codename"] for s in SPECIAL_PERMISSIONS}


def managed_universe_codenames():
    """注册表管辖全集（含 PLANNED/RETIRED 模块码）：保存时管辖面整体声明式
    覆盖，管辖面之外的权限（Django Admin 手工授予）原样保留。"""
    codes = set()
    for module in MODULE_REGISTRY_V2.values():
        codes.update(iter_module_permission_codes(module))
    codes.update(s["codename"] for s in SPECIAL_PERMISSIONS)
    return codes


def special_codenames():
    """SPECIAL_PERMISSIONS 全量码（高危变更判定依据）。"""
    return {s["codename"] for s in SPECIAL_PERMISSIONS}


# ---------------------------------------------------------------------------
# 内容 hash 版本 / diff / 高危 token
# ---------------------------------------------------------------------------

def content_version(codenames):
    """权限码全集（排序后）的 sha256 前 16 位内容 hash（§8.1 version）。"""
    payload = "\n".join(sorted(codenames)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def diff_hash(added, removed):
    """added/removed 两组码的稳定短 hash（token 绑定用）。"""
    payload = json.dumps(
        {"added": sorted(added), "removed": sorted(removed)},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def compute_diff(current, requested):
    """before/after diff：added = requested−current，removed = current−requested。

    requested 已经过 allowed 校验；current 为组当前全集（含管辖外码时，
    管辖外码由调用方并入 requested 侧保留，避免误报 removed）。
    """
    added = set(requested) - set(current)
    removed = set(current) - set(requested)
    return added, removed


def is_high_risk_diff(added, removed):
    """diff 是否触及任一 SPECIAL_PERMISSIONS 码（§8.3.f 高危变更判定）。"""
    touched = set(added) | set(removed)
    return not touched.isdisjoint(special_codenames())


def _confirm_salt(group_id, user_pk, diff_digest):
    """salt 绑定 group_id+diff_hash+user.pk（§8.3.f）。"""
    material = f"{group_id}|{diff_digest}|{user_pk}".encode("utf-8")
    return _CONFIRM_SALT_PREFIX + hashlib.sha256(material).hexdigest()[:16]


def issue_confirm_token(group_id, user_pk, added, removed):
    """签发高危变更确认 token：不落库、不存明文，签名即凭证。"""
    digest = diff_hash(added, removed)
    salt = _confirm_salt(group_id, user_pk, digest)
    signer = TimestampSigner(salt=salt)
    return signer.sign(f"{group_id}:{user_pk}:{digest}"), digest


def validate_confirm_token(token, group_id, user_pk, added, removed):
    """校验确认 token。返回 None=有效，否则返回拒绝原因码。

    salt 携带当前请求的 diff_hash——diff 变化（含保存成功后的重放）时
    salt 不匹配 → BadSignature；超时 → SignatureExpired。
    """
    digest = diff_hash(added, removed)
    salt = _confirm_salt(group_id, user_pk, digest)
    signer = TimestampSigner(salt=salt)
    try:
        value = signer.unsign(token, max_age=CONFIRM_TOKEN_MAX_AGE)
    except SignatureExpired:
        return "token_expired"
    except BadSignature:
        return "token_invalid"
    if value != f"{group_id}:{user_pk}:{digest}":
        return "token_invalid"
    return None


# ---------------------------------------------------------------------------
# 组 / 角色元数据与影响范围
# ---------------------------------------------------------------------------

def role_meta_for_group(group):
    """Group → ROLE_REGISTRY 元数据（未登记角色返回 None）。"""
    if group is None:
        return None
    for role in ROLE_REGISTRY:
        if role["group_name"] == group.name:
            return role
    return None


def group_permission_codes(group):
    """组当前全部权限码全集（"app_label.codename"，跨 App）。"""
    return {f"{app_label}.{codename}"
            for app_label, codename in group.permissions.values_list(
                "content_type__app_label", "codename")}


def effective_assignments(group):
    """某角色组当前生效（starts_at≤now<ends_at 或长期）的岗位授权查询集。"""
    now = timezone.now()
    return StaffRoleAssignment.objects.filter(
        group=group, starts_at__lte=now,
    ).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))


def role_impact(group):
    """影响范围计数（§8.1 impact）：员工数按去重员工档案，任职数按去重任职。"""
    effective = effective_assignments(group)
    membership_count = effective.values("membership_id").distinct().count()
    staff_count = (effective
                   .values("membership__staff_profile_id").distinct().count())
    return {"staff_count": staff_count, "membership_count": membership_count}


def last_matrix_save(group):
    """最近一次矩阵保存审计（§8.1 右区展示）。"""
    event = AuditEvent.objects.filter(
        action="permissions.matrix_saved",
        entity_type="Group", entity_id=str(group.pk)).first()
    if event is None:
        return None
    return {
        "at": timezone.localtime(event.created_at).isoformat(),
        "actor": event.actor.username if event.actor else "",
        "reason": event.reason,
        "request_id": event.request_id,
    }


# ---------------------------------------------------------------------------
# 页面 bootstrap（矩阵结构为静态注册表数据，经 json_script 嵌入页面）
# ---------------------------------------------------------------------------

def page_bootstrap():
    """四区页面初始化数据：角色目录（含计数）+ 注册表矩阵 + 专项权限。"""
    group_ids = {g.name: g.id for g in Group.objects.filter(
        name__in=[r["group_name"] for r in ROLE_REGISTRY])}
    roles = []
    for role in ROLE_REGISTRY:
        group_id = group_ids.get(role["group_name"])
        staff_count = membership_count = 0
        if group_id is not None:
            impact = role_impact(Group.objects.get(pk=group_id))
            staff_count = impact["staff_count"]
            membership_count = impact["membership_count"]
        roles.append({
            "key": role["key"], "name": role["name"],
            "group_id": group_id, "status": role["status"],
            "editable": role["editable"], "risk_level": role["risk_level"],
            "description": role["description"],
            "staff_count": staff_count, "membership_count": membership_count,
        })

    specials_by_module = {}
    for item in SPECIAL_PERMISSIONS:
        specials_by_module.setdefault(item["module"], []).append({
            "key": item["key"], "codename": item["codename"],
            "risk": item["risk"],
            "label": SPECIAL_LABELS.get(item["key"], item["codename"]),
            "note": item["note"],
            "requires_reason": item["requires_reason"],
            "requires_confirm": item["requires_confirm"],
        })

    modules = []
    for module in MODULE_REGISTRY_V2.values():
        columns = []
        matrix_codes = set()
        for action in MATRIX_ACTIONS:
            value = module["actions"].get(action, NOT_APPLICABLE)
            code = None if value == NOT_APPLICABLE else value
            if code:
                matrix_codes.add(code)
            columns.append({
                "action": action, "label": ACTION_LABELS[action],
                "code": code,
            })
        # 专项区只列矩阵动作列未呈现的专项码（与矩阵列同码的不重复渲染）
        specials = [s for s in specials_by_module.get(module["code"], [])
                    if s["codename"] not in matrix_codes]
        modules.append({
            "code": module["code"], "name": module["name_zh"],
            "status": module["status"], "risk_level": module["risk_level"],
            "app_label": module["app_label"],
            "property_scoped": module["property_scoped"],
            "columns": columns, "specials": specials,
        })

    return {
        "roles": roles,
        "modules": modules,
        "special_codenames": sorted(special_codenames()),
        "allowed_count": len(allowed_codenames()),
        "scope_notice": PLATFORM_SCOPE_NOTICE,
    }


# ---------------------------------------------------------------------------
# 请求解析辅助
# ---------------------------------------------------------------------------

class _ApiError(Exception):
    """携带现成 JsonResponse 的流程中断（视图内统一捕获返回）。"""

    def __init__(self, response):
        super().__init__(response.status_code)
        self.response = response


def _json_body(request):
    """解析 JSON 请求体；非法 → 400。"""
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ApiError(JsonResponse(
            {"error": "请求体必须是有效 JSON。", "code": "bad_json"}, status=400))
    if not isinstance(data, dict):
        raise _ApiError(JsonResponse(
            {"error": "请求体必须是 JSON 对象。", "code": "bad_json"}, status=400))
    return data


def _requested_permissions(data):
    """校验 permissions 字段：字符串列表。"""
    requested = data.get("permissions")
    if (not isinstance(requested, list)
            or not all(isinstance(c, str) for c in requested)):
        raise _ApiError(JsonResponse(
            {"error": "permissions 必须是权限码字符串列表。",
             "code": "bad_permissions"}, status=400))
    # 全角/空串防护：去首尾空白并丢弃空串
    cleaned = {code.strip() for code in requested}
    cleaned.discard("")
    return cleaned


def _validate_reason(data):
    """reason 必须 1-240 字符（§8.3.g/i），缺失/超长 → 400。"""
    reason = data.get("reason")
    if not isinstance(reason, str):
        raise _ApiError(JsonResponse(
            {"error": "变更理由必填（1-240 字符）。",
             "code": "reason_required"}, status=400))
    reason = reason.strip()
    if not reason:
        raise _ApiError(JsonResponse(
            {"error": "变更理由必填（1-240 字符）。",
             "code": "reason_required"}, status=400))
    if len(reason) > 240:
        raise _ApiError(JsonResponse(
            {"error": "变更理由不能超过 240 字符。",
             "code": "reason_too_long"}, status=400))
    return reason


def _get_registry_group(group_id):
    """加载组并确认属于 ROLE_REGISTRY（未登记 → 404）。"""
    group = Group.objects.filter(pk=group_id).first()
    if group is None or role_meta_for_group(group) is None:
        raise _ApiError(JsonResponse(
            {"error": "角色不存在或未在注册表登记。", "code": "group_not_found"},
            status=404))
    return group


def _reject_unknown(request, group, requested):
    """allowed 校验：越权码整体拒绝（§8.3.c，不静默过滤）+ 安全审计。"""
    unknown = sorted(set(requested) - allowed_codenames())
    if not unknown:
        return
    AuditEvent.record(
        action="security.permission_denied",
        entity_type="Group", entity_id=str(group.pk), actor=request.user,
        reason="permission_center 越权码整体拒绝: " + "、".join(unknown),
        request=request)
    raise _ApiError(JsonResponse({
        "error": "包含不可授予的权限码，整体拒绝（不静默过滤）："
                 + "、".join(unknown),
        "code": "unknown_permissions",
        "unknown": unknown,
    }, status=409))


def _split_preserved(current):
    """管辖外权限码（注册表未登记，如 Django Admin 手工授予的 auth.*）。"""
    return set(current) - managed_universe_codenames()


# ---------------------------------------------------------------------------
# API 视图
# ---------------------------------------------------------------------------

@require_GET
@platform_superadmin_required
def permission_read(request, group_id):
    """GET /platform/api/permissions/<group_id>/（§8.1 读取 API）。"""
    try:
        group = _get_registry_group(group_id)
    except _ApiError as exc:
        return exc.response
    role = role_meta_for_group(group)
    current = group_permission_codes(group)
    held_specials = sorted(set(current) & special_codenames())
    impact = role_impact(group)
    return JsonResponse({
        "role": {
            "key": role["key"], "name": role["name"],
            "status": role["status"], "editable": role["editable"],
            "risk_level": role["risk_level"],
            "description": role["description"],
        },
        "permissions": sorted(current),
        "version": content_version(current),
        "impact": impact,
        "special": held_specials,
        # 保存时将被声明式回收的历史 PLANNED 授权（UI 预告，非静默）
        "reclaimed_on_save": sorted(
            (current & managed_universe_codenames()) - allowed_codenames()),
        "last_save": last_matrix_save(group),
    })


@require_POST
@platform_superadmin_required
def permission_save(request, group_id):
    """POST /platform/api/permissions/<group_id>/save/（§8.3 九步保存流程）。"""
    try:
        data = _json_body(request)                      # JSON → 400
        reason = _validate_reason(data)                 # reason → 400
        requested = _requested_permissions(data)        # permissions 形状 → 400
        group = _get_registry_group(group_id)           # 未登记组 → 404
        _reject_unknown(request, group, requested)      # allowed → 409+审计
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise _ApiError(JsonResponse(
                {"error": "缺少 version（内容 hash 并发保护）。",
                 "code": "version_required"}, status=400))
        confirm_token = data.get("confirm_token")
        if confirm_token is not None and not isinstance(confirm_token, str):
            raise _ApiError(JsonResponse(
                {"error": "confirm_token 必须是字符串。",
                 "code": "token_invalid"}, status=403))
    except _ApiError as exc:
        return exc.response

    try:
        with transaction.atomic():
            # 行锁串行化并发保存；version/diff/token 全部基于事务内最新状态
            group = Group.objects.select_for_update().get(pk=group.pk)
            current = group_permission_codes(group)

            # §8.3.d 内容 hash 并发保护：不匹配 → 409
            if content_version(current) != version:
                raise _ApiError(JsonResponse({
                    "error": "版本冲突：角色权限已被他人修改，请刷新后重试。",
                    "code": "version_conflict",
                    "current_version": content_version(current),
                }, status=409))

            # §8.3.e diff（管辖外码原样并入请求侧保留，不计入 removed）
            preserved = _split_preserved(current)
            after = requested | preserved
            added, removed = compute_diff(current, after)

            # §8.3.f 高危变更必须携带有效 confirm_token（缺失/过期/diff 不匹配 → 403）
            if is_high_risk_diff(added, removed) or confirm_token is not None:
                if confirm_token is None:
                    raise _ApiError(JsonResponse({
                        "error": "本次变更触及高危专项权限，必须先获取确认令牌"
                                 "（confirm-token）后携带 confirm_token 再保存。",
                        "code": "token_required",
                        "added": sorted(added), "removed": sorted(removed),
                    }, status=403))
                problem = validate_confirm_token(
                    confirm_token, group.pk, request.user.pk, added, removed)
                if problem is not None:
                    raise _ApiError(JsonResponse({
                        "error": {
                            "token_required": "缺少有效确认令牌。",
                            "token_expired": "确认令牌已过期（10 分钟有效期），"
                                             "请重新获取。",
                            "token_invalid": "确认令牌无效：与当前变更内容不匹配。",
                        }[problem],
                        "code": problem,
                    }, status=403))

            # §8.3.g 保存与审计同事务（§8.3.9 一致性）
            perms = list(Permission.objects.filter(
                content_type__app_label__in={c.split(".", 1)[0] for c in after},
                codename__in={c.split(".", 1)[1] for c in after}))
            # 双保险：集合交集回验（防 app_label/codename 交叉错配）
            perms = [p for p in perms
                     if f"{p.content_type.app_label}.{p.codename}" in after]
            group.permissions.set(perms)

            event = AuditEvent.record(
                action="permissions.matrix_saved",
                entity_type="Group", entity_id=str(group.pk),
                actor=request.user, reason=reason,
                before=sorted(current), after=sorted(after),
                request=request)
    except _ApiError as exc:
        # atomic 块内拒绝：事务回滚，任何授权与审计变更都不落库
        return exc.response

    return JsonResponse({
        "saved": True,
        "added": sorted(added),
        "removed": sorted(removed),
        "version": content_version(after),
        "request_id": event.request_id,
    })


@require_POST
@platform_superadmin_required
def permission_confirm_token(request, group_id):
    """POST /platform/api/permissions/<group_id>/confirm-token/（§8.3.f）。

    接收拟提交的权限码集合，计算 diff hash 并返回一次性签名 token；
    不落库、不存明文——签名即凭证，10 分钟有效。
    """
    try:
        data = _json_body(request)
        requested = _requested_permissions(data)
        group = _get_registry_group(group_id)
        _reject_unknown(request, group, requested)
    except _ApiError as exc:
        return exc.response

    current = group_permission_codes(group)
    preserved = _split_preserved(current)
    added, removed = compute_diff(current, requested | preserved)
    if not added and not removed:
        return JsonResponse(
            {"error": "无实际变更，无需确认令牌。", "code": "no_changes"},
            status=400)

    token, digest = issue_confirm_token(
        group.pk, request.user.pk, added, removed)
    return JsonResponse({
        "token": token,
        "diff_hash": digest,
        "added": sorted(added),
        "removed": sorted(removed),
        "high_risk": is_high_risk_diff(added, removed),
        "expires_in": CONFIRM_TOKEN_MAX_AGE,
    })
