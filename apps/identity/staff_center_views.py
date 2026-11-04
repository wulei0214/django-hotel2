# -*- coding: utf-8 -*-
"""员工中心写操作视图（治理方案 §10.1：员工中心写操作迁移与旧入口退役）。

路由全部挂 /manage/staff/ 与 /manage/staff/actions/...（PMS 会话域，非 /platform/）：
  * 页面      GET  /manage/staff/                          templates/pms/staff_center.html
  * 新增员工  POST /manage/staff/actions/create/
  * 开通账号  POST /manage/staff/actions/<pk>/provision/
  * 角色分配  POST /manage/staff/actions/<pk>/assign_role/
  * 结束授权  POST /manage/staff/actions/<pk>/end_assignment/<assignment_id>/
  * 任职状态  POST /manage/staff/actions/<pk>/set_status/
  * 密码重置  POST /manage/staff/actions/<pk>/reset_password/
  * 邀请创建  POST /manage/staff/actions/<pk>/invite/          （AUTH-17，方案 §5.3）
  * 邀请撤销  POST /manage/staff/actions/<pk>/invite/revoke/
  * 邀请重发  POST /manage/staff/actions/<pk>/invite/resend/

门禁：@staff_required（登录 + DEFAULT 有效任职 + 强制改密拦截）
      + @pms_permission_required("identity.staff_manage")，与旧 /staff_accounts/ 完全一致。

写入纪律：六个写操作一律调用 EmployeeLifecycleService——本模块不直接
create_user / 改 Group / set_password；服务自带事务与 AuditEvent。
邀请三操作（AUTH-17）调用 EmployeeInvitationService（状态机收口）+
EmployeeLifecycleService（账号开通）——本模块不直接创建 User 或 Invitation。
EmployeeLifecycleError / InvitationError 统一转 ApiError("VALIDATION_ERROR")
→ HTTP 400 中文提示。响应契约沿用 apps.core.api_contract 封套（{data, meta}）。

初始密码语义（§10.1 步骤 1.f）：管理员留空时由服务端生成强随机密码，
仅在本次响应中返回一次（"初始密码仅本次显示"），不落库不落审计；
无论生成或手输，开通/重置后 must_change_password=True，首登强制改密。

邀请语义（AUTH-17，§5.3）：创建/重发响应返回一次性明文链接
（/invite/accept?token=...，仅本次显示）；界面文案固定为
"邀请链接已生成，请复制并人工发送"——不配置邮件/短信供应商，
不得显示"发送成功"。邀请操作同样限流（§5.3，identity.invitation_manage）。
"""
import datetime
import secrets
import string

from django.contrib.auth.models import Group
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.core import rate_limit
from apps.core.api_contract import ApiError, api_response, api_view
from apps.identity.decorators import pms_permission_required
from apps.identity.invitation_services import (
    EmployeeInvitationService,
    InvitationError,
    invite_accept_url,
)
from apps.identity.roles import BUSINESS_ROLE_NAMES
from apps.identity.services import (
    EmployeeLifecycleError,
    EmployeeLifecycleService,
)
from .models import Invitation, StaffProfile, StaffRoleAssignment

# 任职状态动作白名单（SUSPENDED 由服务层映射为档案 INACTIVE）
STATUS_ACTIONS = ("ACTIVE", "SUSPENDED", "TERMINATED")
STATUS_MESSAGES = {
    "ACTIVE": "已恢复在职",
    "SUSPENDED": "已暂停任职",
    "TERMINATED": "已办理离职（其登录账号已同步停用）",
}


