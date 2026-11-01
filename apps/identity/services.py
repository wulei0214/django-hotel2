# -*- coding: utf-8 -*-
"""统一员工生命周期服务（PMS_后台与前台员工账号权限关联设计方案 Phase 1）。

设计原则（方案 §4.2/§16）：
  * 前台业务页、Django Admin、管理命令与未来 API 的员工写操作，
    一律调用本服务——不允许任何入口直接 model.save() 拼接员工/账号/角色关系
  * StaffProfile = 自然人档案；User = 登录身份；PropertyStaffMembership = DEFAULT 任职；
    StaffRoleAssignment = 岗位授权记录；User.groups = 运行时权限镜像（与授权记录同步）
  * 所有写操作幂等可重复执行；关键动作写 hotel.audit 审计（不含敏感值）
  * 授权历史不物理删除：调岗/撤销用 ends_at 关闭
"""
import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.utils import timezone

from apps.core.models import Property
from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment

audit = logging.getLogger("hotel.audit")


def _default_property():
    return Property.objects.filter(
        code="DEFAULT", status=Property.Status.ACTIVE).order_by("id").first()


def _sync_group_mirror(user):
    """把 User.groups 镜像同步为该用户当前有效 RoleAssignment 的组集合。

    这是"授权记录 ↔ 运行时权限"的唯一同步点：
      * 授权生效（含临时授权未到期）→ 组在镜像中
      * 授权到期/结束 → 组从镜像移除（下一请求立即失权，AUTH-05/E-37）
    镜像只增删与身份链对应的业务组，不触碰其他组。
    """
    now = timezone.now()
    # 注意：多值反向关系的条件必须放在同一个 filter(Q(...)) 内——
    # 链式 .filter() 会产生独立 JOIN，导致其他用户的授权令组被错误保留
    from django.db.models import Q
    active_groups = Group.objects.filter(
        Q(pms_role_assignments__membership__staff_profile__user=user,
          pms_role_assignments__starts_at__lte=now,
          pms_role_assignments__ends_at__isnull=True)
        | Q(pms_role_assignments__membership__staff_profile__user=user,
            pms_role_assignments__starts_at__lte=now,
            pms_role_assignments__ends_at__gt=now)
    ).distinct()
    user.groups.set(active_groups)


class EmployeeLifecycleError(Exception):
    """员工生命周期操作的业务校验失败（消息面向操作者）。"""


