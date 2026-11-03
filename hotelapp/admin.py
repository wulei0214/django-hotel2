"""Django Admin 安全边界（RBAC V2 P0-2）。

* 隐藏模块（餐饮/停车）不注册 Admin——业务页与 Admin 入口同时关闭（评审 P0-2-1），
  直接访问 /admin/hotelapp/food/ 等返回 404，无法绕过 MODULE_REGISTRY 门禁
* 业务记录禁止 Admin 物理删除——冲账/状态流转必须走业务视图（软流程），Admin 删除
  按钮一律关闭（评审 P0-2-3：delete_* 业务权限不能等价于物理删除权）
* 订单状态/冲账留痕字段只读——Admin 不能绕过业务状态机直接改状态（评审 P0-2-2）
* 敏感字段（月薪/身份证/联系方式）在 admin 的可见性由模型权限（view_*）门控，
  字段级遮罩待 D1-D5 决策后与业务页面统一实施（P1-1）
"""
from django.contrib import admin
from hotelapp.models import *

admin.site.site_header = '酒店管理系统'
admin.site.site_title = '酒店管理系统'
admin.site.index_title = '酒店管理系统'


# ---------- P0-2 安全边界：Admin 禁止物理删除 / 订单状态只读 ----------
class NoAdminDeleteMixin:
    """业务记录不允许从 Django Admin 物理删除。

    冲账是软流程（记冲账人/时间，不删数据）；Admin 的 delete_* 模型权限
    不能等价于物理删除权。物理清理属于运维通道（manage.py/SQL，先备份）。
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OrderStatusReadonlyMixin:
    """订单状态与冲账留痕只读：Admin 编辑不得绕过业务状态机（state_machine）。"""

    status_readonly = ("状态", "冲账人", "冲账时间")

    def get_readonly_fields(self, request, obj=None):
        # 新建时状态可设默认值；编辑已存在订单时状态与冲账留痕只读
        return self.status_readonly if obj else ()


class AccommodationAdmin(OrderStatusReadonlyMixin, NoAdminDeleteMixin, admin.ModelAdmin):
    '''客房订单：状态/冲账留痕只读，禁止物理删除'''
    list_display = ("顾客id", "客房编号", "状态", "masked_phone", "入住时间", "退房时间",
                    "收款金额", "押金", "冲账人")
    list_filter = ("状态", "客房编号", "入住时间")
    exclude = ("联系电话",)
    list_per_page = 15

    @admin.display(description="联系电话（脱敏）")
    def masked_phone(self, obj):
        value = (obj.联系电话 or "").strip()
        return value[:3] + "****" + value[-4:] if len(value) >= 8 else ("已隐藏" if value else "—")


class ClientAdmin(NoAdminDeleteMixin, admin.ModelAdmin):
    '''顾客信息：身份证号唯一，禁止物理删除（历史订单引用）'''
    list_display = ("顾客id", "姓名", "masked_identity")
    list_filter = ("顾客id", "姓名")
    search_fields = ("姓名",)
    exclude = ("身份证号",)
    list_per_page = 15

    @admin.display(description="身份证号（脱敏）")
    def masked_identity(self, obj):
        value = (obj.身份证号 or "").strip()
        return value[:2] + "*" * max(0, len(value) - 4) + value[-2:] if len(value) >= 6 else ("已隐藏" if value else "—")


class RoomAdmin(NoAdminDeleteMixin, admin.ModelAdmin):
    '''客房信息：房态流转走业务视图（room_status/工作流按钮）'''
    list_display = ("房间号", "客房类型", "价格", "房间状态", "负责人编号")
    list_filter = ("房间状态", "客房类型", "价格", "负责人编号")
    list_per_page = 15


class StaffAdmin(NoAdminDeleteMixin, admin.ModelAdmin):
    '''员工信息：月薪可见性受 view_staff 门控（字段遮罩待 P1-1）'''
    list_display = ("员工id", "姓名", "职务", "salary_hidden", "工龄")
    list_filter = ("员工id", "姓名", "职务")
    exclude = ("月薪",)
    list_per_page = 15

    @admin.display(description="月薪")
    def salary_hidden(self, obj):
        return "受控字段"


class VipAdmin(NoAdminDeleteMixin, admin.ModelAdmin):
    '''会员信息：联系方式唯一，禁止物理删除'''
    list_display = ("会员编号", "姓名", "会员等级", "masked_phone")
    list_filter = ("会员编号", "姓名", "会员等级")
    search_fields = ("姓名", "会员编号")
    exclude = ("联系方式",)
    list_per_page = 15

    @admin.display(description="联系方式（脱敏）")
    def masked_phone(self, obj):
        value = (obj.联系方式 or "").strip()
        return value[:3] + "****" + value[-4:] if len(value) >= 8 else ("已隐藏" if value else "—")


class FinanceRecordAdmin(NoAdminDeleteMixin, admin.ModelAdmin):
    '''财务流水：入账记录不物理删除（纠错走冲销/红字流程）'''
    list_display = ("id", "类型", "金额", "事由", "关联订单", "日期", "经手人")
    list_filter = ("类型", "日期", "经手人")
    list_per_page = 15


class NightAuditAdmin(NoAdminDeleteMixin, admin.ModelAdmin):
    '''夜审日结记录：营业日审计证据，禁止删除/修改'''
    list_display = ("id", "营业日期", "在住单数", "过租总额", "未到处理数", "操作人", "执行时间")
    list_filter = ("营业日期", "操作人")
    list_per_page = 15

    def has_change_permission(self, request, obj=None):
        # 夜审记录是不可变审计证据（含历史重复数据的可追溯性）
        return obj is None


class FolioAdmin(NoAdminDeleteMixin, admin.ModelAdmin):
    '''账单流水：凭证不物理删除（冲账为红字记录）'''
    list_display = ("id", "order", "类型", "金额", "事由", "日期", "经手人")
    list_filter = ("类型", "日期", "经手人")
    list_per_page = 15


# ---------- 注册（餐饮 Food / 餐饮订单 Order / 停车 Park 已随模块隐藏，不注册 Admin） ----------
admin.site.register(Accommodation, AccommodationAdmin)
admin.site.register(Client, ClientAdmin)
admin.site.register(Room, RoomAdmin)
admin.site.register(Vip, VipAdmin)
admin.site.register(Staff, StaffAdmin)
admin.site.register(FinanceRecord, FinanceRecordAdmin)
admin.site.register(NightAudit, NightAuditAdmin)
admin.site.register(Folio, FolioAdmin)
