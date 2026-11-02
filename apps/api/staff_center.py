# -*- coding: utf-8 -*-
"""员工中心 + 会员管理只读 API（V1.1 Phase 1）。

Staff API：列表/详情（角色/任职/账号状态/审计时间线）。
Membership API：列表/详情（等级/状态/旧 Vip 映射状态）。
统一使用 api_contract 封套；物业范围过滤；权限门控。
"""
from django.views.decorators.http import require_GET

from apps.core.api_contract import ApiError, api_view, api_response, paginate
from apps.identity.authorization import AuthorizationService
from apps.identity.models import (
    PropertyStaffMembership, StaffProfile, StaffRoleAssignment,
)
from apps.guests.models import GuestMembership, MembershipLevelHistory

from apps.core.api_contract import ApiError

def _api_error(code, message):
    return ApiError(code, message)


def timezone_now():
    from django.utils import timezone
    return timezone.now()



def _staff_dto(profile, property_obj):
    membership = PropertyStaffMembership.objects.filter(
        staff_profile=profile).first()
    user = profile.user
    now = timezone_now()
    assignments = []
    if membership:
        for a in StaffRoleAssignment.objects.filter(
                membership=membership).select_related("group"):
            active = a.starts_at <= now and (a.ends_at is None or a.ends_at > now)
            assignments.append({
                "role": a.group.name, "active": active,
                "starts_at": a.starts_at.isoformat(),
                "ends_at": a.ends_at.isoformat() if a.ends_at else None,
            })
    return {
        "id": profile.pk,
        "full_name": profile.full_name,
        "phone_masked": (profile.phone[:3] + "****" + profile.phone[-4:]
                         if len(profile.phone or "") >= 8 else profile.phone or None),
        "employee_no": membership.employee_no if membership else None,
        "position_code": membership.position_code if membership else None,
        "employment_status": profile.employment_status,
        "membership_status": membership.status if membership else None,
        "account": ({
            "username": user.username, "is_active": user.is_active,
            "last_login": user.last_login.isoformat() if user.last_login else None,
        } if user else None),
        "active_roles": [a["role"] for a in assignments if a["active"]],
        "assignments": assignments,
        "source_system": "PMS2",
    }


@require_GET
@api_view
def staff_list(request):
    """员工中心：StaffProfile 列表（分页/搜索/角色标签）。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise _api_error("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(user, property_obj, "identity.staff_manage"):
        raise _api_error("PERMISSION_DENIED", "缺少员工管理权限。")

    qs = StaffProfile.objects.select_related("user").order_by("-created_at")
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(full_name__icontains=q) |
            Q(property_memberships__employee_no__icontains=q)
        ).distinct()
    items, pagination = paginate(qs, request)
    data = [_staff_dto(p, property_obj) for p in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def staff_detail(request, profile_id):
    """员工详情：档案 + 任职 + 角色授权历史 + 账号状态。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if not AuthorizationService.has_permission(user, property_obj, "identity.staff_manage"):
        raise _api_error("PERMISSION_DENIED", "缺少员工管理权限。")
    try:
        profile = StaffProfile.objects.select_related("user").get(pk=profile_id)
    except StaffProfile.DoesNotExist:
        raise _api_error("RESOURCE_NOT_FOUND", "员工不存在。")
    data = _staff_dto(profile, property_obj)
    return api_response(request, data)


@require_GET
@api_view
def membership_list(request):
    """会员管理：GuestMembership 列表（等级/状态/旧映射）。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise _api_error("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_vip"):
        raise _api_error("PERMISSION_DENIED", "缺少会员查看权限。")

    qs = GuestMembership.objects.filter(
        property=property_obj).select_related("guest").order_by("-created_at")
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    items, pagination = paginate(qs, request)
    data = [{
        "id": m.pk,
        "member_no": m.member_no,
        "guest": {"id": m.guest.pk, "full_name": m.guest.full_name},
        "level_code": m.level_code,
        "status": m.status,
        "joined_at": str(m.joined_at) if m.joined_at else None,
        "expires_at": str(m.expires_at) if m.expires_at else None,
        "legacy_vip_id": m.legacy_vip_id or None,
        "source_system": "PMS2",
    } for m in items]
    return api_response(request, data, pagination=pagination)


@require_GET
@api_view
def membership_detail(request, membership_id):
    """会员详情：等级历史 + Guest 摘要。"""
    user = request.user
    property_obj = getattr(request, "property", None)
    if not AuthorizationService.has_permission(
            user, property_obj, "hotelapp.view_vip"):
        raise _api_error("PERMISSION_DENIED", "缺少会员查看权限。")
    try:
        m = GuestMembership.objects.select_related("guest").get(
            pk=membership_id, property=property_obj)
    except GuestMembership.DoesNotExist:
        raise _api_error("RESOURCE_NOT_FOUND", "会员关系不存在。")
    history = MembershipLevelHistory.objects.filter(
        membership=m).order_by("-created_at")
    data = {
        "id": m.pk, "member_no": m.member_no,
        "guest": {"id": m.guest.pk, "full_name": m.guest.full_name},
        "level_code": m.level_code, "status": m.status,
        "joined_at": str(m.joined_at) if m.joined_at else None,
        "legacy_vip_id": m.legacy_vip_id or None,
        "level_history": [{
            "from_level": h.from_level, "to_level": h.to_level,
            "reason": h.reason, "changed_at": h.created_at.isoformat(),
        } for h in history],
        "source_system": "PMS2",
    }
    return api_response(request, data)
