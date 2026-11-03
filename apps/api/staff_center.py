# -*- coding: utf-8 -*-
"""员工中心 + 会员管理只读 API（V1.1 Phase 1）。

Staff API：列表/详情（角色/任职/账号状态/审计时间线）。
Membership API：列表/详情（等级/状态/旧 Vip 映射状态）。
统一使用 api_contract 封套；物业范围过滤；权限门控。
"""
import json

from django.views.decorators.http import require_GET, require_POST

from apps.core import labels as L
from apps.core.api_contract import ApiError, api_view, api_response, paginate
from apps.identity.authorization import AuthorizationService
from apps.identity.models import (
    PropertyStaffMembership, StaffProfile, StaffRoleAssignment,
)
from apps.guests.models import Guest, GuestMembership, MembershipLevelHistory
from apps.guests.services import GuestMembershipService, GuestServiceError
from apps.core.api_contract import idempotent

from apps.core.api_contract import ApiError

def _api_error(code, message):
    return ApiError(code, message)


def timezone_now():
    from django.utils import timezone
    return timezone.now()



def _staff_dto(profile, property_obj):
    membership = PropertyStaffMembership.objects.filter(
        staff_profile=profile, property=property_obj).first()
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
        "employment_status_label": L.employment_status_labels().get(
            profile.employment_status, profile.employment_status),
        "membership_status": membership.status if membership else None,
        "membership_status_label": (
            L.gmembership_status_labels().get(membership.status, membership.status)
            if membership else None),
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

    qs = StaffProfile.objects.filter(
        property_memberships__property=property_obj
    ).select_related("user").distinct().order_by("-created_at")
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
        profile = StaffProfile.objects.filter(
                property_memberships__property=property_obj
            ).select_related("user").get(pk=profile_id)
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
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(member_no__icontains=q) | Q(guest__full_name__icontains=q)
            | Q(guest__phone__icontains=q)
        )
    items, pagination = paginate(qs, request)
    data = [{
        "id": m.pk,
        "member_no": m.member_no,
        "guest": {"id": m.guest.pk, "full_name": m.guest.full_name},
        "level_code": m.level_code,
        "level_label": L.member_level_label(m.level_code),
        "status": m.status,
        "status_label": L.gmembership_status_labels().get(m.status, m.status),
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
        "level_code": m.level_code,
        "level_label": L.member_level_label(m.level_code),
        "status": m.status,
        "status_label": L.gmembership_status_labels().get(m.status, m.status),
        "joined_at": str(m.joined_at) if m.joined_at else None,
        "legacy_vip_id": m.legacy_vip_id or None,
        "level_history": [{
            "from_level": h.from_level, "to_level": h.to_level,
            "reason": h.reason, "changed_at": h.created_at.isoformat(),
        } for h in history],
        "source_system": "PMS2",
    }
    return api_response(request, data)


def _membership_body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError("VALIDATION_ERROR", "请求体必须是有效 JSON。")


@require_POST
@api_view
@idempotent
def membership_create(request):
    """创建物业内会员关系；不按手机号自动合并客人档案。"""
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            request.user, property_obj, "hotelapp.add_vip"):
        raise ApiError("PERMISSION_DENIED", "缺少会员新增权限。")
    body = _membership_body(request)
    guest = Guest.objects.filter(
        pk=body.get("guest_id"), property=property_obj).first()
    if guest is None:
        raise ApiError("RESOURCE_NOT_FOUND", "当前物业下不存在该客人档案。")
    joined_at = None
    if body.get("joined_at"):
        from datetime import date
        try:
            joined_at = date.fromisoformat(str(body["joined_at"]))
        except ValueError:
            raise ApiError("VALIDATION_ERROR", "joined_at 必须使用 YYYY-MM-DD 格式。")
    try:
        membership = GuestMembershipService.create_membership(
            property_obj=property_obj, guest=guest,
            member_no=body.get("member_no"),
            level_code=body.get("level_code") or "STANDARD",
            joined_at=joined_at, source="PMS2 管理中心",
            created_by=request.user, request=request)
    except GuestServiceError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "id": membership.pk, "member_no": membership.member_no,
        "guest_id": guest.pk, "level_code": membership.level_code,
        "status": membership.status,
    }, status=201)


@require_POST
@api_view
@idempotent
def membership_change_level(request, membership_id):
    """会员等级变更：必须填写业务原因，写等级历史与审计。"""
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    if not AuthorizationService.has_permission(
            request.user, property_obj, "hotelapp.change_vip"):
        raise ApiError("PERMISSION_DENIED", "缺少会员修改权限。")
    membership = GuestMembership.objects.filter(
        pk=membership_id, property=property_obj).first()
    if membership is None:
        raise ApiError("RESOURCE_NOT_FOUND", "当前物业下不存在该会员关系。")
    body = _membership_body(request)
    try:
        membership = GuestMembershipService.change_level(
            membership=membership, new_level=body.get("level_code"),
            reason=body.get("reason"), changed_by=request.user, request=request)
    except GuestServiceError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "id": membership.pk, "level_code": membership.level_code,
        "status": membership.status,
    })
