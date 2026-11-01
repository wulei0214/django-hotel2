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
from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import render, redirect

from .models import (Staff, Room, Client, Food, Vip, Park, Order, Accommodation,
                     FinanceRecord, NightAudit, Folio, ROOM_STATUS, ORDER_STATUS)
from . import events


# ---------- 模块启停注册表（RBAC 决策：餐饮/停车对接第三方，本阶段隐藏入口） ----------
# 代码与历史数据全部保留；置 True 即恢复页面（权限矩阵中对应行也同步恢复）
MODULE_REGISTRY = {
    "food": False,   # 餐厅管理（菜品）
    "order": False,  # 餐饮订单（依赖菜品）
    "park": False,   # 停车场
}


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
from . import handlers  # noqa: F401  订阅器注册（事件 -> 后台联动）
from . import state_machine


def _has_active_employment(user):
    """RBAC V2 P0-1：员工须有有效 DEFAULT 任职（档案在职 + 任职 ACTIVE）。

    与 Django 组权限双写配套的失效闸门：停职/离职/任职终止后，
    即使 User.groups 仍保留角色，下一个请求也会被拒绝（AUTH-05）。
    V2 SCOPE-01/P1-6：仅 DEFAULT 物业任职有效，其他物业任职视同无任职。
    平台超级管理员是技术身份，不受业务任职约束。
    """
    from apps.identity.models import PropertyStaffMembership, StaffProfile
    return PropertyStaffMembership.objects.filter(
        staff_profile__user=user,
        staff_profile__employment_status=StaffProfile.EmploymentStatus.ACTIVE,
        status=PropertyStaffMembership.Status.ACTIVE,
        property__code=settings.DEFAULT_PROPERTY_CODE,
    ).exists()


def staff_required(view):
    """登录 + 员工身份双重校验；登录页统一用前台 /login/。

    RBAC V2 P0-1：非超管员工还必须持有有效酒店任职——
    账号停用/员工离职/任职暂停立即失权，不依赖重新登录。
    """
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if (user.is_authenticated and user.is_staff
                and not user.is_superuser
                and not _has_active_employment(user)):
            raise PermissionDenied("当前账号没有有效的酒店任职关系，请联系管理员")
        return view(request, *args, **kwargs)
    return login_required(staff_member_required(wrapped, login_url='/login/'))


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
    exists = cache.get(key)
    if exists is None:
        exists = model.objects.filter(**{field: value}).exists()
        cache.set(key, exists, 60)
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
@permission_required('hotelapp.view_staff', raise_exception=True)
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
    })


@staff_required
@permission_required('hotelapp.change_staff', raise_exception=True)
def staff_toggle(request):
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
@permission_required('hotelapp.view_staff', raise_exception=True)
@permission_required("hotelapp.export_pii", raise_exception=True)
def export_staff(request):
    """员工数据导出（CSV，UTF-8 BOM 兼容 Excel）；含月薪，需「导出敏感数据」权限"""
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = 'attachment; filename="staff.csv"'
    writer = csv.writer(response)
    writer.writerow(["员工编号", "姓名", "职务", "月薪", "工龄", "是否启用"])
    for s in Staff.objects.order_by("员工id"):
        writer.writerow([s.员工id, s.姓名, s.职务, s.月薪, s.工龄, "启用" if s.启用 else "停用"])
    return response


@staff_required
@permission_required('hotelapp.add_staff', raise_exception=True)
def insert(request):
    error, efield = None, None
    if request.method == "POST":
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
                Staff.objects.create(**kwargs)
            except (ValueError, IntegrityError):
                error, efield = "保存失败：工号必须是数字且不能重复", "id"
            else:
                messages.success(request, "员工信息添加成功")
                return redirect("/index/")
    ctx = _error_context(request, efield)
    ctx.update({"people_list": Staff.objects.order_by("员工id"), "error": error})
    return render(request, "index.html", ctx)