def _generate_initial_password(length=12):
    """生成一次性初始密码：大小写字母+数字强随机组合，可通过 validate_password。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in candidate)
                and any(c.isupper() for c in candidate)
                and any(c.isdigit() for c in candidate)):
            return candidate


def _call_service(func, *args, **kwargs):
    """EmployeeLifecycleError → 400 中文提示（api_contract 封套）。"""
    try:
        return func(*args, **kwargs)
    except EmployeeLifecycleError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))


def _get_profile_or_error(profile_id, property_obj):
    """物业范围内的员工档案；越店访问一律 404（不泄露存在性）。"""
    profile = (StaffProfile.objects
               .filter(pk=profile_id, property_memberships__property=property_obj)
               .select_related("user")
               .first())
    if profile is None:
        raise ApiError("RESOURCE_NOT_FOUND", "员工不存在或不属于当前物业。")
    return profile


def _initial_password_response(password):
    """管理员留空密码时服务端生成；仅本次随响应显示。"""
    if password:
        return password, None
    generated = _generate_initial_password()
    return generated, generated


# ---------- 员工中心页面（§10.1 步骤 1） ----------

def _staff_center_page(request):
    """员工生命周期工作台：列表读 /api/v1/staff，操作面板走 actions 路由。"""
    from hotelapp.models import Staff as LegacyStaff
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return HttpResponse("当前账号没有可用物业上下文", status=503)
    return render(request, "pms/staff_center.html", {
        "property_name": property_obj.name,
        "workbench_roles": list(BUSINESS_ROLE_NAMES),
        "unmapped_staff": LegacyStaff.objects.filter(pms_staff_profile__isnull=True),
    })


# ---------- 写操作（每个操作都收口 EmployeeLifecycleService） ----------

def _action_create(request):
    """新增员工：建档 + DEFAULT 任职 + 可选开通账号 + 初始角色（STAFF-01/02）。"""
    from hotelapp.models import Staff as LegacyStaff
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        raise ApiError("RESOURCE_NOT_FOUND", "物业上下文不可用。")
    full_name = (request.POST.get("full_name") or "").strip()
    employee_no = (request.POST.get("employee_no") or "").strip()
    position_code = (request.POST.get("position_code") or "").strip() or "UNASSIGNED"
    phone = (request.POST.get("phone") or "").strip()
    username = (request.POST.get("username") or "").strip()
    legacy_id = (request.POST.get("legacy_staff") or "").strip()
    legacy = (LegacyStaff.objects.filter(员工id=legacy_id).first()
              if legacy_id.isdigit() else None)

    password, generated = _initial_password_response(request.POST.get("password") or "")
    role_names = [r for r in request.POST.getlist("roles") if r in BUSINESS_ROLE_NAMES]
    groups = list(Group.objects.filter(name__in=role_names))

    profile, membership, user = _call_service(
        EmployeeLifecycleService.create_employee,
        property_obj=property_obj, full_name=full_name, employee_no=employee_no,
        position_code=position_code, phone=phone, legacy_staff=legacy,
        account_username=username, account_password=password,
        role_groups=groups, granted_by=request.user)
    message = f"员工「{profile.full_name}」已建档" + (
        "，登录账号已开通" if user else "（未开通登录账号）")
    data = {"message": message, "profile_id": profile.pk,
            "employee_no": membership.employee_no}
    if user is not None:
        data["must_change_password"] = True
        if generated:
            data["initial_password"] = generated
            data["message"] += "；初始密码仅本次显示，请立即线下告知本人"
    return api_response(request, data)


def _action_provision(request, pk):
    """开通登录账号（STAFF-02）：一次性初始密码，首登强制改密。"""
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    username = (request.POST.get("username") or "").strip()
    password, generated = _initial_password_response(request.POST.get("password") or "")
    _call_service(
        EmployeeLifecycleService.provision_account,
        profile, username=username, password=password,
        granted_by=request.user, property_obj=property_obj)
    message = "登录账号已开通" + (
        "；初始密码仅本次显示，请立即线下告知本人" if generated
        else "；初始密码请线下告知本人") + "；首次登录将强制修改密码"
    data = {"message": message, "username": username,
            "must_change_password": True}
    if generated:
        data["initial_password"] = generated
    return api_response(request, data)


def _action_assign_role(request, pk):
    """分配角色（STAFF-07）：可选结束时间（临时兼岗）+ 原因（记入审计）。"""
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    membership = EmployeeLifecycleService.get_membership(profile, property_obj)
    if membership is None:
        raise ApiError("VALIDATION_ERROR", "该员工在当前物业没有有效任职记录，不能授予岗位。")
    group = Group.objects.filter(name=request.POST.get("role") or "").first()
    ends_at_raw = (request.POST.get("ends_at") or "").strip()
    ends_at = None
    if ends_at_raw:
        try:
            ends_at = datetime.datetime.strptime(ends_at_raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            raise ApiError("VALIDATION_ERROR", "结束时间格式无效，请使用日期时间选择器提供的格式。")
    assignment = _call_service(
        EmployeeLifecycleService.assign_role,
        membership=membership, group=group, ends_at=ends_at,
        granted_by=request.user, reason=(request.POST.get("reason") or "").strip())
    return api_response(request, {
        "message": f"已授权角色「{assignment.group.name}」"
                   + ("（临时授权）" if ends_at else ""),
        "assignment_id": assignment.pk, "role": assignment.group.name,
    })


def _action_end_assignment(request, pk, assignment_id):
    """结束一条角色授权（STAFF-08）：调岗/撤销，历史保留不删除。"""
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    assignment = (StaffRoleAssignment.objects
                  .select_related("group", "membership__staff_profile")
                  .filter(pk=assignment_id,
                          membership__staff_profile=profile,
                          membership__property=property_obj)
                  .first())
    if assignment is None:
        raise ApiError("RESOURCE_NOT_FOUND", "角色授权记录不存在或不属于该员工。")
    _call_service(
        EmployeeLifecycleService.end_assignment,
        assignment, ended_by=request.user,
        reason=(request.POST.get("reason") or "").strip() or "调岗/撤销")
    return api_response(request, {
        "message": f"角色「{assignment.group.name}」的授权已结束（历史保留）",
        "assignment_id": assignment.pk,
    })


def _action_set_status(request, pk):
    """任职状态（STAFF-09）：暂停/恢复/离职；离职同步终止授权并停用账号。"""
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    status = request.POST.get("status") or ""
    if status not in STATUS_ACTIONS:
        raise ApiError("VALIDATION_ERROR", "未知的任职状态操作。")
    _call_service(
        EmployeeLifecycleService.set_employment_status,
        profile, property_obj=property_obj, status=status,
        ended_by=request.user, reason=(request.POST.get("reason") or "").strip())
    return api_response(request, {
        "message": STATUS_MESSAGES[status], "status": status,
    })


def _action_reset_password(request, pk):
    """密码重置（§10.1 步骤 1.f）：must_change_password=True；明文仅本次显示。"""
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    password, generated = _initial_password_response(request.POST.get("password") or "")
    _call_service(
        EmployeeLifecycleService.reset_password,
        profile, property_obj=property_obj, new_password=password,
        reset_by=request.user)
    data = {"message": "密码已重置；该账号下次登录将强制修改密码",
            "must_change_password": True}
    if generated:
        data["initial_password"] = generated
        data["message"] = "密码已重置；新初始密码仅本次显示，请立即线下告知本人"
    return api_response(request, data)


# ---------- 邀请激活（AUTH-17，方案 §5.2/§5.3：状态机与 token 收口服务层） ----------

# §5.3：邀请创建/重发/撤销接口必须限流（已认证管理面，IP 维度防脚本滥用）
INVITE_RATE_ACTION = "identity.invitation_manage"


def _invite_rate_guard(request):
    if not rate_limit.check(request, INVITE_RATE_ACTION):
        raise ApiError("RATE_LIMITED", "邀请操作过于频繁，请稍后再试。")


def _get_membership_or_error(profile, property_obj):
    membership = EmployeeLifecycleService.get_membership(profile, property_obj)
    if membership is None:
        raise ApiError("VALIDATION_ERROR",
                       "该员工在当前物业没有有效任职记录，不能发起邀请。")
    return membership


def _action_invite(request, pk):
    """创建邀请（AUTH-17）：撤销旧邀请 + 预开通待激活账号 + 返回一次性链接。

    明文链接（/invite/accept?token=...）仅本次响应显示；文案固定
    "邀请链接已生成，请复制并人工发送"——不出现"发送成功"（§5.3）。
    """
    _invite_rate_guard(request)
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    membership = _get_membership_or_error(profile, property_obj)
    username = (request.POST.get("username") or "").strip()
    try:
        invitation, raw_token = EmployeeInvitationService.create_invitation(
            profile, membership, request.user, username=username,
            request=request)
    except InvitationError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "message": "邀请链接已生成，请复制并人工发送给本人；链接仅本次显示，"
                   f"有效期至 {timezone.localtime(invitation.expires_at):%Y-%m-%d %H:%M}",
        "invitation_id": invitation.pk,
        "invite_url": invite_accept_url(request, raw_token),
        "username": profile.user.username if profile.user else username,
        "expires_at": invitation.expires_at.isoformat(),
    })


def _action_invite_revoke(request, pk):
    """撤销邀请（AUTH-17）：原链接立即失效；仅待激活邀请可撤销。"""
    _invite_rate_guard(request)
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    invitation = (Invitation.objects.filter(
        staff_profile=profile, membership__property=property_obj,
        status=Invitation.Status.PENDING).order_by("-id").first())
    if invitation is None:
        raise ApiError("RESOURCE_NOT_FOUND", "该员工没有待激活的邀请，无需撤销。")
    try:
        EmployeeInvitationService.revoke(invitation, revoked_by=request.user,
                                         request=request)
    except InvitationError as exc:
        raise ApiError("BUSINESS_CONFLICT", str(exc))
    return api_response(request, {
        "message": "邀请已撤销，原邀请链接立即失效", "invitation_id": invitation.pk,
    })


def _action_invite_resend(request, pk):
    """重发邀请（AUTH-17）：旧 token 全部失效，生成新一次性链接（仅本次显示）。"""
    _invite_rate_guard(request)
    property_obj = getattr(request, "property", None)
    profile = _get_profile_or_error(pk, property_obj)
    invitation = (Invitation.objects.filter(
        staff_profile=profile, membership__property=property_obj)
        .exclude(status=Invitation.Status.ACCEPTED).order_by("-id").first())
    if invitation is None:
        raise ApiError("RESOURCE_NOT_FOUND", "该员工还没有邀请记录，请先创建邀请。")
    try:
        new_invitation, raw_token = EmployeeInvitationService.resend(
            invitation, resent_by=request.user, request=request)
    except InvitationError as exc:
        raise ApiError("VALIDATION_ERROR", str(exc))
    return api_response(request, {
        "message": "邀请链接已重新生成，请复制并人工发送给本人；原链接已全部失效，"
                   "新链接仅本次显示",
        "invitation_id": new_invitation.pk,
        "invite_url": invite_accept_url(request, raw_token),
        "expires_at": new_invitation.expires_at.isoformat(),
        "resend_count": new_invitation.resend_count,
    })


# ---------- 装饰器组装（门禁在 api_view 之外，保证 302/403 语义） ----------
# staff_required / pms_permission_required 来自 hotelapp.views 与 identity.decorators：
# 与旧 /staff_accounts/ 系列视图完全一致的门禁组合（§10.1 迁移等价性）。
from hotelapp.views import staff_required  # noqa: E402  统一 PMS 员工门禁

staff_center_page = staff_required(pms_permission_required("identity.staff_manage")(
    _staff_center_page))

staff_action_create = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_create)))

staff_action_provision = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_provision)))

staff_action_assign_role = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_assign_role)))

staff_action_end_assignment = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_end_assignment)))

staff_action_set_status = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_set_status)))

staff_action_reset_password = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_reset_password)))

staff_action_invite = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_invite)))

staff_action_invite_revoke = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_invite_revoke)))

staff_action_invite_resend = staff_required(pms_permission_required("identity.staff_manage")(
    api_view(_action_invite_resend)))
