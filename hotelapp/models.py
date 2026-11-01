"""
hotelapp 数据模型：8 张业务表 + 权限约束说明。

约定：
  * 表名与字段名沿用酒店业务中文命名（db_table 映射到 MySQL 实际表）
  * 唯一性约束（业务唯一，见 0004 迁移）：
      Client.身份证号 / Vip.联系方式 / Food.菜名 / Park.车辆牌号
  * 各角色（前台/值班经理/财务/店长）对模型的增删改查权限见 0005 迁移与 deploy_notes.md
  * 字段级校验规则集中在各模型 clean()：Django admin 保存时自动执行 full_clean，
    与前台视图的白名单校验（views._first_error）、前端正则（static/js/app.js 的 VPAT）
    保持同一口径——三端一致，后台手工改数不会写脏数据（后台↔前台数据一致性）。
"""

import re

from django.core.exceptions import ValidationError
from django.db import models

# ---------- 前后端共用字段校验正则（与 views 白名单 / app.js VPAT 同口径） ----------
RE_ID10 = re.compile(r"^\d{1,10}$")                            # 业务编号：1-10 位数字
RE_DIGITS4 = re.compile(r"^\d{1,4}$")                          # 工龄：1-4 位数字
RE_NAME = re.compile(r"^[\u4e00-\u9fa5A-Za-z·]{2,10}$")        # 姓名：2-10 位中英文（可含·）
RE_NAME20 = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9·]{2,20}$")   # 菜名等：2-20 位中英文数字
RE_TYPE10 = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9]{2,10}$")    # 客房类型：2-10 位中英文数字
RE_MONEY = re.compile(r"^(0|[1-9]\d{0,5})(\.\d{1,2})?$")       # 金额：非负，最多两位小数（与前台 VPAT 同口径）
RE_IDCARD = re.compile(r"^\d{17}[\dXx]$")                      # 身份证：18 位（末位可 X）
RE_TEL = re.compile(r"^1\d{10}$")                              # 手机号：1 开头 11 位
RE_PLATE = re.compile(r"^[\u4e00-\u9fa5][A-Z][A-Z0-9]{4,6}$")  # 车牌：如 黑A12345

# 枚举值（与前台交互口径一致，修改需三端同步）
JOB_CHOICES = ("总经理", "大堂经理", "厨师", "服务员", "保安")          # 职务
VIP_RANK_CHOICES = ("普通会员", "高级会员")                            # 会员等级
PARK_STATUS_CHOICES = ("空闲", "有车")                                  # 车位当前状态
FINANCE_TYPE_CHOICES = ("收入", "支出")                                 # 财务流水类型
FOLIO_TYPE_CHOICES = ("押金", "房费", "消费", "收款", "冲账")           # 账单流水类型


def _validate(value, pattern, label):
    """按正则校验字段值；空值由必填校验负责，这里跳过空串。"""
    v = (value or "").strip()
    if v and not pattern.match(v):
        raise ValidationError({label: f"{label}格式不正确：{value}"})
    return v


class Accommodation(models.Model):
    顾客id = models.ForeignKey('Client', models.DO_NOTHING, db_column='顾客id', verbose_name="入住顾客")
    客房编号 = models.ForeignKey('Room', models.DO_NOTHING, db_column='客房编号', verbose_name="客房")
    # 日期统一存 YYYY-MM-DD 字符串（历史表结构如此），视图层负责格式校验
    入住时间 = models.CharField(max_length=45, blank=True, null=True, verbose_name="入住日期")
    id = models.IntegerField(primary_key=True)
    # 订单状态机：预订 → 已确认 → 已入住 → 已退房；旁支：已取消 / 未到 / 已冲账（不物理删除）
    状态 = models.CharField(max_length=10, default='预订', blank=True, null=True, verbose_name="订单状态")
    联系电话 = models.CharField(max_length=45, blank=True, null=True, verbose_name="联系电话")
    收款金额 = models.CharField(max_length=45, blank=True, null=True, verbose_name="收款金额")
    押金 = models.CharField(max_length=45, blank=True, null=True, verbose_name="押金")
    退房时间 = models.CharField(max_length=45, blank=True, null=True, verbose_name="退房日期")
    冲账人 = models.CharField(max_length=45, blank=True, null=True, verbose_name="冲账操作人")
    冲账时间 = models.CharField(max_length=45, blank=True, null=True, verbose_name="冲账日期")

    def clean(self):
        """后台编辑时与前台同口径校验（前台视图另有白名单预检）。"""
        if self.状态 and self.状态 not in ORDER_STATUS:
            raise ValidationError({"状态": f"订单状态必须是：{'、'.join(ORDER_STATUS)}"})
        if self.联系电话:
            _validate(self.联系电话, RE_TEL, "联系电话")

    def __str__(self):
        return str(self.id)

    class Meta:
        verbose_name = "客房订单管理"
        verbose_name_plural = verbose_name
        db_table = 'accommodation'


