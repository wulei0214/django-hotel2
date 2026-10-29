"""
hotelapp 数据模型：8 张业务表 + 权限约束说明。

约定：
  * 表名与字段名沿用酒店业务中文命名（db_table 映射到 MySQL 实际表）
  * 唯一性约束（业务唯一，见 0004 迁移）：
      Client.身份证号 / Vip.联系方式 / Food.菜名 / Park.车辆牌号
  * 各角色（前台/值班经理/财务/店长）对模型的增删改查权限见 0005 迁移与 deploy_notes.md
"""

from django.db import models


class Accommodation(models.Model):
    顾客id = models.ForeignKey('Client', models.DO_NOTHING, db_column='顾客id')
    客房编号 = models.ForeignKey('Room', models.DO_NOTHING, db_column='客房编号')
    入住时间 = models.CharField(max_length=45, blank=True, null=True)
    id = models.IntegerField(primary_key=True)
    # 订单状态流：预订 → 已入住 → 已退房；冲账后标记「已冲账」（不物理删除）
    状态 = models.CharField(max_length=10, default='预订', blank=True, null=True)
    联系电话 = models.CharField(max_length=45, blank=True, null=True)
    收款金额 = models.CharField(max_length=45, blank=True, null=True)
    押金 = models.CharField(max_length=45, blank=True, null=True)
    退房时间 = models.CharField(max_length=45, blank=True, null=True)
    冲账人 = models.CharField(max_length=45, blank=True, null=True)
    冲账时间 = models.CharField(max_length=45, blank=True, null=True)
    def __str__(self):
        return str(self.id)
    class Meta:
        verbose_name = "客房订单管理"
        verbose_name_plural = verbose_name
        db_table = 'accommodation'


class Client(models.Model):
    顾客id = models.AutoField(primary_key=True)
    姓名 = models.CharField(max_length=45, blank=True, null=True)
    身份证号 = models.CharField(max_length=45, unique=True, blank=True, null=True)
    def __str__(self):
        return self.姓名
    class Meta:
        verbose_name = "顾客管理"
        verbose_name_plural = verbose_name
        db_table = 'client'


class Food(models.Model):
    菜品编号 = models.IntegerField(primary_key=True)
    菜名 = models.CharField(max_length=45, unique=True)
    价格 = models.CharField(max_length=45)
    厨师id = models.ForeignKey('Staff', models.DO_NOTHING, db_column='厨师id')
    def __str__(self):
        return self.菜名
    class Meta:
        verbose_name = "餐厅管理"
        verbose_name_plural = verbose_name
        db_table = 'food'


class Order(models.Model):
    顾客编号 = models.ForeignKey(Client, models.DO_NOTHING, db_column='顾客编号')
    菜品编号 = models.ForeignKey(Food, models.DO_NOTHING, db_column='菜品编号')
    服务员编号 = models.ForeignKey('Staff', models.DO_NOTHING, db_column='服务员编号')
    桌号 = models.IntegerField()
    id = models.IntegerField(primary_key=True)
    # 冲账后标记「已冲账」，不再物理删除；报表排除已冲账订单
    状态 = models.CharField(max_length=10, default='正常', blank=True, null=True)
    冲账人 = models.CharField(max_length=45, blank=True, null=True)
    冲账时间 = models.CharField(max_length=45, blank=True, null=True)
    def __str__(self):
        return str(self.id)

    class Meta:
        verbose_name = "餐厅订单管理"
        verbose_name_plural = verbose_name
        db_table = 'order'


class Park(models.Model):
    车位编号 = models.IntegerField(primary_key=True)
    当前状态 = models.CharField(max_length=45)
    车辆牌号 = models.CharField(max_length=45, unique=True, blank=True, null=True)
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
    类型 = models.CharField(max_length=10)  # 收入 / 支出
    金额 = models.CharField(max_length=45)
    事由 = models.CharField(max_length=100)
    关联订单 = models.CharField(max_length=45, blank=True, null=True)
    日期 = models.CharField(max_length=45)
    经手人 = models.CharField(max_length=45, blank=True, null=True)

    def __str__(self):
        return f"{self.日期} {self.类型} {self.金额}"

    class Meta:
        verbose_name = "财务流水"
        verbose_name_plural = verbose_name
        db_table = 'finance_record'


class NightAudit(models.Model):
    """夜审日结记录：营业日切换、过租汇总、No-Show 处理结果"""
    id = models.AutoField(primary_key=True)
    营业日期 = models.CharField(max_length=45)
    在住单数 = models.IntegerField(default=0)
    过租总额 = models.CharField(max_length=45, blank=True, null=True)
    未到处理数 = models.IntegerField(default=0)
    操作人 = models.CharField(max_length=45)
    执行时间 = models.CharField(max_length=45)

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
    order = models.ForeignKey('Accommodation', models.DO_NOTHING, db_column='order_id', blank=True, null=True)
    类型 = models.CharField(max_length=10)  # 押金 / 房费 / 消费 / 收款 / 冲账
    金额 = models.CharField(max_length=45)  # 带符号：冲账红字为负数
    事由 = models.CharField(max_length=100)
    日期 = models.CharField(max_length=45)
    经手人 = models.CharField(max_length=45, blank=True, null=True)

    def __str__(self):
        return f"[{self.类型}] {self.金额} {self.事由}"

    class Meta:
        verbose_name = "账单流水"
        verbose_name_plural = verbose_name
        db_table = 'folio'


class Room(models.Model):
    房间号 = models.IntegerField(primary_key=True)
    客房类型 = models.CharField(max_length=45)
    价格 = models.IntegerField()
    负责人编号 = models.ForeignKey('Staff', models.DO_NOTHING, db_column='负责人编号')
    # 房间状态：空闲 / 已预订 / 已入住 / 清洁中 / 维修中
    房间状态 = models.CharField(max_length=10, default='空闲', blank=True, null=True)
    def __str__(self):
        return str(self.房间号)
    class Meta:
        verbose_name = "客房信息管理"
        verbose_name_plural = verbose_name
        db_table = 'room'


class Staff(models.Model):
    员工id = models.AutoField(primary_key=True)
    姓名 = models.CharField(max_length=45)
    职务 = models.CharField(max_length=45)
    月薪 = models.CharField(max_length=45)
    工龄 = models.CharField(max_length=45)

    def __str__(self):
        return self.姓名

    class Meta:
        verbose_name = "员工信息管理"
        verbose_name_plural = verbose_name
        db_table = 'staff'


class Vip(models.Model):
    会员编号 = models.AutoField(primary_key=True)
    姓名 = models.CharField(max_length=45)
    会员等级 = models.CharField(max_length=45)
    联系方式 = models.CharField(max_length=45, unique=True)
    def __str__(self):
        return self.姓名

    class Meta:
        verbose_name = "会员信息管理"
        verbose_name_plural = verbose_name
        db_table = 'vip'