class EmployeeLifecycleService:
    """员工/账号/任职/角色授权的唯一写入入口（方案 §4.2 实现原则）。"""

    # ---------- 查询 ----------
    @staticmethod
    def get_membership(profile):
        return PropertyStaffMembership.objects.filter(
            staff_profile=profile).order_by("id").first()

    @staticmethod
    def active_assignments(membership):
        now = timezone.now()
        qs = StaffRoleAssignment.objects.filter(
            membership=membership, starts_at__lte=now)
        return [a for a in qs
                if a.ends_at is None or a.ends_at > now]

    # ---------- 新增员工（STAFF-01：允许只建档不开账号） ----------
    @classmethod
    @transaction.atomic
    def create_employee(cls, *, full_name, employee_no, position_code="UNASSIGNED",
                        phone="", started_at=None, legacy_staff=None,
                        account_username="", account_password="",
                        role_groups=None, granted_by=None):
        """创建员工档案 + DEFAULT 任职；可选开通账号并分配初始角色。

        查重（STAFF-06）：同店工号唯一、同手机号唯一、同账号名唯一。
        返回 (profile, membership, user_or_None)。
        """
        User = get_user_model()
        property_obj = _default_property()
        if property_obj is None:
            raise EmployeeLifecycleError("未初始化 DEFAULT 物业，无法建档")
        full_name = (full_name or "").strip()
        employee_no = (employee_no or "").strip()
        if not full_name:
            raise EmployeeLifecycleError("员工姓名不能为空")
        if not employee_no:
            raise EmployeeLifecycleError("店内工号不能为空")
        if PropertyStaffMembership.objects.filter(
                property=property_obj, employee_no=employee_no).exists():
            raise EmployeeLifecycleError(f"工号 {employee_no} 已存在，不能重复建档")
        if phone and StaffProfile.objects.filter(phone=phone).exclude(
                full_name=full_name).exists():
            raise EmployeeLifecycleError(f"手机号 {phone} 已被其他员工使用")

        user = None
        account_username = (account_username or "").strip()
        if account_username:
            if User.objects.filter(username=account_username).exists():
                raise EmployeeLifecycleError(f"登录账号 {account_username} 已被占用")
            if not account_password or len(account_password) < 6:
                raise EmployeeLifecycleError("初始密码至少 6 位")
            user = User.objects.create_user(
                username=account_username, password=account_password,
                first_name=full_name)
            # D-04：is_staff 仅用于 Django Admin；PMS 员工不再自动获得后台入口

        profile = StaffProfile.objects.create(
            user=user, full_name=full_name, phone=phone or "",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
            legacy_staff=legacy_staff)
        membership = PropertyStaffMembership.objects.create(
            property=property_obj, staff_profile=profile,
            employee_no=employee_no, position_code=position_code or "UNASSIGNED",
            status=PropertyStaffMembership.Status.ACTIVE,
            started_at=started_at or timezone.localdate())

        for group in role_groups or []:
            cls.assign_role(membership=membership, group=group,
                            granted_by=granted_by, reason="入职初始授权")
        if user:
            _sync_group_mirror(user)

        audit.info("员工创建 user=%s 员工=%s(profile %s) 工号=%s 账号=%s",
                   getattr(granted_by, "username", "system"),
                   full_name, profile.pk, employee_no,
                   account_username or "未开通")
        return profile, membership, user

    # ---------- 开通/关联账号（STAFF-02） ----------
    @classmethod
    @transaction.atomic
    def provision_account(cls, profile, *, username, password, granted_by=None):
        """为已有档案开通登录账号（或修复缺失关联）；幂等：已绑定同账号直接返回。"""
        User = get_user_model()
        if profile.user_id:
            if profile.user.username == username:
                return profile.user
            raise EmployeeLifecycleError("该员工已绑定其他登录账号")
        if User.objects.filter(username=username).exists():
            raise EmployeeLifecycleError(f"登录账号 {username} 已被占用")
        if not password or len(password) < 6:
            raise EmployeeLifecycleError("初始密码至少 6 位")
        user = User.objects.create_user(username=username, password=password,
                                        first_name=profile.full_name)
        profile.user = user
        profile.save(update_fields=("user", "updated_at"))
        _sync_group_mirror(user)
        audit.info("账号开通 user=%s 员工=%s(profile %s) 账号=%s",
                   getattr(granted_by, "username", "system"),
                   profile.full_name, profile.pk, username)
        return user

    # ---------- 角色分配 / 结束（STAFF-07/08：兼岗并集、调岗留历史） ----------
    @classmethod
    @transaction.atomic
    def assign_role(cls, *, membership, group, starts_at=None, ends_at=None,
                    granted_by=None, reason=""):
        """在任职下新增一条角色授权并同步组镜像；重复的同组有效授权幂等返回。

        职责分离（方案 §6.2/§10.3）：操作者不能给自己分配任何角色（禁止自我提权）。
        """
        target_user = membership.staff_profile.user
        if (granted_by is not None and target_user is not None
                and granted_by.pk == target_user.pk):
            raise EmployeeLifecycleError(
                "不能为自己分配角色（禁止自我提权）；如需调整请联系其他管理员")
        now = timezone.now()
        starts_at = starts_at or now
        active = StaffRoleAssignment.objects.filter(
            membership=membership, group=group,
            starts_at__lte=now).filter(
            Q_ends_null_or_future()).first()
        if active is not None:
            return active
        assignment = StaffRoleAssignment.objects.create(
            membership=membership, group=group, starts_at=starts_at,
            ends_at=ends_at, granted_by=granted_by)
        if membership.staff_profile.user_id:
            _sync_group_mirror(membership.staff_profile.user)
        audit.info("角色授权 user=%s 员工=%s 角色=%s 起=%s 止=%s 原因=%s",
                   getattr(granted_by, "username", "system"),
                   membership.staff_profile.full_name, group.name,
                   starts_at.strftime("%Y-%m-%d %H:%M"),
                   ends_at.strftime("%Y-%m-%d %H:%M") if ends_at else "长期",
                   reason or "-")
        return assignment

    @classmethod
    @transaction.atomic
    def end_assignment(cls, assignment, *, ended_by=None, reason=""):
        """结束一条角色授权（ends_at=now，不删除历史）并同步组镜像。"""
        now = timezone.now()
        if assignment.ends_at and assignment.ends_at <= now:
            return assignment  # 已结束，幂等
        assignment.ends_at = now
        assignment.save(update_fields=("ends_at",))
        if assignment.membership.staff_profile.user_id:
            _sync_group_mirror(assignment.membership.staff_profile.user)
        audit.info("角色结束 user=%s 员工=%s 角色=%s 原因=%s",
                   getattr(ended_by, "username", "system"),
                   assignment.membership.staff_profile.full_name,
                   assignment.group.name, reason or "-")
        return assignment

    # ---------- 停用 / 离职（STAFF-09） ----------
    @classmethod
    @transaction.atomic
    def set_employment_status(cls, profile, *, status, ended_by=None, reason="",
                              deactivate_user=None):
        """暂停（SUSPENDED）/ 离职（TERMINATED）/ 恢复（ACTIVE）。

        离职：终止全部当前授权、Membership 置 TERMINATED 并填 ended_at；
        deactivate_user=None 表示按策略自动处理（离职→停用账号，暂停→保留）。
        """
        # E-35：不能对自己执行暂停/离职（避免最后一个管理者把自己锁在门外）
        target_user = profile.user
        if (ended_by is not None and target_user is not None
                and ended_by.pk == target_user.pk
                and status != StaffProfile.EmploymentStatus.ACTIVE):
            raise EmployeeLifecycleError(
                "不能对自己执行停职/离职；如需移交请联系其他管理员或平台管理员")
        now = timezone.now()
        profile.employment_status = status
        profile.save(update_fields=("employment_status", "updated_at"))
        membership = cls.get_membership(profile)
        ended_count = 0
        if membership is not None:
            if status == StaffProfile.EmploymentStatus.ACTIVE:
                membership.status = PropertyStaffMembership.Status.ACTIVE
                membership.ended_at = None
            else:
                if status == StaffProfile.EmploymentStatus.TERMINATED:
                    membership.status = PropertyStaffMembership.Status.TERMINATED
                    membership.ended_at = timezone.localdate()
                    for a in cls.active_assignments(membership):
                        cls.end_assignment(a, ended_by=ended_by,
                                           reason=reason or "员工离职")
                        ended_count += 1
                else:
                    membership.status = PropertyStaffMembership.Status.SUSPENDED
            membership.save()
        user = profile.user
        if user is not None:
            if deactivate_user is True or (
                    deactivate_user is None
                    and status == StaffProfile.EmploymentStatus.TERMINATED):
                user.is_active = False
                user.save(update_fields=("is_active",))
            elif status == StaffProfile.EmploymentStatus.ACTIVE:
                user.is_active = True
                user.save(update_fields=("is_active",))
            if user.pk:
                _sync_group_mirror(user)
        audit.info("员工状态变更 user=%s 员工=%s 状态=%s 终止授权=%d 原因=%s",
                   getattr(ended_by, "username", "system"),
                   profile.full_name, status, ended_count, reason or "-")
        return membership

    # ---------- 密码重置（审计只记事件，不记密码值） ----------
    @classmethod
    @transaction.atomic
    def reset_password(cls, profile, *, new_password, reset_by=None):
        User = get_user_model()
        if not profile.user_id:
            raise EmployeeLifecycleError("该员工尚未开通登录账号")
        if not new_password or len(new_password) < 6:
            raise EmployeeLifecycleError("密码至少 6 位")
        user = User.objects.get(pk=profile.user_id)
        user.set_password(new_password)
        user.save(update_fields=("password",))
        audit.info("密码重置 user=%s 目标账号=%s",
                   getattr(reset_by, "username", "system"), user.username)


def Q_ends_null_or_future():
    """ends_at 为空或晚于当前的 Q 条件（避免循环 import 的小工具）。"""
    from django.db.models import Q
    now = timezone.now()
    return Q(ends_at__isnull=True) | Q(ends_at__gt=now)