class Client(models.Model):
    顾客id = models.AutoField(primary_key=True, verbose_name="顾客编号")
    姓名 = models.CharField(max_length=45, blank=True, null=True, verbose_name="顾客姓名")
    身份证号 = models.CharField(max_length=45, unique=True, blank=True, null=True, verbose_name="身份证号",
                                help_text="18 位，末位可为 X；全局唯一，重复录入被数据库与校验双重拦截")

    def clean(self):
        _validate(self.姓名, RE_NAME, "姓名")
        if self.身份证号:
            _validate(self.身份证号, RE_IDCARD, "身份证号")

    def __str__(self):
        return self.姓名

    class Meta:
        verbose_name = "顾客管理"
        verbose_name_plural = verbose_name
        db_table = 'client'


class Food(models.Model):
    菜品编号 = models.IntegerField(primary_key=True, verbose_name="菜品编号")
    菜名 = models.CharField(max_length=45, unique=True, verbose_name="菜名",
                            help_text="全局唯一，2-20 位中英文或数字")
    价格 = models.CharField(max_length=45, verbose_name="价格（元）",
                            help_text="非负数字，最多两位小数")
    厨师id = models.ForeignKey('Staff', models.DO_NOTHING, db_column='厨师id', verbose_name="负责厨师")

    def clean(self):
        _validate(self.菜名, RE_NAME20, "菜名")
        _validate(self.价格, RE_MONEY, "价格")

    def __str__(self):
        return self.菜名

    class Meta:
        verbose_name = "餐厅管理"
        verbose_name_plural = verbose_name
        db_table = 'food'


class Order(models.Model):
    顾客编号 = models.ForeignKey(Client, models.DO_NOTHING, db_column='顾客编号', verbose_name="消费顾客")
    菜品编号 = models.ForeignKey(Food, models.DO_NOTHING, db_column='菜品编号', verbose_name="所点菜品")
    服务员编号 = models.ForeignKey('Staff', models.DO_NOTHING, db_column='服务员编号', verbose_name="服务员工")
    桌号 = models.IntegerField(verbose_name="桌号", help_text="正整数桌位号")
    id = models.IntegerField(primary_key=True)
    # 冲账后标记「已冲账」，不再物理删除；报表排除已冲账订单
    状态 = models.CharField(max_length=10, default='正常', blank=True, null=True, verbose_name="订单状态")
    冲账人 = models.CharField(max_length=45, blank=True, null=True, verbose_name="冲账操作人")
    冲账时间 = models.CharField(max_length=45, blank=True, null=True, verbose_name="冲账日期")

    def clean(self):
        if self.状态 and self.状态 not in ("正常", "已冲账"):
            raise ValidationError({"状态": "订单状态必须是：正常、已冲账"})
        if self.桌号 is not None and (self.桌号 < 1 or self.桌号 > 9999):
            raise ValidationError({"桌号": "桌号必须是 1-9999 的整数"})

    def __str__(self):
        return str(self.id)

    class Meta:
        verbose_name = "餐厅订单管理"
        verbose_name_plural = verbose_name
        db_table = 'order'


class Park(models.Model):
    车位编号 = models.IntegerField(primary_key=True, verbose_name="车位编号")
    当前状态 = models.CharField(max_length=45, verbose_name="车位状态",
                               help_text="空闲 / 有车")
    车辆牌号 = models.CharField(max_length=45, unique=True, blank=True, null=True, verbose_name="车辆牌号",
                                help_text="如 黑A12345；同车牌全局唯一（一辆车只占一个车位）")

    def clean(self):
        if self.当前状态 and self.当前状态 not in PARK_STATUS_CHOICES:
            raise ValidationError({"当前状态": "车位状态必须是：空闲、有车"})
        if self.车辆牌号:
            _validate(self.车辆牌号, RE_PLATE, "车辆牌号")

    def __str__(self):
        return str(self.车位编号)

    class Meta:
        verbose_name = "停车场管理"
        verbose_name_plural = verbose_name
        db_table = 'park'


