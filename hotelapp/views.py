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

import datetime
import re

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import User, Group
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect

from .models import (Staff, Room, Client, Food, Vip, Park, Order, Accommodation,
                     FinanceRecord, ROOM_STATUS, ORDER_STATUS)


def staff_required(view):
    """登录 + 员工身份双重校验；登录页统一用前台 /login/"""
    return login_required(staff_member_required(view, login_url='/login/'))


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
    return render(request, "index.html", {"people_list": Staff.objects.order_by("员工id")})


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
    return render(request, "html1.html", {"people_list": Client.objects.order_by("顾客id")})


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
    rows, searched = _search(Client, "顾客id", CLIENT_SEARCH_FIELDS, request)
    return render(request, "html1.html", {
        "people_list": Client.objects.order_by("顾客id"),
        "clients": rows,
        "searched": searched,
    })


# ---------- 餐厅信息管理 ----------
@staff_required
@permission_required('hotelapp.view_food', raise_exception=True)
def show_food(request):
    return render(request, "html4.html", {"people_list": Food.objects.order_by("菜品编号")})


@staff_required
@permission_required('hotelapp.add_food', raise_exception=True)
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
def delete_food(request):
    if request.method == "GET":
        try:
            Food.objects.filter(菜品编号=request.GET.get("id")).delete()
        except (ValueError, IntegrityError):
            return HttpResponse("删除失败：该菜品已被餐饮订单引用", status=400)
    return render(request, "html4.html", {"people_list": Food.objects.order_by("菜品编号")})


@staff_required
@permission_required('hotelapp.change_food', raise_exception=True)
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
def search_food(request):
    rows, searched = _search(Food, "菜品编号", FOOD_SEARCH_FIELDS, request)
    return render(request, "html4.html", {
        "people_list": Food.objects.order_by("菜品编号"),
        "food": rows,
        "searched": searched,
    })


# ---------- 会员信息管理 ----------
@staff_required
@permission_required('hotelapp.view_vip', raise_exception=True)
def show_vip(request):
    return render(request, "vip.html", {"people_list": Vip.objects.order_by("会员编号")})


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
    rows, searched = _search(Vip, "会员编号", VIP_SEARCH_FIELDS, request)
    return render(request, "vip.html", {
        "people_list": Vip.objects.order_by("会员编号"),
        "vips": rows,
        "searched": searched,
    })


# ---------- 停车场信息管理 ----------
@staff_required
@permission_required('hotelapp.view_park', raise_exception=True)
def show_park(request):
    return render(request, "park.html", {"people_list": Park.objects.order_by("车位编号")})


@staff_required
@permission_required('hotelapp.add_park', raise_exception=True)
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
def delete_park(request):
    if request.method == "GET":
        try:
            Park.objects.filter(车位编号=request.GET.get("id")).delete()
        except ValueError:
            return HttpResponse("删除失败：车位编号非法", status=400)
    return render(request, "park.html", {"people_list": Park.objects.order_by("车位编号")})


@staff_required
@permission_required('hotelapp.change_park', raise_exception=True)
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
def search_park(request):
    rows, searched = _search(Park, "车位编号", PARK_SEARCH_FIELDS, request)
    return render(request, "park.html", {
        "people_list": Park.objects.order_by("车位编号"),
        "parks": rows,
        "searched": searched,
    })


# ---------- 客房信息管理 ----------
@staff_required
@permission_required('hotelapp.view_room', raise_exception=True)
def show_room(request):
    return render(request, "room.html", {"people_list": Room.objects.order_by("房间号")})


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
        staff = _get_staff(request.POST.get("waiter"), "服务员")
        if staff is None:
            return HttpResponse("修改失败：服务员编号必须存在（职务为服务员）", status=400)
        try:
            updated = Room.objects.filter(房间号=rid).update(客房类型=room_type, 价格=price, 负责人编号=staff)
        except ValueError:
            return HttpResponse("修改失败：房间号和价格必须是数字", status=400)
        if not updated:
            return HttpResponse("修改失败：客房不存在", status=400)
    return render(request, "room.html", {"people_list": Room.objects.order_by("房间号")})


