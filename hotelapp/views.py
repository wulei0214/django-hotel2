"""
hotelapp 视图层：前台 8 大业务模块的全部页面与接口。

结构约定（所有模块一致，新增模块请照此模式）：
  show_xxx    展示页（GET）        权限：hotelapp.view_xxx
  insert_xxx  录入（POST）         权限：hotelapp.add_xxx
  update_xxx  行内修改（XHR POST） 权限：hotelapp.change_xxx
  delete_xxx  删除（GET）          权限：hotelapp.delete_xxx
  search_xxx  查询（POST）         权限：hotelapp.view_xxx

通用机制：
  * _first_error   逐字段格式校验，返回 (错误信息, 出错输入框name)，用于页面回填
  * _error_context 错误重渲染上下文：保留已填内容，仅清空出错字段
  * _search        查询字段白名单校验 + 按主键排序，杜绝注入
  * UNIQUE_FIELDS  唯一性字段注册表（身份证/手机号/菜名/车牌），供 check_unique 失焦查重
  * 视图统一挂 @staff_required（登录+员工身份）与 @permission_required（模块权限）
"""

import csv
import datetime
import functools
import logging
import re

from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import render, redirect, get_object_or_404

logger = logging.getLogger(__name__)

from .models import (Staff, Room, Client, Food, Vip, Park, Order, Accommodation,
                     FinanceRecord, NightAudit, Folio, ROOM_STATUS)
from . import events


# ---------- 模块启停注册表（RBAC 决策：餐饮/停车对接第三方，本阶段隐藏入口） ----------
# §7 跨 App RBAC 注册表（apps/identity/registry.py）自 Phase 2 起是模块启停的
# 唯一权威定义：food/order/park 三个旧门禁键映射到注册表的 fnb/parking 模块，
# 状态非 ACTIVE 即隐藏。此处仅保留旧的键值开关视图，module_gate 行为不变；
# 代码与历史数据全部保留，注册表把对应模块置 ACTIVE 即恢复页面。
from apps.identity.registry import legacy_module_gate_keys, module_gate_open

MODULE_REGISTRY = {key: module_gate_open(key)
                   for key in legacy_module_gate_keys()}


def module_gate(key):
    """隐藏模块统一门禁：命中则带提示跳回主界面（fail-closed，直接 URL 也不可达）。"""
    def deco(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if not MODULE_REGISTRY.get(key, True):
                messages.warning(request, "该模块已对接第三方系统，暂不开放；历史数据已保留。")
                return redirect("/home/")
            return view(request, *args, **kwargs)
        return wrapper
    return deco
from apps.identity.authorization import AuthorizationService
from apps.identity.decorators import pms_permission_required

from . import handlers  # noqa: F401  订阅器注册（事件 -> 后台联动）
from . import state_machine


def _must_change_password(user):
    """D-07：员工档案标记须强制改密（开通账号/被管理员重置密码后置位）。"""
    from apps.identity.models import StaffProfile
    return StaffProfile.objects.filter(
        user=user, must_change_password=True).exists()


def _has_active_employment(user):
    """RBAC 关联方案 §6：员工身份 = 档案在职 + DEFAULT 任职有效 + 角色授权有效。

    判定链（登录后每一请求执行，Fail Closed）：
      1) StaffProfile 指向该账号且在职；2) DEFAULT 任职 ACTIVE；
      3) 至少一条当前生效的 StaffRoleAssignment（无角色/过期 → STAFF-03/E-37 拒绝）。
    停职/离职/角色到期立即失权（AUTH-05），不依赖 User.groups 镜像或重新登录。
    D-04：is_staff 只控制 Django Admin 入口，不参与本判定。
    所有用户（含 superuser）进入 PMS 业务页面均须持有有效任职；
    superuser 仅在 Django Admin 侧拥有平台维护权限（V1.1 §12.2）。
    """
    from apps.identity.models import PropertyStaffMembership, StaffProfile
    memberships = PropertyStaffMembership.objects.filter(
        staff_profile__user=user,
        staff_profile__employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        status=PropertyStaffMembership.Status.ACTIVE,
        property__code=settings.DEFAULT_PROPERTY_CODE,
    )
    if not memberships.exists():
        return False
    now = timezone.now()
    from apps.identity.models import StaffRoleAssignment
    return StaffRoleAssignment.objects.filter(
        membership__in=memberships, starts_at__lte=now,
    ).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now)).exists()


def staff_required(view):
    """登录 + 员工身份校验；登录页统一用前台 /login/。

    V1.1 §12.2：所有用户（含 superuser）进入 PMS 业务页面均须持有
    有效酒店任职——账号停用/员工离职/任职暂停立即失权。
    superuser 仅拥有 Django Admin 平台维护权限，不自动获得 PMS 业务入口。
    """
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        # D-04 + V1.1 §12.2：PMS 员工门禁 = 登录 + 有效任职/角色授权；
        # superuser 不豁免（平台技术身份 ≠ 酒店经营角色，与 AuthorizationService 一致）；
        # is_staff 只管 Django Admin 入口。
        if user.is_authenticated and not _has_active_employment(user):
            raise PermissionDenied(
                "当前账号没有有效的酒店任职或岗位授权，请联系管理员分配")
        # D-07：首次登录/被重置密码后强制改密（改密页与登出自身放行）
        if (user.is_authenticated
                and _must_change_password(user)
                and request.path not in ("/force-password-change/", "/logout/")):
            return redirect("/force-password-change/")
        return view(request, *args, **kwargs)
    return login_required(wrapped, login_url='/login/')


# 各模块查询表单允许使用的字段白名单，用户提交的字段名不在白名单内则忽略查询
STAFF_SEARCH_FIELDS = ("员工id", "姓名", "职务", "月薪", "工龄")
CLIENT_SEARCH_FIELDS = ("顾客id", "姓名", "身份证号")
FOOD_SEARCH_FIELDS = ("菜品编号", "菜名", "价格", "厨师id")
VIP_SEARCH_FIELDS = ("会员编号", "姓名", "会员等级")
PARK_SEARCH_FIELDS = ("车位编号", "当前状态", "车辆牌号")
ROOM_SEARCH_FIELDS = ("房间号", "客房类型", "价格", "负责人编号")