# 房间/订单状态机常量（views 与模板共用语义）
# 订单状态机：待确认 → 已确认 → 已入住 → 已退房；旁支：已取消 / 未到 / 已冲账
ORDER_STATUS = ("待确认", "已确认", "已入住", "已退房", "已取消", "未到", "已冲账")
# 房态清洁链：退房 → 待清洁 → 清洁中 → 待检查 → 空闲（只有检查通过才重回可售）
ROOM_STATUS = ("空闲", "已预订", "已入住", "待清洁", "清洁中", "待检查", "维修中")


class FinanceRecord(models.Model):
    """财务流水：收入（订单收款）/支出（维修、采购等），报表分析的依据"""
    id = models.AutoField(primary_key=True)
    类型 = models.CharField(max_length=10, verbose_name="流水类型", help_text="收入 / 支出")  # 收入 / 支出
    金额 = models.CharField(max_length=45, verbose_name="金额（元）", help_text="非负数字，最多两位小数")
    事由 = models.CharField(max_length=100, verbose_name="事由")
    关联订单 = models.CharField(max_length=45, blank=True, null=True, verbose_name="关联订单",
                               help_text="收入来源的订单编号；支出可空")
    日期 = models.CharField(max_length=45, verbose_name="记账日期", help_text="YYYY-MM-DD")
    经手人 = models.CharField(max_length=45, blank=True, null=True, verbose_name="经手人")

    def clean(self):
        if self.类型 and self.类型 not in FINANCE_TYPE_CHOICES:
            raise ValidationError({"类型": "流水类型必须是：收入、支出"})
        _validate(self.金额, RE_MONEY, "金额")

    def __str__(self):
        return f"{self.日期} {self.类型} {self.金额}"

    class Meta:
        verbose_name = "财务流水"
        verbose_name_plural = verbose_name
        db_table = 'finance_record'


class NightAudit(models.Model):
    """夜审日结记录：营业日切换、过租汇总、No-Show 处理结果"""
    id = models.AutoField(primary_key=True)
    营业日期 = models.CharField(max_length=45, verbose_name="营业日期", help_text="YYYY-MM-DD，夜审切换到的营业日")
    在住单数 = models.IntegerField(default=0, verbose_name="在住单数")
    过租总额 = models.CharField(max_length=45, blank=True, null=True, verbose_name="过租总额（元）",
                               help_text="为每间在住房过租一晚的房费合计")
    未到处理数 = models.IntegerField(default=0, verbose_name="未到处理数",
                                    help_text="本次夜审标记「未到」并释放房间的订单数")
    操作人 = models.CharField(max_length=45, verbose_name="操作人")
    执行时间 = models.CharField(max_length=45, verbose_name="执行时间")

    def clean(self):
        if self.过租总额:
            _validate(self.过租总额, RE_MONEY, "过租总额")

    def __str__(self):
        return f"夜审 {self.营业日期}"

    class Meta:
        verbose_name = "夜审记录"
        verbose_name_plural = verbose_name
        db_table = 'night_audit'


class Folio(models.Model):
    """账单流水（Folio/Ledger）：订单维度账务中心。

    每一笔押金、房费、消费、收款、冲账都是一条独立凭证（金额带符号，冲账为红字负数）；
    前台看到的"应收/余额"一律由流水聚合而来，禁止直接改单笔金额。
    """
    id = models.AutoField(primary_key=True)
    order = models.ForeignKey('Accommodation', models.DO_NOTHING, db_column='order_id', blank=True, null=True,
                              verbose_name="关联客房订单")
    类型 = models.CharField(max_length=10, verbose_name="凭证类型",
                           help_text="押金 / 房费 / 消费 / 收款 / 冲账")  # 押金 / 房费 / 消费 / 收款 / 冲账
    金额 = models.CharField(max_length=45, verbose_name="金额（带符号）",
                            help_text="冲账红字为负数；余额由流水聚合计算")  # 带符号：冲账红字为负数
    事由 = models.CharField(max_length=100, verbose_name="事由")
    日期 = models.CharField(max_length=45, verbose_name="记账日期")
    经手人 = models.CharField(max_length=45, blank=True, null=True, verbose_name="经手人")

    def clean(self):
        if self.类型 and self.类型 not in FOLIO_TYPE_CHOICES:
            raise ValidationError({"类型": f"凭证类型必须是：{'、'.join(FOLIO_TYPE_CHOICES)}"})

    def __str__(self):
        return f"[{self.类型}] {self.金额} {self.事由}"

    class Meta:
        verbose_name = "账单流水"
        verbose_name_plural = verbose_name
        db_table = 'folio'