@staff_required
@permission_required('hotelapp.view_room', raise_exception=True)
def search_room(request):
    rows, searched = _search(Room, "房间号", ROOM_SEARCH_FIELDS, request)
    return render(request, "room.html", {
        "people_list": Room.objects.order_by("房间号"),
        "room": rows,
        "searched": searched,
    })


# ---------- 餐饮订单管理 ----------
@staff_required
@permission_required('hotelapp.view_order', raise_exception=True)
def show_order(request):
    return render(request, "order.html", {"people_list": Order.objects.order_by("id")})


@staff_required
@permission_required('hotelapp.add_order', raise_exception=True)
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
def delete_order(request):
    """餐饮订单「冲账」：打已冲账标记并记录冲账人/时间，不再物理删除（值班经理权限）"""
    if request.method == "GET":
        try:
            item = Order.objects.get(id=request.GET.get("id"))
        except (Order.DoesNotExist, ValueError):
            return HttpResponse("订单不存在", status=400)
        if item.状态 == "已冲账":
            return HttpResponse("该订单已冲账", status=400)
        item.状态 = "已冲账"
        item.冲账人 = request.user.username
        item.冲账时间 = _today()
        item.save()
    return render(request, "order.html", {"people_list": Order.objects.order_by("id")})


@staff_required
@permission_required('hotelapp.view_order', raise_exception=True)
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
    return render(request, "accomodation.html", {"people_list": Accommodation.objects.order_by("id")})


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
        try:
            item = Accommodation.objects.get(id=request.GET.get("id"))
        except (Accommodation.DoesNotExist, ValueError):
            return HttpResponse("订单不存在", status=400)
        if item.状态 == "已冲账":
            return HttpResponse("该订单已冲账", status=400)
        prev = item.状态 or "预订"
        item.状态 = "已冲账"
        item.冲账人 = request.user.username
        item.冲账时间 = _today()
        item.save()
        room = item.客房编号
        if prev == "已入住":
            room.房间状态 = "清洁中"
        elif prev == "预订" and room.房间状态 == "已预订":
            room.房间状态 = "空闲"
        room.save()
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
        simple = {("空闲", "清洁中"), ("清洁中", "空闲")}
        if (room.房间状态, target) not in simple and not is_manager:
            return HttpResponse("该状态流转需要值班经理操作", status=403)
        room.房间状态 = target
        room.save()
        return JsonResponse({"ok": True, "status": target})
    return HttpResponse("非法请求", status=400)


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
    """注册即「前台」角色：注册成功自动登录进入主界面"""
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
            user.is_staff = True
            user.save()
            user.groups.add(Group.objects.get(name="前台"))
            auth_login(request, user)
            messages.success(request, "注册成功，欢迎使用酒店管理系统")
            return redirect("/home/")
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
        ("餐厅管理", "菜品信息维护", "/show_food/", "hotelapp.view_food"),
        ("餐厅订单", "餐饮订单录入与查询", "/show_order/", "hotelapp.view_order"),
        ("停车场管理", "车位与车辆管理", "/show_park/", "hotelapp.view_park"),
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
        "pending": Accommodation.objects.filter(状态="预订").count(),
        "living": Accommodation.objects.filter(状态="已入住").count(),
        "cleaning": Room.objects.filter(房间状态="清洁中").count(),
        "revenue": None,
    }
    if _has_role(user, "财务", "店长", "值班经理"):
        total = 0.0
        for r in FinanceRecord.objects.filter(类型="收入"):
            try:
                total += float(r.金额)
            except (TypeError, ValueError):
                pass
        stats["revenue"] = f"{total:.2f}"
    return render(request, "home.html", {"cards": cards, "stats": stats, "role": _role_name(user)})


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
        if error is None and Accommodation.objects.filter(联系电话=tel, 入住时间=date, 状态="预订").exists():
            error = "该手机号在此日期已有预订，请勿重复提交"
        if error is None:
            room = (Room.objects.filter(客房类型=room_type, 房间状态="空闲").first()
                    or Room.objects.filter(客房类型=room_type).first())
            client = Client.objects.create(姓名=name, 身份证号=None)
            last = Accommodation.objects.order_by("-id").first()
            new_id = (last.id + 1) if last else 1
            Accommodation.objects.create(
                id=new_id, 顾客id=client, 客房编号=room, 入住时间=date,
                状态="预订", 联系电话=tel,
            )
            if room.房间状态 == "空闲":
                room.房间状态 = "已预订"
                room.save()
            ok = True
    return render(request, "booking.html", {"error": error, "ok": ok, "types": types, "today": _today()})