# 字段格式白名单：编号类 1-10 位数字；姓名 2-10 位中英文；
# 金额最多 6 位整数加 2 位小数；身份证 18 位；手机号 11 位；车牌为省份汉字开头
VALIDATORS = {
    "id10": (re.compile(r"^\d{1,10}$"), "必须是1-10位数字"),
    "digits4": (re.compile(r"^\d{1,4}$"), "必须是1-4位数字"),
    "price5": (re.compile(r"^\d{1,5}$"), "必须是1-5位整数"),
    "name": (re.compile(r"^[\u4e00-\u9fa5A-Za-z·]{2,10}$"), "必须是2-10位中英文"),
    "name20": (re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9·]{2,20}$"), "必须是2-20位中英文或数字"),
    "type10": (re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9]{2,10}$"), "必须是2-10位中英文或数字"),
    "money": (re.compile(r"^(0|[1-9]\d{0,5})(\.\d{1,2})?$"), "必须是数字，整数最多6位、最多两位小数"),
    "idcard": (re.compile(r"^\d{17}[\dXx]$"), "必须是18位身份证号（末位可为X）"),
    "tel": (re.compile(r"^1\d{10}$"), "必须是11位手机号（1开头）"),
    "plate": (re.compile(r"^[\u4e00-\u9fa5][A-Z][A-Z0-9]{4,6}$"), "格式必须正确，如 黑A12345"),
    "date": (re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$"), "必须是日期，格式如 2026-01-01"),
    "job": (re.compile(r"^(总经理|大堂经理|厨师|服务员|保安)$"), "必须是规范职务名称"),
    "viprank": (re.compile(r"^(普通会员|高级会员)$"), "必须是普通会员或高级会员"),
}

STAFF_JOBS = ("总经理", "大堂经理", "厨师", "服务员", "保安")

# 具有唯一性业务含义的字段（失焦即时查重接口使用）：同一证件/手机号/菜名/车牌全局唯一
UNIQUE_FIELDS = {
    "client": ("身份证号", Client),
    "vip": ("联系方式", Vip),
    "food": ("菜名", Food),
    "park": ("车辆牌号", Park),
}


def _first_error(checks):
    """依次校验字段列表，返回 (第一条错误信息, 出错字段的输入name)。
    checks 每项: (值, 规则, 标签, 输入name[, 是否必填，默认必填])；全部通过返回 (None, None)。"""
    for item in checks:
        value, rule, label, field = item[0], item[1], item[2], item[3]
        required = item[4] if len(item) > 4 else True
        if not (value or "").strip():
            if required:
                return f"{label}不能为空", field
            continue
        pattern, msg = VALIDATORS[rule]
        if not pattern.match(value.strip()):
            return f"{label}{msg}", field
    return None, None


def _error_context(request, efield):
    """构造错误重渲染上下文：保留用户已填内容，仅清空出错字段的输入框"""
    form = request.POST.copy()
    if efield:
        form[efield] = ""
    return {"error": None, "form": form}


@staff_required
def check_unique(request):
    """唯一字段查重接口：/check_unique/?model=client&value=xxx（结果缓存 60 秒）"""
    info = UNIQUE_FIELDS.get(request.GET.get("model"))
    value = (request.GET.get("value") or "").strip()
    if not info or not value:
        return JsonResponse({"exists": False})
    field, model = info
    key = f"uq:{model.__name__}:{field}:{value}"
    # 查重缓存仅是性能优化，不能成为唯一性判断的可用性依赖。
    # Redis 读取/写入故障时退化为数据库查询；数据库仍是权威来源。
    try:
        exists = cache.get(key)
    except Exception:
        logger.warning("check_unique cache read failed; falling back to DB",
                       exc_info=True, extra={"cache_key": key})
        exists = None
    if exists is None:
        exists = model.objects.filter(**{field: value}).exists()
        try:
            cache.set(key, exists, 60)
        except Exception:
            logger.warning("check_unique cache write failed; returning DB result",
                           exc_info=True, extra={"cache_key": key})
    return JsonResponse({"exists": exists})


def _get_staff(sid, job):
    """按工号取出指定职务的员工，查不到或编号非法时返回 None"""
    try:
        return Staff.objects.get(员工id=sid, 职务=job)
    except (Staff.DoesNotExist, ValueError, TypeError):
        return None


def _search(model, pk, fields, request):
    """按白名单字段执行安全查询；返回 (结果列表, 是否执行了查询)，结果按主键排序"""
    if request.method == "POST":
        columns = request.POST.get("columns")
        value = (request.POST.get("value") or "").strip()
        if columns in fields and value:
            try:
                return list(model.objects.filter(**{columns: value}).order_by(pk)), True
            except ValueError:
                return [], True
    return [], False


# ---------- 员工信息管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_staff')
def index(request):
    """员工管理列表（图5）：筛选条件 + 可排序表格 + 分页（15 条/页）"""
    order = request.GET.get("order", "asc")
    order_field = "员工id" if order != "desc" else "-员工id"
    status = request.GET.get("status")
    queryset = Staff.objects.order_by(order_field)
    if status in ("on", "off"):
        queryset = queryset.filter(启用=(status == "on"))
    paginator = Paginator(queryset, 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "index.html", {
        "page_obj": page, "people_list": page.object_list, "order": order,
        "status_filter": status or "",
        "can_view_salary": _has_role(request.user, "财务", "店长"),
        "can_manage_salary": _has_role(request.user, "店长"),
    })


@staff_required
@pms_permission_required('hotelapp.change_staff')
def staff_toggle(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """启用/停用员工（图5 是否启用列的开关）"""
    if request.method != "POST":
        return HttpResponse("非法请求", status=400)
    sid = request.POST.get("id")
    staff = Staff.objects.filter(员工id=sid).first()
    if staff is None:
        return HttpResponse("员工不存在", status=400)
    staff.启用 = not staff.启用
    staff.save()
    return JsonResponse({"ok": True, "enabled": staff.启用})


@staff_required
def export_staff(request):
    """旧版敏感员工 CSV 通道停用；完整薪酬导出必须走双人审批。"""
    return HttpResponse(
        "旧版薪酬导出已关闭。请先申请敏感导出审批，再由另一位经理账号批准并下载。",
        status=410,
    )


@staff_required
def request_staff_export(request):
    """申请员工薪酬敏感导出；必须填写详细理由并由另一位经理审批。"""
    if request.method != "POST":
        return HttpResponse("请使用 POST 提交导出申请。", status=405)
    if not _has_permission(request, "hotelapp.export_pii"):
        return HttpResponse("当前岗位没有敏感数据导出申请权限。", status=403)
    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 8 or len(reason) > 240:
        return HttpResponse("敏感导出必须填写 8 至 240 个字符的详细用途/原因。", status=400)
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return HttpResponse("当前物业上下文不可用。", status=503)
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key or len(key) > 64:
        return HttpResponse("必须提供 1 至 64 个字符的 Idempotency-Key。", status=400)
    import hashlib
    entity_id = hashlib.sha256(
        f"{property_obj.pk}:{request.user.pk}:staff-export:{key}".encode("utf-8")
    ).hexdigest()
    from apps.billing.services_v2 import ApprovalService
    try:
        approval, created = ApprovalService.request_approval(
            property_obj=property_obj, action_code="privacy.export_staff",
            entity_type="StaffExport", entity_id=entity_id,
            requested_by=request.user, reason=reason,
            payload={"module": "staff", "contains_salary": True,
                     "format": "csv", "requested_by": request.user.pk})
    except ValueError as exc:
        return HttpResponse(str(exc), status=400)
    return JsonResponse({
        "approval_id": approval.pk, "status": approval.status,
        "created": created,
        "message": "已提交敏感导出审批；审批人必须是另一位值班经理或店长。",
    }, status=201 if created else 200, json_dumps_params={"ensure_ascii": False})


@staff_required
def approve_staff_export(request, approval_id):
    """另一位经理审批并下载薪酬 CSV；审批动作与下载绑定为一次 POST。"""
    if request.method != "POST":
        return HttpResponse("请使用 POST 执行审批和下载。", status=405)
    # F-06：审批判定改走 AuthorizationService（hotelapp.export_approve，矩阵可管理）
    from apps.identity.authorization import AuthorizationService
    property_obj = getattr(request, "property", None)
    if property_obj is None or not AuthorizationService.has_permission(
            request.user, property_obj, "hotelapp.export_approve"):
        return HttpResponse("只有另一位值班经理或店长可以审批薪酬导出。", status=403)
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return HttpResponse("当前物业上下文不可用。", status=503)
    from apps.billing.models import ApprovalRecord
    from apps.billing.services_v2 import ApprovalService
    approval = ApprovalRecord.objects.filter(
        pk=approval_id, property=property_obj,
        action_code="privacy.export_staff", entity_type="StaffExport",
    ).first()
    if approval is None:
        return HttpResponse("导出审批不存在或不属于当前物业。", status=404)
    try:
        approval = ApprovalService.approve(
            approval.pk, approved_by=request.user, property_obj=property_obj)
    except ValueError as exc:
        return HttpResponse(str(exc), status=409)
    if approval.status != ApprovalRecord.Status.APPROVED:
        return HttpResponse("导出审批已过期或未获批准。", status=409)
    rows = list(Staff.objects.order_by("员工id"))
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = 'attachment; filename="staff.csv"'
    writer = csv.writer(response)
    writer.writerow(["员工编号", "姓名", "职务", "月薪", "工龄", "是否启用"])
    for s in rows:
        writer.writerow([s.员工id, s.姓名, s.职务, s.月薪, s.工龄,
                         "启用" if s.启用 else "停用"])
    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="privacy.export_staff", entity_type="StaffExport",
        entity_id=approval.entity_id, actor=request.user,
        property_obj=property_obj,
        after={"approval_id": approval.pk, "row_count": len(rows)},
        reason=approval.reason, request=request)
    return response


@staff_required
@pms_permission_required('hotelapp.add_staff')
def insert(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        if not _has_role(request.user, "店长"):
            return HttpResponse("员工薪酬初始录入仅允许店长操作。", status=403)
        salary_reason = (request.POST.get("salary_reason") or "").strip()
        if len(salary_reason) < 8 or len(salary_reason) > 240:
            return HttpResponse("录入薪酬必须填写 8 至 240 个字符的业务原因。", status=400)
        sid = request.POST.get("id")
        name = request.POST.get("name")
        job = request.POST.get("job")
        salary = request.POST.get("salary")
        work_time = request.POST.get("time_work")
        error, efield = _first_error([
            (sid, "id10", "工号", "id", False),
            (name, "name", "姓名", "name"),
            (job, "job", "职务", "job"),
            (salary, "money", "月薪", "salary"),
            (work_time, "digits4", "工龄", "time_work"),
        ])
        if error is None:
            kwargs = {"姓名": name.strip(), "职务": job, "月薪": salary, "工龄": work_time}
            try:
                if sid:
                    kwargs["员工id"] = sid
                created_staff = Staff.objects.create(**kwargs)
                from apps.core.models import AuditEvent
                AuditEvent.record(
                    action="staff.salary_initial_set", entity_type="LegacyStaff",
                    entity_id=created_staff.员工id, actor=request.user,
                    property_obj=getattr(request, "property", None),
                    after={"salary_set": True},
                    reason=salary_reason, request=request)
            except (ValueError, IntegrityError):
                error, efield = "保存失败：工号必须是数字且不能重复", "id"
            else:
                messages.success(request, "员工信息添加成功")
                return redirect("/index/")
    ctx = _error_context(request, efield)
    ctx.update({
        "people_list": Staff.objects.order_by("员工id"), "error": error,
        "can_view_salary": _has_role(request.user, "财务", "店长"),
        "can_manage_salary": _has_role(request.user, "店长"),
    })
    return render(request, "index.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_staff')
def delete_staff(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if request.method == "GET":
        try:
            Staff.objects.filter(员工id=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该员工已被菜品、客房或订单信息引用", status=400)
    return render(request, "index.html", {"people_list": Staff.objects.order_by("员工id")})


@staff_required
@pms_permission_required('hotelapp.view_staff')
def search_staff(request):
    can_view_salary = _has_role(request.user, "财务", "店长")
    searchable_fields = list(STAFF_SEARCH_FIELDS)
    if not can_view_salary:
        searchable_fields = [field for field in searchable_fields if field != "月薪"]
    rows, searched = _search(Staff, "员工id", searchable_fields, request)
    return render(request, "index.html", {
        "people_list": Staff.objects.order_by("员工id"),
        "staffs": rows,
        "searched": searched,
        "can_view_salary": can_view_salary,
        "can_manage_salary": _has_role(request.user, "店长"),
    })


@staff_required
@pms_permission_required('hotelapp.change_staff')
def update_staff(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method != "POST":
        return HttpResponse("员工薪酬修改必须使用 POST。", status=405)
    if not _has_role(request.user, "店长"):
        return HttpResponse("员工薪酬及人事字段修改仅允许店长操作；财务岗位只读。", status=403)
    sid = request.POST.get("id")
    job = request.POST.get("job")
    salary = request.POST.get("salary")
    work_time = request.POST.get("time")
    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 8 or len(reason) > 240:
        return HttpResponse("修改员工薪酬必须填写 8 至 240 个字符的业务原因。", status=400)
    error, _ = _first_error([
        (sid, "id10", "工号", "id"),
        (job, "job", "职务", "job"),
        (salary, "money", "月薪", "salary"),
        (work_time, "digits4", "工龄", "time"),
    ])
    if error:
        return HttpResponse(error, status=400)
    try:
        with transaction.atomic():
            staff = Staff.objects.select_for_update().filter(员工id=sid).first()
            if staff is None:
                return HttpResponse("修改失败：员工不存在", status=404)
            before = {
                "job": staff.职务,
                "work_years": staff.工龄,
                "salary_set": bool(staff.月薪),
            }
            salary_changed = str(staff.月薪 or "") != str(salary or "")
            staff.职务 = job
            staff.月薪 = salary
            staff.工龄 = work_time
            staff.save(update_fields=("职务", "月薪", "工龄"))
            from apps.core.models import AuditEvent
            AuditEvent.record(
                action="staff.profile_update", entity_type="LegacyStaff",
                entity_id=staff.员工id, actor=request.user,
                property_obj=getattr(request, "property", None),
                before=before,
                after={"job": staff.职务, "work_years": staff.工龄,
                       "salary_changed": salary_changed},
                reason=reason, request=request)
    except ValueError as exc:
        return HttpResponse(f"修改失败：{exc}", status=400)
    return JsonResponse({"ok": True, "salary_changed": salary_changed})


# ---------- 顾客信息管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_client')
def show_client(request):
    """顾客信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Client.objects.order_by("顾客id"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "html1.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@pms_permission_required('hotelapp.add_client')
def insert_client(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        cid = request.POST.get("id")
        name = request.POST.get("name")
        identify = request.POST.get("identify")
        error, efield = _first_error([
            (cid, "id10", "顾客编号", "id", False),
            (name, "name", "姓名", "name"),
            (identify, "idcard", "身份证号", "identify"),
        ])
        if error is None and Client.objects.filter(身份证号=identify.strip()).exists():
            error, efield = "保存失败：该身份证号已登记，证件号不能重复", "identify"
        if error is None:
            kwargs = {"姓名": name.strip(), "身份证号": identify.strip()}
            try:
                if cid:
                    kwargs["顾客id"] = cid
                Client.objects.create(**kwargs)
            except (ValueError, IntegrityError):
                error, efield = "保存失败：顾客编号必须是数字且不能重复", "id"
            else:
                messages.success(request, "顾客信息添加成功")
                return redirect("/show_client/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Client.objects.order_by("顾客id"), "error": error})
    return render(request, "html1.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_client')
def delete_client(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if request.method == "GET":
        try:
            Client.objects.filter(顾客id=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该顾客已被餐饮订单或客房订单引用", status=400)
    return render(request, "html1.html", {"people_list": Client.objects.order_by("顾客id")})


@staff_required
@pms_permission_required('hotelapp.change_client')
def update_client(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method == "POST":
        cid = request.POST.get("id")
        name = request.POST.get("name")
        identify = request.POST.get("identify")
        error, _ = _first_error([
            (cid, "id10", "顾客编号", "id"),
            (name, "name", "姓名", "name"),
            (identify, "idcard", "身份证号", "identify"),
        ])
        if error:
            return HttpResponse(error, status=400)
        if Client.objects.filter(身份证号=identify).exclude(顾客id=cid).exists():
            return HttpResponse("修改失败：该身份证号已登记，证件号不能重复", status=400)
        try:
            updated = Client.objects.filter(顾客id=cid).update(姓名=name, 身份证号=identify)
        except ValueError:
            return HttpResponse("修改失败：顾客编号非法", status=400)
        if not updated:
            return HttpResponse("修改失败：顾客不存在", status=400)
    return render(request, "html1.html", {"people_list": Client.objects.order_by("顾客id")})


@staff_required
@pms_permission_required('hotelapp.view_client')
def search_client(request):
    """顾客信息查询：结果直接作为列表数据源（searched 标记驱动提示）"""
    rows, searched = _search(Client, "顾客id", CLIENT_SEARCH_FIELDS, request)
    people = rows if searched else Client.objects.order_by("顾客id")
    return render(request, "html1.html", {
        "people_list": people,
        "searched": searched,
    })


# ---------- 餐厅信息管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_food')
@module_gate("food")
def show_food(request):
    """菜品信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Food.objects.order_by("菜品编号"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "html4.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@pms_permission_required('hotelapp.add_food')
@module_gate("food")
def insert_food(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        fid = request.POST.get("id")
        name = request.POST.get("name")
        price = request.POST.get("price")
        cooker = request.POST.get("cooker")
        error, efield = _first_error([
            (fid, "id10", "菜品编号", "id"),
            (name, "name20", "菜名", "name"),
            (price, "money", "价格", "price"),
        ])
        if error is None and Food.objects.filter(菜名=name.strip()).exists():
            error, efield = "保存失败：该菜名已存在，菜名不能重复", "name"
        if error is None:
            cook = _get_staff(cooker, "厨师")
            if cook is None:
                error, efield = "保存失败：厨师编号必须存在（职务为厨师）", "cooker"
            else:
                try:
                    Food.objects.create(菜品编号=fid, 菜名=name.strip(), 价格=price, 厨师id=cook)
                except (ValueError, IntegrityError):
                    error, efield = "保存失败：菜品编号必须是数字且不能重复", "id"
                else:
                    messages.success(request, "菜品信息添加成功")
                    return redirect("/show_food/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Food.objects.order_by("菜品编号"), "error": error})
    return render(request, "html4.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_food')
@module_gate("food")
def delete_food(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if request.method == "GET":
        try:
            Food.objects.filter(菜品编号=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该菜品已被餐饮订单引用", status=400)
    return render(request, "html4.html", {"people_list": Food.objects.order_by("菜品编号")})


@staff_required
@pms_permission_required('hotelapp.change_food')
@module_gate("food")
def update_food(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method == "POST":
        fid = request.POST.get("id")
        price = request.POST.get("price")
        cooker = request.POST.get("cooker")
        error, _ = _first_error([
            (fid, "id10", "菜品编号", "id"),
            (price, "money", "价格", "price"),
        ])
        if error:
            return HttpResponse(error, status=400)
        cook = _get_staff(cooker, "厨师")
        if cook is None:
            return HttpResponse("修改失败：厨师编号必须存在（职务为厨师）", status=400)
        try:
            updated = Food.objects.filter(菜品编号=fid).update(价格=price, 厨师id=cook)
        except ValueError:
            return HttpResponse("修改失败：菜品编号非法", status=400)
        if not updated:
            return HttpResponse("修改失败：菜品不存在", status=400)
    return render(request, "html4.html", {"people_list": Food.objects.order_by("菜品编号")})


@staff_required
@pms_permission_required('hotelapp.view_food')
@module_gate("food")
def search_food(request):
    """菜品信息查询：结果直接作为列表数据源（searched 标记驱动提示）"""
    rows, searched = _search(Food, "菜品编号", FOOD_SEARCH_FIELDS, request)
    people = rows if searched else Food.objects.order_by("菜品编号")
    return render(request, "html4.html", {
        "people_list": people,
        "searched": searched,
    })


# ---------- 会员信息管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_vip')
def show_vip(request):
    """会员信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Vip.objects.order_by("会员编号"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "vip.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@pms_permission_required('hotelapp.add_vip')
def insert_vip(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        vid = request.POST.get("id")
        name = request.POST.get("name")
        rank = request.POST.get("rank")
        tel = request.POST.get("tel")
        error, efield = _first_error([
            (vid, "id10", "会员编号", "id", False),
            (name, "name", "姓名", "name"),
            (rank, "viprank", "会员等级", "rank"),
            (tel, "tel", "联系方式", "tel"),
        ])
        if error is None and tel and Vip.objects.filter(联系方式=tel.strip()).exists():
            error, efield = "保存失败：该联系方式已被其他会员使用，手机号不能重复", "tel"
        if error is None:
            kwargs = {"姓名": name.strip(), "会员等级": rank, "联系方式": tel}
            try:
                if vid:
                    kwargs["会员编号"] = vid
                Vip.objects.create(**kwargs)
            except (ValueError, IntegrityError):
                error, efield = "保存失败：会员编号必须是数字且不能重复", "id"
            else:
                messages.success(request, "会员信息添加成功")
                return redirect("/show_vip/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Vip.objects.order_by("会员编号"), "error": error})
    return render(request, "vip.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_vip')
def delete_vip(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if request.method == "GET":
        try:
            Vip.objects.filter(会员编号=request.GET.get("id")).delete()
        except ValueError:
            return HttpResponse("删除失败：会员编号非法", status=400)
    return render(request, "vip.html", {"people_list": Vip.objects.order_by("会员编号")})


@staff_required
@pms_permission_required('hotelapp.change_vip')
def update_vip(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method == "POST":
        vid = request.POST.get("id")
        rank = request.POST.get("rank")
        tel = request.POST.get("tel")
        error, _ = _first_error([
            (vid, "id10", "会员编号", "id"),
            (rank, "viprank", "会员等级", "rank"),
            (tel, "tel", "联系方式", "tel"),
        ])
        if error:
            return HttpResponse(error, status=400)
        if tel and Vip.objects.filter(联系方式=tel).exclude(会员编号=vid).exists():
            return HttpResponse("修改失败：该联系方式已被其他会员使用，手机号不能重复", status=400)
        try:
            updated = Vip.objects.filter(会员编号=vid).update(会员等级=rank, 联系方式=tel)
        except ValueError:
            return HttpResponse("修改失败：会员编号非法", status=400)
        if not updated:
            return HttpResponse("修改失败：会员不存在", status=400)
    return render(request, "vip.html", {"people_list": Vip.objects.order_by("会员编号")})


@staff_required
@pms_permission_required('hotelapp.view_vip')
def search_vip(request):
    """会员信息查询：结果直接作为列表数据源（searched 标记驱动提示）"""
    rows, searched = _search(Vip, "会员编号", VIP_SEARCH_FIELDS, request)
    people = rows if searched else Vip.objects.order_by("会员编号")
    return render(request, "vip.html", {
        "people_list": people,
        "searched": searched,
    })


# ---------- 停车场信息管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_park')
@module_gate("park")
def show_park(request):
    """车位信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Park.objects.order_by("车位编号"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "park.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@pms_permission_required('hotelapp.add_park')
@module_gate("park")
def insert_park(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        pid = request.POST.get("id")
        status = request.POST.get("status")
        num = request.POST.get("num")
        error, efield = _first_error([
            (pid, "id10", "车位编号", "id"),
            (num, "plate", "车辆牌号", "num", False),
        ])
        if error is None:
            if status not in ("空闲", "有车"):
                error, efield = "当前状态必须是空闲或有车", "status"
            elif num and Park.objects.filter(车辆牌号=num.strip()).exists():
                error, efield = "保存失败：该车牌已停入其他车位，车牌不能重复", "num"
            else:
                try:
                    Park.objects.create(车位编号=pid, 当前状态=status, 车辆牌号=(num.strip() or None))
                except (ValueError, IntegrityError):
                    error, efield = "保存失败：车位编号必须是数字且不能重复", "id"
                else:
                    messages.success(request, "车位信息添加成功")
                    return redirect("/show_park/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Park.objects.order_by("车位编号"), "error": error})
    return render(request, "park.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_park')
@module_gate("park")
def delete_park(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if request.method == "GET":
        try:
            Park.objects.filter(车位编号=request.GET.get("id")).delete()
        except ValueError:
            return HttpResponse("删除失败：车位编号非法", status=400)
    return render(request, "park.html", {"people_list": Park.objects.order_by("车位编号")})


@staff_required
@pms_permission_required('hotelapp.change_park')
@module_gate("park")
def update_park(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method == "POST":
        pid = request.POST.get("id")
        status = request.POST.get("status")
        num = request.POST.get("num")
        error, _ = _first_error([
            (pid, "id10", "车位编号", "id"),
            (num, "plate", "车辆牌号", "num", False),
        ])
        if error:
            return HttpResponse(error, status=400)
        if status not in ("空闲", "有车"):
            return HttpResponse("修改失败：当前状态必须是空闲或有车", status=400)
        if num and Park.objects.filter(车辆牌号=num.strip()).exclude(车位编号=pid).exists():
            return HttpResponse("修改失败：该车牌已停入其他车位，车牌不能重复", status=400)
        try:
            updated = Park.objects.filter(车位编号=pid).update(当前状态=status, 车辆牌号=(num.strip() or None))
        except ValueError:
            return HttpResponse("修改失败：车位编号非法", status=400)
        if not updated:
            return HttpResponse("修改失败：车位不存在", status=400)
    return render(request, "park.html", {"people_list": Park.objects.order_by("车位编号")})


@staff_required
@pms_permission_required('hotelapp.view_park')
@module_gate("park")
def search_park(request):
    """车位信息查询：结果直接作为列表数据源（searched 标记驱动提示）"""
    rows, searched = _search(Park, "车位编号", PARK_SEARCH_FIELDS, request)
    people = rows if searched else Park.objects.order_by("车位编号")
    return render(request, "park.html", {
        "people_list": people,
        "searched": searched,
    })


# ---------- 客房信息管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_room')
def show_room(request):
    """旧客房管理入口：保留原权限校验，统一跳转 PMS2 房态与库存中心。"""
    return redirect("/manage/rooms/")


@staff_required
@pms_permission_required('hotelapp.add_room')
def insert_room(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        rid = request.POST.get("id")
        room_type = request.POST.get("type")
        price = request.POST.get("price")
        waiter = request.POST.get("waiter")
        error, efield = _first_error([
            (rid, "id10", "房间号", "id"),
            (room_type, "type10", "客房类型", "type"),
            (price, "price5", "价格", "price"),
        ])
        if error is None:
            staff = _get_staff(waiter, "服务员")
            if staff is None:
                error, efield = "保存失败：服务员编号必须存在（职务为服务员）", "waiter"
            else:
                try:
                    Room.objects.create(房间号=rid, 客房类型=room_type.strip(), 价格=price, 负责人编号=staff)
                except (ValueError, IntegrityError):
                    error, efield = "保存失败：房间号必须是数字且不能重复", "id"
                else:
                    messages.success(request, "客房信息添加成功")
                    return redirect("/show_room/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Room.objects.order_by("房间号"), "error": error})
    return render(request, "room.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_room')
def delete_room(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if request.method == "GET":
        try:
            Room.objects.filter(房间号=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该客房已被客房订单引用", status=400)
    return render(request, "room.html", {"people_list": Room.objects.order_by("房间号")})


@staff_required
@pms_permission_required('hotelapp.change_room')
def update_room(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    if request.method == "POST":
        rid = request.POST.get("id")
        room_type = request.POST.get("type")
        price = request.POST.get("price")
        error, _ = _first_error([
            (rid, "id10", "房间号", "id"),
            (room_type, "type10", "客房类型", "type"),
            (price, "price5", "价格", "price"),
        ])
        if error:
            return HttpResponse(error, status=400)
        update_kwargs = {"客房类型": room_type, "价格": price}
        waiter = request.POST.get("waiter")
        if waiter:
            staff = _get_staff(waiter, "服务员")
            if staff is None:
                return HttpResponse("修改失败：服务员编号必须存在（职务为服务员）", status=400)
            update_kwargs["负责人编号"] = staff
        try:
            updated = Room.objects.filter(房间号=rid).update(**update_kwargs)
        except ValueError:
            return HttpResponse("修改失败：房间号和价格必须是数字", status=400)
        if not updated:
            return HttpResponse("修改失败：客房不存在", status=400)
    return render(request, "room.html", {"people_list": Room.objects.order_by("房间号")})


@staff_required
@pms_permission_required('hotelapp.view_room')
def search_room(request):
    """客房信息查询：结果直接作为列表数据源（searched 标记驱动提示）"""
    rows, searched = _search(Room, "房间号", ROOM_SEARCH_FIELDS, request)
    people = rows if searched else Room.objects.order_by("房间号")
    return render(request, "room.html", {
        "people_list": people,
        "searched": searched,
    })


# ---------- 餐饮订单管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_order')
@module_gate("order")
def show_order(request):
    """餐饮订单管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Order.objects.order_by("id"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "order.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@pms_permission_required('hotelapp.add_order')
@module_gate("order")
def insert_order(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        oid = request.POST.get("id")
        client = request.POST.get("client")
        food = request.POST.get("food")
        waiter = request.POST.get("waiter")
        table = request.POST.get("table")
        error, efield = _first_error([
            (oid, "id10", "订单编号", "id"),
            (client, "id10", "顾客编号", "client"),
            (food, "id10", "菜品编号", "food"),
            (waiter, "id10", "服务员编号", "waiter"),
            (table, "digits4", "桌号", "table"),
        ])
        if error is None:
            try:
                cli = Client.objects.get(顾客id=client)
                foodid = Food.objects.get(菜品编号=food)
            except (Client.DoesNotExist, Food.DoesNotExist, ValueError, TypeError):
                cli, foodid = None, None
            staff = _get_staff(waiter, "服务员")
            if cli is None:
                error, efield = "保存失败：顾客编号必须存在", "client"
            elif foodid is None:
                error, efield = "保存失败：菜品编号必须存在", "food"
            elif staff is None:
                error, efield = "保存失败：服务员编号必须存在（职务为服务员）", "waiter"
            else:
                try:
                    Order.objects.create(id=oid, 顾客编号=cli, 菜品编号=foodid, 服务员编号=staff, 桌号=table)
                except (ValueError, IntegrityError):
                    error, efield = "保存失败：订单编号必须是数字且不能重复", "id"
                else:
                    messages.success(request, "餐饮订单添加成功")
                    return redirect("/show_order/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Order.objects.order_by("id"), "error": error})
    return render(request, "order.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_order')
@module_gate("order")
def delete_order(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """餐饮订单「冲账」：打已冲账标记并记录冲账人/时间，不再物理删除（值班经理权限）"""
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if request.method == "GET":
        try:
            item = Order.objects.get(id=request.GET.get("id"))
        except (Order.DoesNotExist, ValueError):
            return HttpResponse("订单不存在", status=400)
        if item.状态 == "已冲账":
            return HttpResponse("该订单已冲账", status=400)
        reason = (request.GET.get("reason") or "").strip()
        item.状态 = "已冲账"
        item.冲账人 = request.user.username
        item.冲账时间 = _today()
        item.save()
        import logging
        logging.getLogger("hotel.audit").info("冲账：餐饮订单%s 原因[%s]，操作人 %s",
                                              item.id, reason or "未填写", request.user.username)
    return render(request, "order.html", {"people_list": Order.objects.order_by("id")})


@staff_required
@pms_permission_required('hotelapp.view_order')
@module_gate("order")
def search_order(request):
    food_list = []
    searched = False
    if request.method == "POST":
        cid = (request.POST.get("id") or "").strip()
        if cid:
            searched = True
            try:
                for item in Order.objects.filter(顾客编号=cid):
                    food_list.append({"菜名": item.菜品编号.菜名, "价格": item.菜品编号.价格})
            except ValueError:
                food_list = []
    return render(request, "order.html", {"people_list": Order.objects.order_by("id"), "food_list": food_list, "searched": searched})


# ---------- 客房订单管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_accommodation')
def show_accomodation(request):
    """客房订单管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Accommodation.objects.order_by("id"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "accomodation.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@pms_permission_required('hotelapp.add_accommodation')
def insert_accomodation(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    error, efield = None, None
    if request.method == "POST":
        aid = request.POST.get("id")
        client = request.POST.get("client")
        room = request.POST.get("room")
        time = request.POST.get("time")
        error, efield = _first_error([
            (aid, "id10", "订单编号", "id"),
            (client, "id10", "顾客编号", "client"),
            (room, "id10", "客房编号", "room"),
            (time, "date", "入住时间", "time"),
        ])
        if error is None:
            try:
                datetime.datetime.strptime(time, "%Y-%m-%d")
            except ValueError:
                error, efield = "入住时间不是有效日期", "time"
        if error is None:
            try:
                cli = Client.objects.get(顾客id=client)
                roomid = Room.objects.get(房间号=room)
            except (Client.DoesNotExist, Room.DoesNotExist, ValueError, TypeError):
                cli, roomid = None, None
            if cli is None:
                error, efield = "保存失败：顾客编号必须存在", "client"
            elif roomid is None:
                error, efield = "保存失败：客房编号必须存在", "room"
            else:
                try:
                    Accommodation.objects.create(id=aid, 顾客id=cli, 客房编号=roomid, 入住时间=time,
                                                 状态="已入住", 联系电话=(request.POST.get("tel") or "").strip())
                except (ValueError, IntegrityError):
                    error, efield = "保存失败：订单编号必须是数字且不能重复", "id"
                else:
                    roomid.房间状态 = "已入住"
                    roomid.save()
                    messages.success(request, "客房订单添加成功（已入住）")
                    return redirect("/show_accomodation/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Accommodation.objects.order_by("id"), "error": error})
    return render(request, "accomodation.html", ctx)


@staff_required
@pms_permission_required('hotelapp.delete_accommodation')
def delete_accomodation(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """客房订单「冲账」：打已冲账标记、释放房间、记录冲账人/时间，不再物理删除"""
    if request.method != "POST":
        return HttpResponse("写操作必须使用 POST（P0 安全护栏）", status=405)
    if True:  # 业务逻辑统一 POST 处理
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            return HttpResponse("冲账必须填写原因（高危操作需留痕）", status=400)
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=request.POST.get("id"))
                if item.状态 == "已冲账":
                    return HttpResponse("该订单已冲账", status=400)
                room = item.客房编号
                # 房态校验：房间已被新客人入住时禁止冲账
                if Accommodation.objects.filter(客房编号=room, 状态="已入住").exclude(id=item.id).exists():
                    return HttpResponse("该房间已被新客人入住，禁止冲账，请联系财务走历史账务调整", status=400)
                # 夜审校验：退房后已经历夜审结转则禁止前台反悔
                if item.退房时间 and NightAudit.objects.filter(营业日期__gte=item.退房时间).exists():
                    return HttpResponse("该订单已经过夜审结转，请走财务历史账务调整流程", status=400)
                prev = item.状态 or "待确认"
                if prev != "已退房":
                    state_machine.require_order_transition(item, "已冲账")
                else:
                    item.状态 = "已冲账"
                item.冲账人 = request.user.username
                item.冲账时间 = _today()
                item.save()
                # 红字凭证平账：反向生成负数收款流水，不修改原账
                collected = 0.0
                for f in Folio.objects.filter(order=item, 类型="收款"):
                    try:
                        collected += float(f.金额)
                    except (TypeError, ValueError):
                        pass
                if collected:
                    Folio.objects.create(order=item, 类型="冲账", 金额=f"-{collected:.2f}",
                                         事由=f"冲账红字平账：{reason}", 日期=_today(),
                                         经手人=request.user.username)
                if prev == "已入住":
                    room.房间状态 = "待清洁"
                elif prev in ("待确认", "已确认") and room.房间状态 == "已预订":
                    room.房间状态 = "空闲"
                room.save()
                import logging
                logging.getLogger("hotel.audit").info(
                    "冲账：订单%s 原因[%s] 红字 %.2f 元，操作人 %s",
                    item.id, reason, collected, request.user.username)
        except ValueError as e:
            return HttpResponse(str(e), status=400)
    return render(request, "accomodation.html", {"people_list": Accommodation.objects.order_by("id")})


@staff_required
@pms_permission_required('hotelapp.view_accommodation')
def search_accomodation(request):
    room_list = []
    searched = False
    if request.method == "POST":
        cid = (request.POST.get("id") or "").strip()
        if cid:
            searched = True
            try:
                for item in Accommodation.objects.filter(顾客id=cid):
                    room = item.客房编号
                    room_list.append({
                        "房间号": room.房间号,
                        "客房类型": room.客房类型,
                        "价格": room.价格,
                        "入住时间": item.入住时间,
                    })
            except ValueError:
                room_list = []
    return render(request, "accomodation.html", {"people_list": Accommodation.objects.order_by("id"), "room_list": room_list, "searched": searched})


# ---------- 入住管理增强：续住 / 换房 / 快捷离店 / 散客直入 / 电子账单 / No-Show ----------
@staff_required
@pms_permission_required('hotelapp.change_accommodation')
def extend_stay(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """续住：为在住订单加收一晚房费（Folio 房费凭证）"""
    if request.method == "POST":
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=request.POST.get("id"), 状态="已入住")
                price = str(item.客房编号.价格)
                Folio.objects.create(order=item, 类型="房费", 金额=price,
                                     事由="续住加收一晚房费", 日期=_today(), 经手人=request.user.username)
        except Accommodation.DoesNotExist:
            return HttpResponse("订单不存在", status=400)
        return JsonResponse({"ok": True, "amount": price})
    return HttpResponse("非法请求", status=400)


@staff_required
@pms_permission_required('hotelapp.change_accommodation')
def change_room(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """换房：在住/已确认订单更换房间，旧房转入清洁链"""
    if request.method == "POST":
        new_no = request.POST.get("new_room")
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=request.POST.get("id"))
                if item.状态 not in ("已确认", "已入住"):
                    return HttpResponse("只有已确认/在住订单可以换房", status=400)
                new_room = Room.objects.select_for_update().get(房间号=new_no)
                if new_room.房间状态 != "空闲":
                    return HttpResponse(f"目标房间不可用（{new_room.房间状态}）", status=400)
                old_room = item.客房编号
                # E-11/MGR-05：跨房型或价差的换房需值班经理/店长处理（价格政策例外）
                if (new_room.客房类型 != old_room.客房类型
                        or float(new_room.价格 or 0) != float(old_room.价格 or 0)):
                    if not _has_role(request.user, "值班经理", "店长"):
                        return HttpResponse(
                            "跨房型/价差换房需要值班经理或店长处理", status=403)
                if item.状态 == "已入住":
                    old_room.房间状态 = "待清洁"
                    new_room.房间状态 = "已入住"
                else:
                    old_room.房间状态 = "空闲"
                    new_room.房间状态 = "已预订"
                old_room.save()
                new_room.save()
                item.客房编号 = new_room
                item.save()
        except (Room.DoesNotExist, ValueError, TypeError):
            return HttpResponse("目标房间不存在", status=400)
        return JsonResponse({"ok": True})
    return HttpResponse("非法请求", status=400)


@staff_required
@pms_permission_required('hotelapp.change_accommodation')
def express_checkout(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """旧版快捷离店已退役，避免在无 PMS2 账本/支付状态核验下自动写账。"""
    if request.method != "POST":
        return HttpResponse("旧版快捷离店已退役，请使用 PMS2 前台工作台。", status=410)
    return HttpResponse(
        "快捷离店不再允许自动补记收款或绕过未结余额。请使用 PMS2 前台工作台核对账单后办理退房。",
        status=410,
    )

@staff_required
@pms_permission_required('hotelapp.add_accommodation')
def walkin_create(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """散客直入（Walk-in）：无预订直接排房入住，收取押金"""
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        tel = (request.POST.get("tel") or "").strip()
        idcard = (request.POST.get("idcard") or "").strip()
        room_no = request.POST.get("room")
        deposit = (request.POST.get("deposit") or "").strip()
        err, _ = _first_error([
            (name, "name", "姓名", "name"),
            (tel, "tel", "联系方式", "tel", False),
            (idcard, "idcard", "身份证号", "idcard", False),
            (deposit, "money", "押金", "deposit", False),
        ])
        try:
            with transaction.atomic():
                room = Room.objects.select_for_update().get(房间号=room_no)
                if room.房间状态 != "空闲":
                    return HttpResponse(f"该房间当前不可入住（{room.房间状态}）", status=400)
                if idcard and Client.objects.filter(身份证号=idcard).exists():
                    return HttpResponse("该身份证号已登记，请直接为已有顾客办理", status=400)
                client = Client.objects.create(姓名=name, 身份证号=(idcard or None))
                last = Accommodation.objects.order_by("-id").first()
                new_id = (last.id + 1) if last else 1
                item = Accommodation.objects.create(
                    id=new_id, 顾客id=client, 客房编号=room, 入住时间=_today(),
                    状态="已入住", 联系电话=tel, 押金=deposit or None,
                )
                state_machine.require_room_transition(room, "已入住")
                room.save()
                if deposit:
                    Folio.objects.create(order=item, 类型="押金", 金额=deposit,
                                         事由="散客直入押金", 日期=_today(), 经手人=request.user.username)
        except ValueError as e:
            return HttpResponse(str(e), status=400)
        return JsonResponse({"ok": True, "id": item.id})
    return HttpResponse("非法请求", status=400)


@staff_required
def no_show_one(request):
    """单独标记未到：过期未确认订单 → 未到，释放房间（需「未到处理」权限）"""
    if request.method == "POST":
        if not _has_permission(request, "hotelapp.no_show_handle"):
            return HttpResponse("处理未到订单需要值班经理/店长授权", status=403)
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=request.POST.get("id"))
                if item.状态 not in ("待确认", "已确认"):
                    return HttpResponse("只有待确认/已确认订单可以标记未到", status=400)
                if not (item.入住时间 or "") < _today():
                    return HttpResponse("未超过入住日期，不能标记未到", status=400)
                events.publish("no_show", order_id=item.id, room_no=item.客房编号.房间号)
                state_machine.require_order_transition(item, "未到")
                item.save()
        except ValueError as e:
            return HttpResponse(str(e), status=400)
        return JsonResponse({"ok": True})
    return HttpResponse("非法请求", status=400)


@staff_required
def folio_api(request):
    """电子账单数据：订单的全部 Folio 凭证 + 聚合（应收/押金/已收/余额）。

    V2 E-30：账单属于客房订单业务域，需 view_accommodation 权限——
    仅持员工登录身份（如客房服务员）不得读取住客账务。
    """
    if not _has_permission(request, "hotelapp.view_accommodation"):
        raise PermissionDenied("查看账单需要客房订单查看权限")
    oid = request.GET.get("id")
    try:
        item = Accommodation.objects.get(id=oid)
    except (Accommodation.DoesNotExist, ValueError):
        return HttpResponse("订单不存在", status=404)
    rows = [{"类型": f.类型, "金额": f.金额, "事由": f.事由, "日期": f.日期, "经手人": f.经手人 or ""}
            for f in Folio.objects.filter(order=item).order_by("id")]
    due = deposit = paid = 0.0
    for f in rows:
        try:
            amt = float(f["金额"])
        except (TypeError, ValueError):
            continue
        if f["类型"] in ("房费", "消费"):
            due += amt
        elif f["类型"] == "押金":
            deposit += amt
        elif f["类型"] == "收款":
            paid += amt
    balance = round(deposit + paid - due, 2)
    return JsonResponse({"rows": rows, "due": f"{due:.2f}", "deposit": f"{deposit:.2f}",
                         "paid": f"{paid:.2f}", "balance": f"{balance:.2f}",
                         "guest": item.顾客id.姓名, "room": str(item.客房编号.房间号),
                         "status": item.状态 or "待确认"})


# ---------- 房间状态流转 ----------
@staff_required
def room_status_change(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """房间状态流转：前台可做 空闲↔清洁中；维修/修复及跨状态流转仅值班经理及以上"""
    if request.method == "POST":
        room_no = request.POST.get("room")
        target = (request.POST.get("status") or "").strip()
        if target not in ROOM_STATUS:
            return HttpResponse("未知状态", status=400)
        try:
            room = Room.objects.get(房间号=room_no)
        except (Room.DoesNotExist, ValueError, TypeError):
            return HttpResponse("房间不存在", status=400)
        is_manager = _has_role(request.user, "值班经理", "店长")
        simple = {("待清洁", "清洁中"), ("清洁中", "待检查")}
        # 净房放行（待检查→空闲）独立授权：由当天管理者确认（决策 6：客房不拆检查岗）
        if (room.房间状态, target) == ("待检查", "空闲"):
            if not _has_permission(request, "hotelapp.room_release_ready"):
                return HttpResponse("净房放行需要值班经理/店长确认", status=403)
        elif (room.房间状态, target) not in simple and not is_manager:
            return HttpResponse("该状态流转需要值班经理操作", status=403)
        room.房间状态 = target
        room.save()
        return JsonResponse({"ok": True, "status": target})
    return HttpResponse("非法请求", status=400)




# 通用业务数据导出（图5 导出数据按钮）：模块白名单 + 查看权限校验
# P0-04（闭环补齐 2.0 §P0-04）：含 PII 明文列的旧导出（client 身份证/vip
# 联系方式/accommodation 顾客关联）从白名单移除——敏感数据导出必须走
# 揭示策略与审批链，不接受 CSV 静态遮罩绕过。非 PII 模块保留并补导出审计。
EXPORT_MODULES = {
    "food": ("food", Food, ["菜品编号", "菜名", "价格", "厨师编号"]),
    "room": ("room", Room, ["房间号", "客房类型", "价格", "负责人编号", "房间状态"]),
    "park": ("park", Park, ["车位编号", "当前状态", "车辆牌号"]),
    "order": ("order", Order, ["订单编号", "顾客编号", "菜品编号", "服务员编号", "桌号", "状态"]),
}


@staff_required
def export_module(request, module):
    """通用业务模块 CSV 导出；按模块白名单取列并校验 view 权限"""
    info = EXPORT_MODULES.get(module)
    if info is None:
        return HttpResponse("未知模块", status=404)
    if not MODULE_REGISTRY.get(module, True):
        messages.warning(request, "该模块已对接第三方系统，暂不开放；历史数据已保留。")
        return redirect("/home/")
    # P0-04（闭环补齐 2.0）：导出授权走 AuthorizationService + 专项
    # hotelapp.export_pii，移除 _has_role 角色名硬编码（矩阵外旁路）
    from apps.identity.authorization import AuthorizationService
    from apps.core.models import Property as _Prop
    _prop = getattr(request, "property", None) or _Prop.objects.filter(
        code=getattr(settings, "DEFAULT_PROPERTY_CODE", "DEFAULT")).first()
    if not AuthorizationService.has_permission(
            request.user, _prop, "hotelapp.export_pii"):
        return HttpResponse("首版常规数据导出仅开放给店长和财务。", status=403)
    model = info[1]
    if not _has_permission(request, f"hotelapp.view_{module}"):
        return HttpResponse("没有导出权限", status=403)
    # 顾客身份证与会员电话只导出脱敏值；完整值不通过常规 CSV 导出。    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="export.module_csv", entity_type="ExportModule",
        entity_id=module, actor=request.user, property_obj=_prop,
        after={"columns": list(info[2])},
        reason="旧只读模块 CSV 导出（非 PII 白名单）")

    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="{module}.csv"'
    writer = csv.writer(response)
    writer.writerow(info[2])
    field_names = {
        "client": ["顾客id", "姓名", "身份证号"],
        "food": ["菜品编号", "菜名", "价格", "厨师id"],
        "room": ["房间号", "客房类型", "价格", "负责人编号", "房间状态"],
        "park": ["车位编号", "当前状态", "车辆牌号"],
        "vip": ["会员编号", "姓名", "会员等级", "联系方式"],
        "order": ["id", "顾客编号", "菜品编号", "服务员编号", "桌号", "状态"],
        "accommodation": ["id", "顾客id", "客房编号", "入住时间", "状态", "收款金额"],
    }[module]
    exported_count = 0
    for row in model.objects.order_by(*[model._meta.pk.name]).values_list(*field_names):
        row = list(row)
        exported_count += 1
        if module == "client":
            # V2 §7.1：身份证完整值导出默认禁止（CSV 输出遮罩值，紧急合规导出待审批流程）
            row[2] = (row[2] or "")[:2] + "*" * max(0, len(row[2] or "") - 4) + (row[2] or "")[-2:] if len(row[2] or "") >= 6 else row[2]
        elif module == "vip":
            row[3] = (row[3] or "")[:3] + "****" + (row[3] or "")[-4:] if len(row[3] or "") >= 8 else row[3]
        writer.writerow([str(x) for x in row])
    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="legacy.export", entity_type="LegacyExport",
        entity_id=module, actor=request.user,
        property_obj=getattr(request, "property", None),
        after={"module": module, "row_count": exported_count,
               "pii_masked": module in ("client", "vip")},
        reason="常规业务数据导出", request=request)
    return response

# ---------- 敏感字段 reveal（V2 §7.3：完整值按需显示 + 理由审计） ----------
@staff_required
@pms_permission_required('hotelapp.guest_document_reveal')
def client_reveal_id(request):
    """取顾客身份证完整值：仅限持修改权岗位（前台/值班经理/店长），
    用于行内核验/修正；每次读取写审计日志（记录操作者与对象，不记录敏感原文）。"""
    if request.method != "POST":
        return HttpResponse("非法请求", status=400)
    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 8 or len(reason) > 240:
        return HttpResponse("揭示身份证必须填写 8 至 240 个字符的业务用途/原因", status=400)
    try:
        item = Client.objects.get(顾客id=request.POST.get("id"))
    except (Client.DoesNotExist, ValueError):
        return HttpResponse("顾客不存在", status=404)
    logging.getLogger("hotel.audit").info(
        "PII reveal 顾客身份证 user=%s 顾客id=%s reason=%s",
        request.user.username, item.顾客id, reason)
    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="guest.pii_reveal", entity_type="Client", entity_id=item.顾客id,
        actor=request.user, property_obj=getattr(request, "property", None),
        reason=reason,
        after={"masked": (item.身份证号 or "")[:2] + "****" + (item.身份证号 or "")[-2:]},
        request=request)
    return JsonResponse({"identify": item.身份证号 or ""})


@staff_required
@pms_permission_required('hotelapp.guest_phone_reveal')
def vip_reveal_tel(request):
    """取会员联系方式完整值：仅限持修改权岗位；读取写审计。"""
    if request.method != "POST":
        return HttpResponse("非法请求", status=400)
    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 8 or len(reason) > 240:
        return HttpResponse("揭示电话必须填写 8 至 240 个字符的业务用途/原因", status=400)
    try:
        item = Vip.objects.get(会员编号=request.POST.get("id"))
    except (Vip.DoesNotExist, ValueError):
        return HttpResponse("会员不存在", status=404)
    logging.getLogger("hotel.audit").info(
        "PII reveal 会员联系方式 user=%s 会员编号=%s reason=%s",
        request.user.username, item.会员编号, reason)
    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="member.pii_reveal", entity_type="Vip", entity_id=item.会员编号,
        actor=request.user, property_obj=getattr(request, "property", None),
        reason=reason,
        after={"masked": (item.联系方式 or "")[:3] + "****" + (item.联系方式 or "")[-4:]},
        request=request)
    return JsonResponse({"tel": item.联系方式 or ""})




# ---------- 员工与账号管理工作台（只读存档页：写操作已迁移员工中心 /manage/staff/） ----------


def _workbench_profile_page(profile, property_obj):
    """聚合当前物业的员工档案/任职/账号/角色授权为一个视图对象。"""
    from apps.identity.models import StaffRoleAssignment
    from apps.identity.services import EmployeeLifecycleService
    membership = EmployeeLifecycleService.get_membership(profile, property_obj)
    user = profile.user
    assignments = list(StaffRoleAssignment.objects.filter(
        membership=membership).select_related("group").order_by("-starts_at")) if membership else []
    now = timezone.now()
    return {
        "profile": profile,
        "membership": membership,
        "user": user,
        "account_state": ("未开通" if user is None else
                          ("已停用" if not user.is_active else "正常")),
        "assignments": assignments,
        "active_roles": [a.group.name for a in assignments
                         if a.ends_at is None or a.ends_at > now],
    }


@staff_required
@pms_permission_required("identity.staff_manage")
def staff_accounts(request):
    """员工与账号管理（只读存档页，§10.1）：列表 + 搜索。

    写操作已迁移至员工中心 /manage/staff/（apps/identity/staff_center_views.py）；
    本页仅保留只读列表，页头有"已迁移"提示条，不再渲染写表单。
    """
    from apps.identity.models import StaffProfile
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return HttpResponse("当前账号没有可用物业上下文", status=503)
    qs = (StaffProfile.objects.filter(property_memberships__property=property_obj)
          .select_related("user").distinct().order_by("-created_at"))
    q = (request.GET.get("q") or "").strip()
    rows = []
    for profile in qs:
        page = _workbench_profile_page(profile, property_obj)
        if q and q not in page["profile"].full_name and q not in (
                page["membership"].employee_no if page["membership"] else ""):
            continue
        rows.append(page)
    paginator = Paginator(rows, 15)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "staff_accounts.html", {
        "rows": page_obj.object_list, "page_obj": page_obj, "q": q,
    })


@staff_required
@pms_permission_required("identity.staff_manage")
def staff_accounts_detail(request, pk):
    """员工详情（只读存档页，§10.1）：资料/任职/账号/角色授权历史。

    写操作已于 §10.1 迁移至 /manage/staff/（apps/identity/staff_center_views.py），
    旧写路由只保留 302 兼容跳转（hotel/urls.py）；本页不再渲染任何写表单。
    """
    from apps.identity.models import StaffProfile
    property_obj = getattr(request, "property", None)
    if property_obj is None:
        return HttpResponse("当前账号没有可用物业上下文", status=503)
    profile = get_object_or_404(
        StaffProfile, pk=pk, property_memberships__property=property_obj)
    page = _workbench_profile_page(profile, property_obj)
    return render(request, "staff_accounts_detail.html", {
        **page, "now": timezone.now(),
    })


@staff_required
def force_password_change(request):
    """D-07：首次登录/密码被重置后强制修改密码；完成后清除标记。"""
    from apps.identity.models import StaffProfile
    profile = StaffProfile.objects.filter(user=request.user).first()
    if profile is None or not profile.must_change_password:
        return redirect("/home/")
    error = None
    if request.method == "POST":
        new1 = request.POST.get("new_password1") or ""
        new2 = request.POST.get("new_password2") or ""
        if len(new1) < 6:
            error = "密码至少 6 位"
        elif new1 != new2:
            error = "两次输入的密码不一致"
        else:
            try:
                from django.contrib.auth.password_validation import validate_password
                from django.core.exceptions import ValidationError as _PwdError
                validate_password(new1, user=request.user)
            except _PwdError as exc:
                error = "；".join(exc.messages)
            if not error:
                request.user.set_password(new1)  # session_auth_hash 变更 → 其他会话失效
                request.user.save(update_fields=("password",))
                profile.must_change_password = False
                profile.last_password_change = timezone.now()
                profile.save(update_fields=("must_change_password",
                                            "last_password_change", "updated_at"))
                from apps.core.models import AuditEvent
                AuditEvent.record(
                    action="identity.password_changed", entity_type="StaffProfile",
                    entity_id=profile.pk, actor=request.user,
                    reason="首次登录/被重置后强制改密完成")
                from django.contrib.auth import update_session_auth_hash
                update_session_auth_hash(request, request.user)  # 保持当前会话
                messages.success(request, "密码修改成功，欢迎使用酒店管理系统")
                return redirect("/home/")
    return render(request, "force_password_change.html", {"error": error})


# ---------- 登录 / 注册 / 退出 / 主界面 ----------
def _active_role_assignments(user):
    """当前 DEFAULT 物业内有效岗位授权；不读取 Django User.groups。"""
    from apps.identity.models import PropertyStaffMembership, StaffRoleAssignment, StaffProfile
    now = timezone.now()
    return StaffRoleAssignment.objects.filter(
        membership__staff_profile__user=user,
        membership__staff_profile__employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        membership__status=PropertyStaffMembership.Status.ACTIVE,
        membership__property__code=settings.DEFAULT_PROPERTY_CODE,
        starts_at__lte=now,
    ).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))


def _has_role(user, *names):
    """按当前 DEFAULT 任职下有效的 StaffRoleAssignment 判断岗位。"""
    if not getattr(user, "is_authenticated", False):
        return False
    return _active_role_assignments(user).filter(group__name__in=names).exists()


def _has_permission(request, permission_code):
    """旧后台统一使用物业范围授权；不调用 User.has_perm()。"""
    return AuthorizationService.has_permission(
        request.user, getattr(request, "property", None), permission_code
    )


def _role_name(user):
    names = list(_active_role_assignments(user).order_by("group__name")
                 .values_list("group__name", flat=True).distinct())
    return "、".join(names) if names else "未分配岗位"
def _today():
    return datetime.date.today().strftime("%Y-%m-%d")


_LOGIN_FAIL_MESSAGE = "账号或密码不正确，或账号暂不可用"


def _audit_login_failure(request, username, reason):
    """P0-11：登录失败持久化审计（AuditEvent）+ 内部日志。

    username 只存 HMAC 指纹（§16.2 脱敏），细分原因保留在内部日志与审计 reason。"""
    import logging as _logging
    from apps.core.models import AuditEvent
    from apps.core.rate_limit import fingerprint
    _logging.getLogger("security").warning(
        "login_failed ip=%s username_fp=%s reason=%s",
        request.META.get("REMOTE_ADDR", ""), fingerprint(username)[:12], reason)
    AuditEvent.record(
        action="auth.login_failure", entity_type="AuthAttempt",
        entity_id=fingerprint(username), reason=reason, request=request)


def _safe_login_next(request, candidate):
    """登录后回跳地址安全校验：仅允许本站相对地址（防 //外部域名 开放重定向）。"""
    if candidate and url_has_allowed_host_and_scheme(
            url=candidate, allowed_hosts={request.get_host()},
            require_https=request.is_secure()):
        return candidate
    return "/home/"


def login_view(request):
    """统一登录页：登录后进入主界面（或按安全校验的 next= 回跳原页面）"""
    # D-04：登录页重定向看"是否 PMS 员工"（有 DEFAULT 任职），不再看 is_staff
    if request.user.is_authenticated and _has_active_employment(request.user):
        return redirect("/home/")
    error = None
    # AUTH-18：邀请激活成功后重定向 /login/?activated=1 → 展示一次性成功提示
    activated = request.GET.get("activated", "") == "1"
    raw_next = request.GET.get("next", "") or request.POST.get("next", "")
    next_url = _safe_login_next(request, raw_next) if raw_next else ""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        # §6.3：登录失败限流（IP + username_hash，5 次/5 分钟，fail-closed）
        from apps.core import rate_limit
        if not rate_limit.check(request, "auth.login", username=username):
            _audit_login_failure(request, username, "rate_limited")
            error = _LOGIN_FAIL_MESSAGE
            return render(request, "login.html",
                          {"error": error, "next": next_url}, status=429)
        # P1-09：authenticate() 对停用账号返回 None（ModelBackend 拒绝 inactive），
        # 旧 disabled 分支不可达且原因失真；改为先取账号再验证，区分真实原因
        login_user = User.objects.filter(username__iexact=username).first()
        if login_user is not None and not login_user.is_active:
            _audit_login_failure(request, username, "disabled")
            error = _LOGIN_FAIL_MESSAGE
            return render(request, "login.html",
                          {"error": error, "next": next_url})
        user = authenticate(request, username=username,
                            password=request.POST.get("password"))
        if user is None:
            error = _LOGIN_FAIL_MESSAGE
            _audit_login_failure(request, username, "bad_credentials")
        elif not _has_active_employment(user):
            error = _LOGIN_FAIL_MESSAGE
            _audit_login_failure(request, username, "no_employment")
        else:
            auth_login(request, user)
            from apps.core.models import AuditEvent
            AuditEvent.record(
                action="auth.login_success", entity_type="User",
                entity_id=str(user.pk), actor=user, request=request)
            return redirect(_safe_login_next(request, request.POST.get("next")))
    return render(request, "login.html",
                  {"error": error, "next": next_url, "activated": activated})


def register_view(request):
    """AUTH-01 注册止血（D1/D8）：公开注册通道停止创建任何 User。

    住客无需账号即可直订（/reserve/ 免注册）；员工账号由管理员经
    EmployeeLifecycleService 开通。历史孤儿 User 分类处理见方案 D10。"""
    return redirect("/reserve/")



@login_required
def logout_view(request):
    auth_logout(request)
    return redirect("/login/")


@staff_required
def home(request):
    """主界面：PMS2 权威数据仪表盘（P1-11 切权威；本次补齐图表数据与模板绑定）。

    修复：home.html 原仍消费旧扁平键 stats.pending 与旧模型图表数据，
    导致页面统计卡与全部图表为空；现按 dashboard_data 嵌套键绑定，
    图表/预警/财务卡全部来自 PMS2 模型（apps/api/dashboard.py）。
    """
    from apps.api.dashboard import (
        dashboard_data, dashboard_charts, dashboard_warnings, dashboard_finance)
    from apps.core.models import Property
    user = request.user
    prop = getattr(request, "property", None)
    if prop is None:
        prop = Property.objects.filter(code="DEFAULT").first()
    stats = dashboard_data(prop) if prop else {}
    charts = dashboard_charts(prop) if prop else {}
    warnings = dashboard_warnings(prop) if prop else []
    # 财务卡权限：财务/值班经理/店长（沿用旧页面口径：finance_access 或员工管理）
    show_finance = bool(prop) and (
        AuthorizationService.has_permission(user, prop, "hotelapp.finance_access")
        or AuthorizationService.has_permission(user, prop, "identity.staff_manage"))
    ctx = {
        "stats": stats,
        "role": _role_name(user),
        "today": str(timezone.localdate()),
        "room_status_counts": charts.get("room_status_counts", []),
        "room_type_inventory": charts.get("room_type_inventory", []),
        "vip_counts": charts.get("vip_counts", []),
        "client_dash": charts.get("client_dash", []),
        "warnings": warnings,
        "show_finance": show_finance,
        "finance_dash": dashboard_finance(prop) if show_finance and prop else None,
    }
    return render(request, "home.html", ctx)

def booking_page(request):
    """AUTH-02 冻结旧预订写入（D8）：统一指向 PMS2 公开直订 /reserve/。

    旧 Accommodation/Client 写路径保留代码待退役，但入口不再可达。"""
    return redirect("/reserve/", permanent=False)



@staff_required
@pms_permission_required('hotelapp.view_accommodation')
def checkin_workspace(request):
    """PMS 2.0 前台接待工作台；业务数据通过 /api/v1/front-desk/ 查询。"""
    return render(request, "frontdesk.html", {
        "role": _role_name(request.user),
        "today": _today(),
    })

# ---------- 入住管理 ----------
@staff_required
@pms_permission_required('hotelapp.view_accommodation')
def checkin_manage(request):
    """入住管理工作台：预抵名单 / 待确认排房 / 已确认待入住 / 在住 / 近期退房"""
    import datetime as _dt
    rooms = Room.objects.order_by("房间号")
    pending = Accommodation.objects.filter(状态="待确认").order_by("id")
    confirmed = Accommodation.objects.filter(状态="已确认").order_by("id")
    living = Accommodation.objects.filter(状态="已入住").order_by("id")
    done = Accommodation.objects.filter(状态="已退房").order_by("-id")[:10]
    pre_arrival = confirmed.filter(入住时间__lte=(_dt.date.today() + _dt.timedelta(days=3)).strftime("%Y-%m-%d"))
    return render(request, "checkin.html", {
        "rooms": rooms, "pending": pending, "confirmed": confirmed, "living": living,
        "done": done, "pre_arrival": pre_arrival,
        "today": _today(), "can_void": _has_permission(request, "hotelapp.reverse_checkout"),
    })


@staff_required
def cancel_booking(request):
    """P1-04（方案 §10.4）：退役兼容入口——去掉员工门禁与重复装饰器，
    匿名/员工 GET/POST 统一跳转 /reserve/，不触发任何旧表写入。"""
    return redirect("/reserve/", permanent=False)



@staff_required
@pms_permission_required('hotelapp.change_accommodation')
def checkin_confirm(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """排房确认（待确认 → 已确认）：分配房间并锁房，补录顾客身份证"""
    if request.method == "POST":
        oid = request.POST.get("id")
        room_no = request.POST.get("room")
        idcard = (request.POST.get("idcard") or "").strip()
        warn = None
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=oid, 状态="待确认")
                room = Room.objects.select_for_update().get(房间号=room_no)
                if room.房间状态 != "空闲" and room.房间号 != item.客房编号.房间号:
                    return HttpResponse(f"该房间当前不可排房（{room.房间状态}）", status=400)
                if idcard:
                    if Client.objects.filter(身份证号=idcard).exclude(顾客id=item.顾客id.顾客id).exists():
                        return HttpResponse("该身份证号已登记，证件号不能重复", status=400)
                    cli = item.顾客id
                    cli.身份证号 = idcard
                    cli.save()
                state_machine.require_order_transition(item, "已确认")
                item.客房编号 = room
                item.save()
                state_machine.require_room_transition(room, "已预订")
                room.save()
                events.publish("booking_confirmed", order_id=item.id,
                               room_no=room.房间号, operator=request.user.username)
                if (item.入住时间 or "") > _today():
                    warn = f"该订单入住日期为 {item.入住时间}，属提前到店，请按酒店规则处理房费"
        except ValueError as e:
            return HttpResponse(str(e), status=400)
        return JsonResponse({"ok": True, "warn": warn})
    return HttpResponse("非法请求", status=400)


@staff_required
@pms_permission_required('hotelapp.change_accommodation')
def checkin_in(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """办理入住（已确认 → 已入住）：收押金，房态转在住"""
    if request.method == "POST":
        oid = request.POST.get("id")
        deposit = (request.POST.get("deposit") or "").strip()
        err = _bad(deposit, "money", "押金", required=False)
        if err:
            return HttpResponse(err, status=400)
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=oid, 状态="已确认")
                room = item.客房编号
                state_machine.require_order_transition(item, "已入住")
                item.入住时间 = _today()
                item.押金 = deposit or None
                item.save()
                state_machine.require_room_transition(room, "已入住")
                room.save()
                if deposit:
                    Folio.objects.create(order=item, 类型="押金", 金额=deposit,
                                         事由="入住押金", 日期=_today(), 经手人=request.user.username)
        except ValueError as e:
            return HttpResponse(str(e), status=400)
        return JsonResponse({"ok": True})
    return HttpResponse("非法请求", status=400)


@staff_required
@pms_permission_required('hotelapp.change_accommodation')
def checkout(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """已入住 → 已退房：结算收款并写入财务流水，房间转清洁中"""
    if request.method == "POST":
        oid = request.POST.get("id")
        amount = (request.POST.get("amount") or "").strip()
        try:
            item = Accommodation.objects.get(id=oid, 状态="已入住")
        except (Accommodation.DoesNotExist, ValueError):
            return HttpResponse("订单不存在或状态已变更", status=400)
        if not re.match(r"^\d{1,7}(\.\d{1,2})?$", amount):
            return HttpResponse("收款金额必须是数字", status=400)
        room = item.客房编号
        state_machine.require_order_transition(item, "已退房")
        item.退房时间 = _today()
        item.save()
        events.publish("checked_out", order_id=item.id, room_no=room.房间号,
                       amount=amount, operator=request.user.username, date=_today())
        Folio.objects.create(order=item, 类型="收款", 金额=amount,
                             事由="退房结算收款", 日期=_today(), 经手人=request.user.username)
        item.收款金额 = amount
        return JsonResponse({"ok": True})
    return HttpResponse("非法请求", status=400)


@staff_required
def uncheckout(request):
    # P1-05 旧写路由退役（方案 §10）：写能力已由 PMS2 服务接管，此入口永久 410
    return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)
    """反结账：已退房 → 已入住，删除对应收入流水（需「反结账」权限：值班经理/店长）"""
    if request.method == "POST":
        if not _has_permission(request, "hotelapp.reverse_checkout"):
            return HttpResponse("反结账需要值班经理/店长授权", status=403)
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=request.POST.get("id"), 状态="已退房")
                room = item.客房编号
                # 房态校验：房间已被新客人入住时绝对禁止反结账
                if room.房间状态 == "已入住":
                    return HttpResponse("该房间已被新客人入住，禁止反结账", status=400)
                # 夜审校验：退房后已经历夜审结转 → 走财务历史账务调整
                if item.退房时间 and NightAudit.objects.filter(营业日期__gte=item.退房时间).exists():
                    return HttpResponse("该订单已过夜审结转，请走财务历史账务调整流程", status=400)
                state_machine.require_order_transition(item, "已入住")
                item.状态 = "已入住"
                item.收款金额 = None
                item.退房时间 = None
                item.save()
                state_machine.require_room_transition(room, "已入住")
                room.save()
                FinanceRecord.objects.filter(关联订单=str(item.id), 类型="收入").delete()
                Folio.objects.filter(order=item, 类型="收款").delete()
        except (ValueError, Accommodation.DoesNotExist) as e:
            return HttpResponse(str(e) or "订单不存在或状态已变更", status=400)
        return JsonResponse({"ok": True})
    return HttpResponse("非法请求", status=400)


# ---------- 财务管理 ----------
def _finance_context():
    """财务页公共上下文：流水列表 + 冲账排除 + 收支统计（finance_manage / finance_add 共用）"""
    records = list(FinanceRecord.objects.order_by("-id"))
    voided = {str(o["id"]) for o in Order.objects.filter(状态="已冲账").values("id")}
    voided |= {str(a["id"]) for a in Accommodation.objects.filter(状态="已冲账").values("id")}
    days = {}
    total_in = total_out = room_rev = food_rev = 0.0
    for r in records:
        if r.关联订单 and r.关联订单 in voided:
            continue
        try:
            amt = float(r.金额)
        except (TypeError, ValueError):
            continue
        day = days.setdefault(r.日期, {"收入": 0.0, "支出": 0.0})
        day[r.类型 if r.类型 in ("收入", "支出") else "支出"] += amt
        if r.类型 == "收入":
            total_in += amt
            if (r.事由 or "").startswith("客房"):
                room_rev += amt
            elif (r.事由 or "").startswith("餐饮"):
                food_rev += amt
        else:
            total_out += amt
    day_rows = [{"日期": k, "收入": f"{d['收入']:.2f}", "支出": f"{d['支出']:.2f}",
                 "结余": f"{d['收入'] - d['支出']:.2f}"} for k, d in sorted(days.items(), reverse=True)]
    total = round(total_in - total_out, 2)
    rooms_total = Room.objects.count()
    living_now = Accommodation.objects.filter(状态="已入住").count()
    occupancy = f"{(living_now / rooms_total * 100):.0f}%" if rooms_total else "0%"
    revpar = f"{(room_rev / rooms_total):.2f}" if rooms_total else "0.00"
    return {
        "records": records, "day_rows": day_rows,
        "total_in": f"{total_in:.2f}", "total_out": f"{total_out:.2f}", "total": f"{total:.2f}",
        "room_rev": f"{room_rev:.2f}", "food_rev": f"{food_rev:.2f}",
        "occupancy": occupancy, "revpar": revpar,
    }


@staff_required
def finance_manage(request):
    """财务管理：流水列表 + 新增收支 + 报表分析（需「财务管理」权限）"""
    if not _has_permission(request, "hotelapp.finance_access"):
        raise PermissionDenied
    ctx = _finance_context()
    ctx["can_add"] = _has_role(request.user, "财务")
    ctx["today"] = _today()
    return render(request, "finance.html", ctx)


@staff_required
def finance_add(request):
    """旧财务写入口永久退役；新收款/退款/冲销只允许走 PMS2 BillingService。"""
    return HttpResponse(
        "该旧写入口已退役（410 Gone），请使用 /manage/finance/。",
        status=410,
        headers={"Location": "/manage/finance/"},
    )


# ---------- 夜审（Night Audit） ----------
@staff_required
def night_audit(request):
    """旧夜审工作台已退役（§4.2）：302 迁移到 PMS2 营业日中心。

    保留 @staff_required（登录 + 有效任职）作为旧路由门禁证据；细粒度权限
    在新页面复核（/manage/night-audit/ 同码 hotelapp.night_audit_execute）。
    """
    return HttpResponseRedirect("/manage/night-audit/")


@staff_required
def night_audit_run(request):
    """旧夜审执行已退役（P1-09 V1.1 §12.4）：请使用 PMS2 NightAuditService API。"""
    return HttpResponse(
        "旧夜审写入口已退役（410 Gone）：请使用 /api/v1/night-audit/preview 和 /api/v1/night-audit/run",
        status=410,
        headers={"Location": "/api/v1/night-audit/preview"})



# ---------- 系统环境配置信息（图4 → §6.1 拆分为员工个人页） ----------
@staff_required
def system_info(request):
    """PMS 员工个人页（§6.1）：仅本人信息（姓名/账号/角色/任职/本人最后登录）
    + 修改自己密码。平台环境信息已拆至 /platform/system/（平台会话域），
    本页不得渲染任何平台级配置或其他员工的 PII。"""
    user = request.user
    role = _role_name(user)

    error = None
    if request.method == "POST":
        # 自助改密：与 /force-password-change/ 同一套校验规则（D-07 逻辑复用）
        new1 = request.POST.get("new_password1") or ""
        new2 = request.POST.get("new_password2") or ""
        if len(new1) < 6:
            error = "密码至少 6 位"
        elif new1 != new2:
            error = "两次输入的密码不一致"
        else:
            try:
                from django.contrib.auth.password_validation import validate_password
                from django.core.exceptions import ValidationError as _PwdError
                validate_password(new1, user=request.user)
            except _PwdError as exc:
                error = "；".join(exc.messages)
        if not error:
            from django.contrib.auth import update_session_auth_hash
            from apps.identity.models import StaffProfile as _StaffProfile
            user.set_password(new1)  # session_auth_hash 变更 → 其他会话失效
            user.save(update_fields=("password",))
            update_session_auth_hash(request, user)  # 当前会话保持登录
            profile = _StaffProfile.objects.filter(user=user).first()
            if profile is not None and profile.must_change_password:
                profile.must_change_password = False
                profile.last_password_change = timezone.now()
                profile.save(update_fields=("must_change_password",
                                            "last_password_change", "updated_at"))
            from apps.core.models import AuditEvent
            AuditEvent.record(
                action="identity.password_changed", entity_type="StaffProfile",
                entity_id=str(profile.pk if profile else user.pk),
                actor=user, reason="self_change_via_pms_me", request=request)
            messages.success(request, "密码已修改")
            return redirect("/system/")

    membership = getattr(request, "staff_membership", None)
    property_obj = getattr(request, "property", None)
    from apps.identity.models import PropertyStaffMembership as _PropertyStaffMembership
    last_login = user.last_login or timezone.now()
    session_age = int(settings.SESSION_COOKIE_AGE)
    expire_at = last_login + datetime.timedelta(seconds=session_age)
    remain_seconds = max(0, int((expire_at - timezone.now()).total_seconds()))
    hours, rem = divmod(remain_seconds, 3600)
    minutes = rem // 60
    full_name = user.first_name or (
        membership.staff_profile.full_name if membership is not None else ""
    ) or user.username
    ctx = {
        "admin_no": f"AD-{user.id:08d}",
        "full_name": full_name,
        "role": role,
        "property_label": (
            f"{property_obj.name}（{property_obj.code}）"
            if property_obj is not None else "未解析到有效任职物业"),
        "employee_no": membership.employee_no if membership is not None else "",
        "membership_status": (
            membership.status if membership is not None else "NONE"),
        "membership_status_label": (
            dict(_PropertyStaffMembership.Status.choices).get(
                membership.status) if membership is not None else "无有效任职"),
        "login_at": timezone.localtime(last_login).strftime("%Y-%m-%d %H:%M:%S"),
        "expire_at": timezone.localtime(expire_at).strftime("%Y-%m-%d %H:%M:%S"),
        "remain": f"{hours}h {minutes}m",
        "password_error": error,
    }
    return render(request, "pms/me.html", ctx)