@staff_required
@permission_required('hotelapp.delete_staff', raise_exception=True)
def delete_staff(request):
    if request.method == "GET":
        try:
            Staff.objects.filter(员工id=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该员工已被菜品、客房或订单信息引用", status=400)
    return render(request, "index.html", {"people_list": Staff.objects.order_by("员工id")})


@staff_required
@permission_required('hotelapp.view_staff', raise_exception=True)
def search_staff(request):
    rows, searched = _search(Staff, "员工id", STAFF_SEARCH_FIELDS, request)
    return render(request, "index.html", {
        "people_list": Staff.objects.order_by("员工id"),
        "staffs": rows,
        "searched": searched,
    })


@staff_required
@permission_required('hotelapp.change_staff', raise_exception=True)
def update_staff(request):
    if request.method == "POST":
        sid = request.POST.get("id")
        job = request.POST.get("job")
        salary = request.POST.get("salary")
        work_time = request.POST.get("time")
        error, _ = _first_error([
            (sid, "id10", "工号", "id"),
            (job, "job", "职务", "job"),
            (salary, "money", "月薪", "salary"),
            (work_time, "digits4", "工龄", "time"),
        ])
        if error:
            return HttpResponse(error, status=400)
        try:
            updated = Staff.objects.filter(员工id=sid).update(职务=job, 月薪=salary, 工龄=work_time)
        except ValueError:
            return HttpResponse("修改失败：工号非法", status=400)
        if not updated:
            return HttpResponse("修改失败：员工不存在", status=400)
    return render(request, "index.html", {"people_list": Staff.objects.order_by("员工id")})


# ---------- 顾客信息管理 ----------
@staff_required
@permission_required('hotelapp.view_client', raise_exception=True)
def show_client(request):
    """顾客信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Client.objects.order_by("顾客id"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "html1.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@permission_required('hotelapp.add_client', raise_exception=True)
def insert_client(request):
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
@permission_required('hotelapp.delete_client', raise_exception=True)
def delete_client(request):
    if request.method == "GET":
        try:
            Client.objects.filter(顾客id=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该顾客已被餐饮订单或客房订单引用", status=400)
    return render(request, "html1.html", {"people_list": Client.objects.order_by("顾客id")})


@staff_required
@permission_required('hotelapp.change_client', raise_exception=True)
def update_client(request):
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
@permission_required('hotelapp.view_client', raise_exception=True)
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
@permission_required('hotelapp.view_food', raise_exception=True)
@module_gate("food")
def show_food(request):
    """菜品信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Food.objects.order_by("菜品编号"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "html4.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@permission_required('hotelapp.add_food', raise_exception=True)
@module_gate("food")
def insert_food(request):
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
@permission_required('hotelapp.delete_food', raise_exception=True)
@module_gate("food")
def delete_food(request):
    if request.method == "GET":
        try:
            Food.objects.filter(菜品编号=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该菜品已被餐饮订单引用", status=400)
    return render(request, "html4.html", {"people_list": Food.objects.order_by("菜品编号")})


@staff_required
@permission_required('hotelapp.change_food', raise_exception=True)
@module_gate("food")
def update_food(request):
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
@permission_required('hotelapp.view_food', raise_exception=True)
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
@permission_required('hotelapp.view_vip', raise_exception=True)
def show_vip(request):
    """会员信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Vip.objects.order_by("会员编号"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "vip.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@permission_required('hotelapp.add_vip', raise_exception=True)
def insert_vip(request):
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
@permission_required('hotelapp.delete_vip', raise_exception=True)
def delete_vip(request):
    if request.method == "GET":
        try:
            Vip.objects.filter(会员编号=request.GET.get("id")).delete()
        except ValueError:
            return HttpResponse("删除失败：会员编号非法", status=400)
    return render(request, "vip.html", {"people_list": Vip.objects.order_by("会员编号")})


@staff_required
@permission_required('hotelapp.change_vip', raise_exception=True)
def update_vip(request):
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
@permission_required('hotelapp.view_vip', raise_exception=True)
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
@permission_required('hotelapp.view_park', raise_exception=True)
@module_gate("park")
def show_park(request):
    """车位信息管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Park.objects.order_by("车位编号"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "park.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@permission_required('hotelapp.add_park', raise_exception=True)
@module_gate("park")
def insert_park(request):
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
@permission_required('hotelapp.delete_park', raise_exception=True)
@module_gate("park")
def delete_park(request):
    if request.method == "GET":
        try:
            Park.objects.filter(车位编号=request.GET.get("id")).delete()
        except ValueError:
            return HttpResponse("删除失败：车位编号非法", status=400)
    return render(request, "park.html", {"people_list": Park.objects.order_by("车位编号")})


@staff_required
@permission_required('hotelapp.change_park', raise_exception=True)
@module_gate("park")
def update_park(request):
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
@permission_required('hotelapp.view_park', raise_exception=True)
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
@permission_required('hotelapp.view_room', raise_exception=True)
def show_room(request):
    """客房信息管理列表（图5）：分页 15 条/页；is_room_manager 控制维修类按钮显隐"""
    paginator = Paginator(Room.objects.order_by("房间号"), 15)
    page = paginator.get_page(request.GET.get("page"))
    is_room_manager = _has_role(request.user, "值班经理", "店长")
    return render(request, "room.html", {
        "people_list": page.object_list, "page_obj": page,
        "is_room_manager": is_room_manager,
    })


@staff_required
@permission_required('hotelapp.add_room', raise_exception=True)
def insert_room(request):
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
@permission_required('hotelapp.delete_room', raise_exception=True)
def delete_room(request):
    if request.method == "GET":
        try:
            Room.objects.filter(房间号=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该客房已被客房订单引用", status=400)
    return render(request, "room.html", {"people_list": Room.objects.order_by("房间号")})


@staff_required
@permission_required('hotelapp.change_room', raise_exception=True)
def update_room(request):
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
@permission_required('hotelapp.view_room', raise_exception=True)
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
@permission_required('hotelapp.view_order', raise_exception=True)
@module_gate("order")
def show_order(request):
    """餐饮订单管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Order.objects.order_by("id"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "order.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@permission_required('hotelapp.add_order', raise_exception=True)
@module_gate("order")
def insert_order(request):
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
@permission_required('hotelapp.delete_order', raise_exception=True)
@module_gate("order")
def delete_order(request):
    """餐饮订单「冲账」：打已冲账标记并记录冲账人/时间，不再物理删除（值班经理权限）"""
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
@permission_required('hotelapp.view_order', raise_exception=True)
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
@permission_required('hotelapp.view_accommodation', raise_exception=True)
def show_accomodation(request):
    """客房订单管理列表（图5）：分页 15 条/页"""
    paginator = Paginator(Accommodation.objects.order_by("id"), 15)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "accomodation.html", {"people_list": page.object_list, "page_obj": page})


@staff_required
@permission_required('hotelapp.add_accommodation', raise_exception=True)
def insert_accomodation(request):
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
@permission_required('hotelapp.delete_accommodation', raise_exception=True)
def delete_accomodation(request):
    """客房订单「冲账」：打已冲账标记、释放房间、记录冲账人/时间，不再物理删除"""
    if request.method == "GET":
        reason = (request.GET.get("reason") or "").strip()
        if not reason:
            return HttpResponse("冲账必须填写原因（高危操作需留痕）", status=400)
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=request.GET.get("id"))
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
@permission_required('hotelapp.view_accommodation', raise_exception=True)
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
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def extend_stay(request):
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
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def change_room(request):
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
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def express_checkout(request):
    """快捷离店：按账单聚合自动结算（免查房），后续流程与普通退房一致"""
    if request.method == "POST":
        try:
            with transaction.atomic():
                item = Accommodation.objects.select_for_update().get(id=request.POST.get("id"), 状态="已入住")
                # 应收 = 房费 + 消费；已收 = 押金 + 收款（不含本次自动结算）
                due = 0.0
                for f in Folio.objects.filter(order=item, 类型__in=("房费", "消费")):
                    try:
                        due += float(f.金额)
                    except (TypeError, ValueError):
                        pass
                if due <= 0:
                    due = float(item.客房编号.价格 or 0)
                paid = 0.0
                for f in Folio.objects.filter(order=item, 类型__in=("押金", "收款")):
                    try:
                        paid += float(f.金额)
                    except (TypeError, ValueError):
                        pass
                # E-05/MGR-03：已有资金往来与应收对不上（多收/部分收）= 账单存在差异，
                # 快捷退房不得自动抹平——转普通结账人工核对或经理处理
                if paid != 0 and abs(due - paid) > 0.001:
                    return HttpResponse(
                        f"账单存在未核平差异（应收 {due:.2f} / 已收 {paid:.2f}），"
                        "请转普通结账核对或联系值班经理处理", status=400)
                amount = round(due - paid, 2)
                if amount < 0:
                    return HttpResponse(
                        f"客人多付 {abs(amount):.2f} 元，请走退款流程后离店", status=400)
                room = item.客房编号
                state_machine.require_order_transition(item, "已退房")
                item.退房时间 = _today()
                item.save()
                events.publish("checked_out", order_id=item.id, room_no=room.房间号,
                               amount=f"{amount:.2f}", operator=request.user.username, date=_today())
                if amount > 0:
                    Folio.objects.create(order=item, 类型="收款", 金额=f"{amount:.2f}",
                                         事由="快捷离店自动结算", 日期=_today(), 经手人=request.user.username)
                item.收款金额 = f"{amount:.2f}"
                item.save()
        except ValueError as e:
            return HttpResponse(str(e), status=400)
        except Accommodation.DoesNotExist:
            return HttpResponse("订单不存在或状态已变更", status=400)
        return JsonResponse({"ok": True, "amount": f"{amount:.2f}"})
    return HttpResponse("非法请求", status=400)


@staff_required
@permission_required('hotelapp.add_accommodation', raise_exception=True)
def walkin_create(request):
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
        if not request.user.has_perm("hotelapp.no_show_handle"):
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
    if not request.user.has_perm("hotelapp.view_accommodation"):
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
            if not request.user.has_perm("hotelapp.room_release_ready"):
                return HttpResponse("净房放行需要值班经理/店长确认", status=403)
        elif (room.房间状态, target) not in simple and not is_manager:
            return HttpResponse("该状态流转需要值班经理操作", status=403)
        room.房间状态 = target
        room.save()
        return JsonResponse({"ok": True, "status": target})
    return HttpResponse("非法请求", status=400)




# 通用业务数据导出（图5 导出数据按钮）：模块白名单 + 查看权限校验
EXPORT_MODULES = {
    "client": ("client", Client, ["顾客编号", "姓名", "身份证号"]),
    "food": ("food", Food, ["菜品编号", "菜名", "价格", "厨师编号"]),
    "room": ("room", Room, ["房间号", "客房类型", "价格", "负责人编号", "房间状态"]),
    "park": ("park", Park, ["车位编号", "当前状态", "车辆牌号"]),
    "vip": ("vip", Vip, ["会员编号", "姓名", "会员等级", "联系方式"]),
    "order": ("order", Order, ["订单编号", "顾客编号", "菜品编号", "服务员编号", "桌号", "状态"]),
    "accommodation": ("accommodation", Accommodation,
                      ["订单编号", "顾客编号", "客房编号", "入住时间", "状态", "收款金额"]),
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
    model = info[1]
    if not request.user.has_perm(f"hotelapp.view_{module}"):
        return HttpResponse("没有导出权限", status=403)
    # 敏感字段（顾客身份证/会员联系方式）不随查看权限导出：需独立「导出敏感数据」授权
    if module in ("client", "vip") and not request.user.has_perm("hotelapp.export_pii"):
        return HttpResponse("导出含敏感字段的数据需要「导出敏感数据」权限", status=403)
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
    for row in model.objects.order_by(*[model._meta.pk.name]).values_list(*field_names):
        row = list(row)
        if module == "client":
            # V2 §7.1：身份证完整值导出默认禁止（CSV 输出遮罩值，紧急合规导出待审批流程）
            row[2] = (row[2] or "")[:2] + "*" * max(0, len(row[2] or "") - 4) + (row[2] or "")[-2:] if len(row[2] or "") >= 6 else row[2]
        elif module == "vip":
            row[3] = (row[3] or "")[:3] + "****" + (row[3] or "")[-4:] if len(row[3] or "") >= 8 else row[3]
        writer.writerow([str(x) for x in row])
    return response

# ---------- 敏感字段 reveal（V2 §7.3：完整值按需显示 + 理由审计） ----------
@staff_required
@permission_required('hotelapp.change_client', raise_exception=True)
def client_reveal_id(request):
    """取顾客身份证完整值：仅限持修改权岗位（前台/值班经理/店长），
    用于行内核验/修正；每次读取写审计日志（记录操作者与对象，不记录敏感原文）。"""
    if request.method != "POST":
        return HttpResponse("非法请求", status=400)
    try:
        item = Client.objects.get(顾客id=request.POST.get("id"))
    except (Client.DoesNotExist, ValueError):
        return HttpResponse("顾客不存在", status=404)
    logging.getLogger("hotel.audit").info(
        "PII reveal 顾客身份证 user=%s 顾客id=%s 用途=行内编辑/核验",
        request.user.username, item.顾客id)
    return JsonResponse({"identify": item.身份证号 or ""})


@staff_required
@permission_required('hotelapp.change_vip', raise_exception=True)
def vip_reveal_tel(request):
    """取会员联系方式完整值：仅限持修改权岗位；读取写审计。"""
    if request.method != "POST":
        return HttpResponse("非法请求", status=400)
    try:
        item = Vip.objects.get(会员编号=request.POST.get("id"))
    except (Vip.DoesNotExist, ValueError):
        return HttpResponse("会员不存在", status=404)
    logging.getLogger("hotel.audit").info(
        "PII reveal 会员联系方式 user=%s 会员编号=%s 用途=行内编辑/服务",
        request.user.username, item.会员编号)
    return JsonResponse({"tel": item.联系方式 or ""})


# ---------- 登录 / 注册 / 退出 / 主界面 ----------
def _has_role(user, *names):
    """判断用户是否拥有指定业务角色（超级管理员视为拥有全部角色）"""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=names).exists()


def _role_name(user):
    g = user.groups.first()
    return g.name if g else ("超级管理员" if user.is_superuser else "员工")


def _today():
    return datetime.date.today().strftime("%Y-%m-%d")


def login_view(request):
    """统一登录页：登录后进入主界面（或按 next 返回原页面）"""
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/home/")
    error = None
    next_url = request.GET.get("next", "") or request.POST.get("next", "")
    if request.method == "POST":
        user = authenticate(request, username=request.POST.get("username"),
                            password=request.POST.get("password"))
        if user is None:
            error = "用户名或密码错误"
        elif not user.is_active:
            error = "该账号已被停用"
        elif not user.is_staff:
            error = "该账号没有后台访问权限，请联系管理员"
        else:
            auth_login(request, user)
            next_url = request.POST.get("next") or "/home/"
            if not next_url.startswith("/"):
                next_url = "/home/"
            return redirect(next_url)
    return render(request, "login.html", {"error": error, "next": next_url})


def register_view(request):
    """注册 = 顾客预订账号（P0 安全止血：不再发放 is_staff/员工组）。

    员工后台账号由管理员在后台创建并授权（或 bind_staff_user 绑定），
    公开注册通道不得产生任何员工后台身份。"""
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("/home/")
    error = None
    form = {"username": "", "fullname": "", "tel": ""}
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        password2 = request.POST.get("password2") or ""
        fullname = (request.POST.get("fullname") or "").strip()
        tel = (request.POST.get("tel") or "").strip()
        form = {"username": username, "fullname": fullname, "tel": tel}
        if not re.match(r"^[\w.@+-]{3,30}$", username):
            error = "用户名须为 3-30 位字母/数字/下划线"
        elif not fullname:
            error = "姓名不能为空"
        elif len(password) < 6:
            error = "密码至少 6 位"
        elif password != password2:
            error = "两次输入的密码不一致"
        elif tel and not re.match(r"^1\d{10}$", tel):
            error = "手机号格式不正确"
        elif User.objects.filter(username=username).exists():
            error = "该用户名已被注册"
        else:
            user = User.objects.create_user(username=username, password=password, first_name=fullname)
            # 决不发放员工后台身份（is_staff / Group 均不动）——公开注册只产顾客账号
            auth_login(request, user)
            messages.success(request, "注册成功，可用于顾客预订；员工账号请联系管理员开通")
            return redirect("/booking/")
    return render(request, "register.html", {"error": error, "form": form})


@login_required
def logout_view(request):
    auth_logout(request)
    return redirect("/login/")


@staff_required
def home(request):
    """主界面：按当前用户权限展示功能模块卡片"""
    user = request.user
    entries = [
        ("入住管理", "预订确认 · 入住登记 · 退房结算", "/checkin/", "hotelapp.view_accommodation"),
        ("客房预订", "订单状态与预订管理", "/show_accomodation/", "hotelapp.view_accommodation"),
        ("房间管理", "房间状态 · 清洁 · 维修", "/show_room/", "hotelapp.view_room"),
        ("顾客管理", "顾客信息维护与查询", "/show_client/", "hotelapp.view_client"),
        ("会员管理", "会员信息维护与查询", "/show_vip/", "hotelapp.view_vip"),
        *([("餐厅管理", "菜品信息维护", "/show_food/", "hotelapp.view_food")] if MODULE_REGISTRY["food"] else []),
        *([("餐厅订单", "餐饮订单录入与查询", "/show_order/", "hotelapp.view_order")] if MODULE_REGISTRY["order"] else []),
        *([("停车场管理", "车位与车辆管理", "/show_park/", "hotelapp.view_park")] if MODULE_REGISTRY["park"] else []),
        ("财务管理", "流水 · 收支 · 报表分析", "/finance/", None),
        ("员工管理", "员工信息维护", "/index/", "hotelapp.view_staff"),
    ]
    cards = []
    for title, desc, url, perm in entries:
        if perm is None:
            if not _has_role(user, "财务", "店长", "值班经理"):
                continue
        elif not user.has_perm(perm):
            continue
        cards.append({"title": title, "desc": desc, "url": url})
    stats = {
        "pending": Accommodation.objects.filter(状态="待确认").count(),
        "confirmed": Accommodation.objects.filter(状态="已确认").count(),
        "living": Accommodation.objects.filter(状态="已入住").count(),
        "cleaning": Room.objects.filter(房间状态__in=("待清洁", "清洁中", "待检查")).count(),
        "revenue": None,
    }
    can_see_finance = _has_role(user, "财务", "店长", "值班经理")
    if can_see_finance:
        total = 0.0
        for r in FinanceRecord.objects.filter(类型="收入"):
            try:
                total += float(r.金额)
            except (TypeError, ValueError):
                pass
        stats["revenue"] = f"{total:.2f}"

    # ---------- 数据仪表盘（结合本项目的客房/顾客/会员/财务数据） ----------
    room_status_counts = [
        {"name": r["房间状态"] or "空闲", "value": r["n"]}
        for r in Room.objects.values("房间状态").annotate(n=Count("房间号")).order_by("-n")
    ]
    room_type_inventory = [
        {"name": r["客房类型"], "total": r["total"], "available": r["available"]}
        for r in Room.objects.values("客房类型").annotate(
            total=Count("房间号"),
            available=Count("房间号", filter=Q(房间状态="空闲")),
        ).order_by("客房类型")
    ]
    # 预约预警：今日待入住 / 已过期未入住的预订
    warnings = []
    today = _today()
    for b in Accommodation.objects.filter(状态__in=("待确认", "已确认")).order_by("入住时间"):
        if not b.入住时间:
            continue
        level = "今日待入住" if b.入住时间 == today else ("过期未入住" if b.入住时间 < today else None)
        if level:
            warnings.append({"level": level, "guest": b.顾客id.姓名 or "未知顾客",
                             "tel": b.联系电话 or "-", "date": b.入住时间})
    client_total = Client.objects.count()
    client_registered = (Client.objects.exclude(身份证号__isnull=True)
                                   .exclude(身份证号="").count())
    vip_counts = [
        {"name": r["会员等级"] or "普通会员", "value": r["n"]}
        for r in Vip.objects.values("会员等级").annotate(n=Count("会员编号")).order_by("-n")
    ]
    show_fnb = MODULE_REGISTRY["food"] or MODULE_REGISTRY["order"]
    food_count = Food.objects.count() if show_fnb else 0
    recent_orders = [
        {"dish": o.菜品编号.菜名, "guest": o.顾客编号.姓名 or "未知顾客", "table": o.桌号}
        for o in (Order.objects.order_by("-id")[:5] if show_fnb else [])
    ]

    # 消费统计（收入来自财务流水；已结算房费来自退房订单）——仅财务及以上可见
    finance_dash = None
    if can_see_finance:
        def _sum_ftype(begin, end, ftype):
            total = 0.0
            for r in FinanceRecord.objects.filter(类型=ftype):
                if begin <= (r.日期 or "") <= end:
                    try:
                        total += float(r.金额)
                    except (TypeError, ValueError):
                        pass
            return total
        week_begin = (datetime.datetime.strptime(today, "%Y-%m-%d")
                      - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
        year_begin = today[:4] + "-01-01"
        settled = 0.0
        for a in Accommodation.objects.filter(状态="已退房"):
            if a.收款金额:
                try:
                    settled += float(a.收款金额)
                except (TypeError, ValueError):
                    pass
        finance_dash = {
            "today_in": f"{_sum_ftype(today, today, '收入'):.2f}",
            "today_out": f"{_sum_ftype(today, today, '支出'):.2f}",
            "week_in": f"{_sum_ftype(week_begin, today, '收入'):.2f}",
            "week_out": f"{_sum_ftype(week_begin, today, '支出'):.2f}",
            "year_in": f"{_sum_ftype(year_begin, today, '收入'):.2f}",
            "year_out": f"{_sum_ftype(year_begin, today, '支出'):.2f}",
            "settled": f"{settled:.2f}",
        }

    ctx = {
        "cards": cards, "stats": stats, "role": _role_name(user), "today": today,
        "room_status_counts": room_status_counts,
        "room_type_inventory": room_type_inventory,
        "warnings": warnings,
        "client_dash": {"total": client_total, "registered": client_registered,
                        "missing": client_total - client_registered},
        "vip_counts": vip_counts,
        "food_count": food_count,
        "recent_orders": recent_orders,
        "finance_dash": finance_dash,
        "show_finance": can_see_finance,
        "show_fnb": show_fnb,
    }
    return render(request, "home.html", ctx)


# ---------- 顾客预订页（对外，无需登录） ----------
def booking_page(request):
    """顾客自助预订：查看房型与价格，提交预订申请（状态=预订，待前台确认）"""
    error = None
    ok = False
    types = {}
    for r in Room.objects.order_by("房间号"):
        t = types.setdefault(r.客房类型, {"price": int(r.价格 or 0), "count": 0})
        t["count"] += 1
        t["price"] = min(t["price"], int(r.价格 or 0))
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        tel = (request.POST.get("tel") or "").strip()
        room_type = (request.POST.get("room_type") or "").strip()
        date = (request.POST.get("date") or "").strip()
        if not re.match(r"^[\u4e00-\u9fa5A-Za-z·]{2,10}$", name):
            error = "请填写 2-10 位中英文姓名"
        elif not re.match(r"^1\d{10}$", tel):
            error = "请填写 11 位手机号（1 开头）"
        elif not Room.objects.filter(客房类型=room_type).exists():
            error = "请选择房间类型"
        else:
            try:
                datetime.datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                error = "请选择入住日期"
            else:
                if date < _today():
                    error = "入住日期不能早于今天"
        if error is None and Accommodation.objects.filter(联系电话=tel, 入住时间=date, 状态="待确认").exists():
            error = "该手机号在此日期已有预订，请勿重复提交"
        if error is None:
            room = (Room.objects.filter(客房类型=room_type, 房间状态="空闲").first()
                    or Room.objects.filter(客房类型=room_type).first())
            client = Client.objects.create(姓名=name, 身份证号=None)
            last = Accommodation.objects.order_by("-id").first()
            new_id = (last.id + 1) if last else 1
            Accommodation.objects.create(
                id=new_id, 顾客id=client, 客房编号=room, 入住时间=date,
                状态="待确认", 联系电话=tel,
            )
            ok = True
    return render(request, "booking.html", {"error": error, "ok": ok, "types": types, "today": _today()})


# ---------- 入住管理 ----------
@staff_required
@permission_required('hotelapp.view_accommodation', raise_exception=True)
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
        "today": _today(), "can_void": request.user.has_perm("hotelapp.reverse_checkout"),
    })


@staff_required
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def cancel_booking(request):
    """取消预订（待确认/已确认 → 已取消）：发布事件释放房间"""
    if request.method == "POST":
        try:
            item = Accommodation.objects.get(id=request.POST.get("id"))
        except (Accommodation.DoesNotExist, ValueError):
            return HttpResponse("订单不存在", status=400)
        if item.状态 not in ("待确认", "已确认"):
            return HttpResponse("只有待确认/已确认的订单可以取消", status=400)
        prev = item.状态
        item.状态 = "已取消"
        item.save()
        events.publish("booking_cancelled", order_id=item.id,
                       room_no=item.客房编号.房间号, prev_status=prev)
        return JsonResponse({"ok": True})
    return HttpResponse("非法请求", status=400)


@staff_required
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def checkin_confirm(request):
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
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def checkin_in(request):
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
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def checkout(request):
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
    """反结账：已退房 → 已入住，删除对应收入流水（需「反结账」权限：值班经理/店长）"""
    if request.method == "POST":
        if not request.user.has_perm("hotelapp.reverse_checkout"):
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
    if not request.user.has_perm("hotelapp.finance_access"):
        raise PermissionDenied
    ctx = _finance_context()
    ctx["can_add"] = _has_role(request.user, "财务")
    ctx["today"] = _today()
    return render(request, "finance.html", ctx)


@staff_required
def finance_add(request):
    """新增收支流水（仅财务）；校验失败时回显表单并保留已填内容"""
    if not _has_role(request.user, "财务"):
        return HttpResponse("只有财务可以录入收支", status=403)
    if request.method != "POST":
        return redirect("/finance/")
    ftype = request.POST.get("type")
    amount = (request.POST.get("amount") or "").strip()
    reason = (request.POST.get("reason") or "").strip()
    date = (request.POST.get("date") or "").strip()
    error, efield = None, None
    if ftype not in ("收入", "支出"):
        error, efield = "类型必须是收入或支出", "type"
    elif not reason:
        error, efield = "事由不能为空", "reason"
    elif not re.match(r"^\d{1,9}(\.\d{1,2})?$", amount):
        error, efield = "金额必须是数字，整数最多9位、最多两位小数", "amount"
    else:
        try:
            datetime.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            error, efield = "日期不正确", "date"
    if error is None:
        FinanceRecord.objects.create(类型=ftype, 金额=amount, 事由=reason,
                                     日期=date, 经手人=request.user.username)
        messages.success(request, "收支记录已保存")
        return redirect("/finance/")
    form = request.POST.copy()
    if efield:
        form[efield] = ""
    ctx = _finance_context()
    ctx.update({"can_add": True, "today": _today(), "error": error, "form": form})
    return render(request, "finance.html", ctx)


# ---------- 夜审（Night Audit） ----------
@staff_required
def night_audit(request):
    """夜审工作台：在住账务核对 + 未到(No-Show)处理 + 营业日结（需「执行夜审」权限）"""
    if not request.user.has_perm("hotelapp.night_audit_execute"):
        raise PermissionDenied
    living = Accommodation.objects.filter(状态="已入住").order_by("id")
    expired = Accommodation.objects.filter(状态__in=("待确认", "已确认"), 入住时间__lt=_today())
    records = NightAudit.objects.order_by("-id")[:10]
    return render(request, "nightaudit.html", {
        "living": living, "expired": expired, "records": records,
        "today": _today(),
    })


@staff_required
def night_audit_run(request):
    """执行夜审：No-Show 处理 → 房费过租 → 记录日结（需「执行夜审」权限）"""
    if request.method == "POST":
        if not request.user.has_perm("hotelapp.night_audit_execute"):
            return HttpResponse("执行夜审需要值班经理/店长授权", status=403)
        # 幂等守卫（AUTH-16/E-17）：同一营业日只能夜审一次——
        # 网络重试/重复提交不得重复产生过租收入或未到处理
        if NightAudit.objects.filter(营业日期=_today()).exists():
            return HttpResponse(f"营业日 {_today()} 已完成夜审，不能重复执行；"
                                "如需纠错请走营业日重开审批流程", status=400)
        expired = Accommodation.objects.filter(状态__in=("待确认", "已确认"), 入住时间__lt=_today())
        living = Accommodation.objects.filter(状态="已入住")
        total = 0.0
        noshow = 0
        try:
            # 事务化（E-18）：No-Show/过租/日结要么全部成功，要么整体回滚
            with transaction.atomic():
                for item in expired:
                    events.publish("no_show", order_id=item.id, room_no=item.客房编号.房间号)
                    item.状态 = "未到"
                    item.save()
                    noshow += 1
                for item in living:
                    amount = str(item.客房编号.价格)
                    FinanceRecord.objects.create(类型="收入", 金额=amount,
                                                 事由=f"房费过租（订单{item.id}）",
                                                 关联订单=str(item.id), 日期=_today(),
                                                 经手人=request.user.username)
                    try:
                        total += float(amount)
                    except (TypeError, ValueError):
                        pass
                NightAudit.objects.create(营业日期=_today(), 在住单数=living.count(),
                                          过租总额=f"{total:.2f}", 未到处理数=noshow,
                                          操作人=request.user.username,
                                          执行时间=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        except Exception:
            return HttpResponse("夜审执行失败：本次操作已整体回滚，请检查数据后重试", status=500)
        messages.success(request, f"夜审完成：在住 {living.count()} 单，过租 {total:.2f} 元，未到处理 {noshow} 单")
        return redirect("/nightaudit/")
    return HttpResponse("非法请求", status=400)


# ---------- 系统环境配置信息（图4） ----------
@staff_required
def system_info(request):
    """个人与系统环境信息页：管理员信息 + 运行环境 + 会话生命周期"""
    user = request.user
    role = _role_name(user)
    last_login = user.last_login or timezone.now()
    session_age = int(settings.SESSION_COOKIE_AGE)
    expire_at = last_login + datetime.timedelta(seconds=session_age)
    remain_seconds = max(0, int((expire_at - timezone.now()).total_seconds()))
    hours, rem = divmod(remain_seconds, 3600)
    minutes = rem // 60
    browser_lang = (request.META.get("HTTP_ACCEPT_LANGUAGE") or "-").split(",")[0]
    ctx = {
        "tz": settings.TIME_ZONE,
        "admin_no": f"AD-{user.id:08d}",
        "full_name": user.first_name or user.username,
        "role": role,
        "api_base": request.build_absolute_uri("/api/v1/"),
        "run_mode": "development" if settings.DEBUG else "production",
        "browser_lang": browser_lang,
        "sys_lang": settings.LANGUAGE_CODE,
        "login_at": timezone.localtime(last_login).strftime("%Y-%m-%d %H:%M:%S"),
        "expire_at": timezone.localtime(expire_at).strftime("%Y-%m-%d %H:%M:%S"),
        "remain": f"{hours}h {minutes}m",
    }
    return render(request, "system.html", ctx)