class Room(models.Model):
    房间号 = models.IntegerField(primary_key=True, verbose_name="房间号")
    客房类型 = models.CharField(max_length=45, verbose_name="客房类型",
                               help_text="如 商务大床房；2-10 位中英文或数字")
    价格 = models.IntegerField(verbose_name="门市价（元）", help_text="1-5 位整数")
    负责人编号 = models.ForeignKey('Staff', models.DO_NOTHING, db_column='负责人编号', verbose_name="保洁负责人")
    # 房间状态：空闲 / 已预订 / 已入住 / 待清洁 / 清洁中 / 待检查 / 维修中
    # 清洁链：退房 → 待清洁 → 清洁中 → 待检查 → 空闲（检查通过才重回可售）
    房间状态 = models.CharField(max_length=10, default='空闲', blank=True, null=True, verbose_name="房间状态")

    def clean(self):
        if self.房间状态 and self.房间状态 not in ROOM_STATUS:
            raise ValidationError({"房间状态": f"房间状态必须是：{'、'.join(ROOM_STATUS)}"})
        if self.价格 is not None and (self.价格 < 0 or self.价格 > 99999):
            raise ValidationError({"价格": "价格必须是 0-99999 的整数"})
        if self.客房类型:
            _validate(self.客房类型, RE_TYPE10, "客房类型")

    def __str__(self):
        return str(self.房间号)

    class Meta:
        verbose_name = "客房信息管理"
        verbose_name_plural = verbose_name
        db_table = 'room'


class Staff(models.Model):
    员工id = models.AutoField(primary_key=True, verbose_name="工号")
    姓名 = models.CharField(max_length=45, verbose_name="员工姓名", help_text="2-10 位中英文")
    启用 = models.BooleanField(default=True, verbose_name="是否启用")
    职务 = models.CharField(max_length=45, verbose_name="职务",
                            help_text="总经理 / 大堂经理 / 厨师 / 服务员 / 保安")
    月薪 = models.CharField(max_length=45, verbose_name="月薪（元）",
                            help_text="非负数字，最多两位小数")
    工龄 = models.CharField(max_length=45, verbose_name="工龄（年）",
                            help_text="1-4 位数字")

    def clean(self):
        """后台编辑与前台录入同一套规则（职务/金额/工龄白名单）。"""
        _validate(self.姓名, RE_NAME, "姓名")
        if self.职务 and self.职务 not in JOB_CHOICES:
            raise ValidationError({"职务": f"职务必须是：{'、'.join(JOB_CHOICES)}"})
        _validate(self.月薪, RE_MONEY, "月薪")
        if self.工龄:
            v = (self.工龄 or "").strip()
            if v and not RE_DIGITS4.match(v):
                raise ValidationError({"工龄": "工龄必须是 1-4 位数字"})

    def __str__(self):
        return self.姓名

    class Meta:
        verbose_name = "员工信息管理"
        verbose_name_plural = verbose_name
        db_table = 'staff'


class Vip(models.Model):
    会员编号 = models.AutoField(primary_key=True, verbose_name="会员编号")
    姓名 = models.CharField(max_length=45, verbose_name="会员姓名", help_text="2-10 位中英文")
    会员等级 = models.CharField(max_length=45, verbose_name="会员等级",
                               help_text="普通会员 / 高级会员")
    联系方式 = models.CharField(max_length=45, unique=True, verbose_name="联系方式",
                               help_text="1 开头的 11 位手机号；全局唯一")

    def clean(self):
        _validate(self.姓名, RE_NAME, "姓名")
        if self.会员等级 and self.会员等级 not in VIP_RANK_CHOICES:
            raise ValidationError({"会员等级": "会员等级必须是：普通会员、高级会员"})
        if self.联系方式:
            _validate(self.联系方式, RE_TEL, "联系方式")

    def __str__(self):
        return self.姓名

    class Meta:
        verbose_name = "会员信息管理"
        verbose_name_plural = verbose_name
        db_table = 'vip'