# ---------- 入住管理 ----------
@staff_required
@permission_required('hotelapp.view_accommodation', raise_exception=True)
def checkin_manage(request):
    """入住管理工作台：待入住预订 / 在住列表 / 近期退房"""
    rooms = Room.objects.order_by("房间号")
    pending = Accommodation.objects.filter(状态="预订").order_by("id")
    living = Accommodation.objects.filter(状态="已入住").order_by("id")
    done = Accommodation.objects.filter(状态="已退房").order_by("-id")[:10]
    return render(request, "checkin.html", {
        "rooms": rooms, "pending": pending, "living": living, "done": done,
        "today": _today(), "can_void": _has_role(request.user, "值班经理"),
    })


@staff_required
@permission_required('hotelapp.change_accommodation', raise_exception=True)
def checkin_confirm(request):
    """预订 → 已入住：分配/确认房间，补录顾客身份证"""
    if request.method == "POST":
        oid = request.POST.get("id")
        room_no = request.POST.get("room")
        idcard = (request.POST.get("idcard") or "").strip()
        try:
            item = Accommodation.objects.get(id=oid, 状态="预订")
        except (Accommodation.DoesNotExist, ValueError):
            return HttpResponse("订单不存在或状态已变更", status=400)
        try:
            room = Room.objects.get(房间号=room_no)
        except (Room.DoesNotExist, ValueError, TypeError):
            return HttpResponse("房间不存在", status=400)
        if room.房间状态 not in ("空闲", "已预订"):
            return HttpResponse(f"该房间当前不可入住（{room.房间状态}）", status=400)
        if idcard:
            if Client.objects.filter(身份证号=idcard).exclude(顾客id=item.顾客id.顾客id).exists():
                return HttpResponse("该身份证号已登记，证件号不能重复", status=400)
            cli = item.顾客id
            cli.身份证号 = idcard
            cli.save()
        item.状态 = "已入住"
        item.入住时间 = _today()
        item.save()
        room.房间状态 = "已入住"
        room.save()
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
        item.状态 = "已退房"
        item.收款金额 = amount
        item.退房时间 = _today()
        item.save()
        room = item.客房编号
        room.房间状态 = "清洁中"
        room.save()
        FinanceRecord.objects.create(类型="收入", 金额=amount,
                                     事由=f"客房结算（订单{item.id}）",
                                     关联订单=str(item.id), 日期=_today(),
                                     经手人=request.user.username)
        return JsonResponse({"ok": True})
    return HttpResponse("非法请求", status=400)


@staff_required
def uncheckout(request):
    """反结账（值班经理及以上）：已退房 → 已入住，删除对应收入流水"""
    if request.method == "POST":
        if not _has_role(request.user, "值班经理"):
            return HttpResponse("只有值班经理可以反结账", status=403)
        try:
            item = Accommodation.objects.get(id=request.POST.get("id"), 状态="已退房")
        except (Accommodation.DoesNotExist, ValueError):
            return HttpResponse("订单不存在或状态已变更", status=400)
        item.状态 = "已入住"
        item.收款金额 = None
        item.退房时间 = None
        item.save()
        room = item.客房编号
        room.房间状态 = "已入住"
        room.save()
        FinanceRecord.objects.filter(关联订单=str(item.id), 类型="收入").delete()
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
    return {
        "records": records, "day_rows": day_rows,
        "total_in": f"{total_in:.2f}", "total_out": f"{total_out:.2f}", "total": f"{total:.2f}",
        "room_rev": f"{room_rev:.2f}", "food_rev": f"{food_rev:.2f}",
    }


@staff_required
def finance_manage(request):
    """财务管理：流水列表 + 新增收支 + 报表分析（财务/店长/值班经理）"""
    if not _has_role(request.user, "财务", "店长", "值班经理"):
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
