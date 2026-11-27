# 业务阅读提示：员工身份、账号、任职和岗位变更必须收口到本服务；View/API 负责权限、参数和响应，不应直接写 User/Profile/Membership/Assignment。邀请激活应扩展本服务的事务方法，不能在 View 或 Invitation Service 直接创建 User。
"""统一员工生命周期服务（PMS_后台与前台员工账号权限关联设计方案 Phase 1）。

设计原则（方案 §4.2/§16）：
  * 前台业务页、Django Admin、管理命令与未来 API 的员工写操作，
    一律调用本服务——不允许任何入口直接 model.save() 拼接员工/账号/角色关系
  * StaffProfile = 自然人档案；User = 登录身份；PropertyStaffMembership = DEFAULT 任职；
    StaffRoleAssignment = 岗位授权记录；User.groups 不参与 PMS 业务授权
  * 所有写操作幂等可重复执行；关键动作写 hotel.audit 审计（不含敏感值）
  * 授权历史不物理删除：调岗/撤销用 ends_at 关闭
"""

import logging
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import Property

from .models import PropertyStaffMembership, StaffProfile, StaffRoleAssignment
from .roles import BUSINESS_ROLE_NAMES

audit = logging.getLogger("hotel.audit")


class EmployeeLifecycleError(Exception):
    """员工生命周期操作的业务校验失败（消息面向操作者）。"""


def _enforce_password_policy(password, user=None):
    """AUTH-05：开通/激活/重置/自助改密统一走 Django validate_password。"""
    try:
        validate_password(password, user=user)
    except DjangoValidationError as exc:
        raise EmployeeLifecycleError("；".join(exc.messages)) from exc


def _active_role_names(membership):
    """该任职当前**生效中**的岗位名称集合（ARCH-04 H1 审计前后值快照用）。

    生效口径 = starts_at <= now 且（ends_at 为空或 ends_at > now）。
    与 `StaffRoleAssignment.Meta.ordering` 无关，输出已排序（调用方不再排序）。
    """
    now = timezone.now()
    qs = StaffRoleAssignment.objects.filter(membership=membership, starts_at__lte=now).filter(
        Q_ends_null_or_future()
    )
    return sorted({name for name in qs.values_list("group__name", flat=True) if name})


class EmployeeLifecycleService:
    """员工/账号/任职/角色授权的唯一写入入口（方案 §4.2 实现原则）。"""

    # ---------- 查询 ----------
    @staticmethod
    def get_membership(profile, property_obj):
        if property_obj is None or property_obj.status != Property.Status.ACTIVE:
            raise EmployeeLifecycleError("必须指定有效物业，不能回退到默认物业。")
        # WB-041 收尾：重新入职会保留旧 TERMINATED 任职并新建 ACTIVE 任职。
        # 旧实现取 id 最小（最旧）任职，会让已重新入职员工的「恢复在职」
        # 命中旧 TERMINATED 行而被误阻断。优先取最新 ACTIVE 任职；无 ACTIVE
        # 时取最新一条（仍保证离职后直接恢复旧任职被守卫拒绝）。
        qs = PropertyStaffMembership.objects.filter(staff_profile=profile, property=property_obj)
        active = qs.filter(status=PropertyStaffMembership.Status.ACTIVE).order_by("-id").first()
        return active or qs.order_by("-id").first()

    @staticmethod
    def active_assignments(membership):
        now = timezone.now()
        qs = StaffRoleAssignment.objects.filter(membership=membership, starts_at__lte=now)
        return [a for a in qs if a.ends_at is None or a.ends_at > now]

    # ---------- 新增员工（STAFF-01：允许只建档不开账号） ----------
    @classmethod
    @transaction.atomic
    def create_employee(
        cls,
        *,
        property_obj,
        full_name,
        employee_no="",
        position_code="UNASSIGNED",
        phone="",
        started_at=None,
        legacy_staff=None,
        account_username="",
        account_password="",
        role_groups=None,
        granted_by=None,
        department_code="",
    ):
        """在服务端明确指定的物业内建档；可选开通账号并分配初始角色。

        查重（STAFF-06）：同店工号唯一、同手机号唯一、同账号名唯一。
        WB-100：工号一律由服务端 generate_employee_no() 按部门代码自动
        编号（DEC-EMP-02，前端不得决定流水号）——employee_no 参数仅保留
        只读兼容，传入非空时忽略并记 WARNING；department_code 必填
        （岗位代码恰为受控部门代码 FO/HK/FIN/MG/ENG 时可自动映射）。
        返回 (profile, membership, user_or_None)。
        """
        User = get_user_model()
        if property_obj is None or property_obj.status != Property.Status.ACTIVE:
            raise EmployeeLifecycleError("必须指定有效的当前物业，不能回退到 DEFAULT 物业。")
        full_name = (full_name or "").strip()
        employee_no = (employee_no or "").strip()
        phone = (phone or "").strip()
        if not full_name:
            raise EmployeeLifecycleError("员工姓名不能为空")
        if employee_no:
            # WB-100 服务端统一编号：调用方传入的工号不作为分配依据（旧签名只读兼容）
            audit.warning(
                "WB-100 服务端统一编号：忽略调用方传入工号 %s（员工 %s），"
                "一律改由服务端按部门代码自动编号",
                employee_no,
                full_name,
            )
        # WB-037/WB-100：主创建路径一律走工号自动编号（部门代码 + 四位流水号）
        from apps.identity.employee_number import DEPARTMENT_CODES, generate_employee_no

        dept = (department_code or "").strip().upper()
        if not dept:
            candidate = (position_code or "").strip().upper()
            if candidate in DEPARTMENT_CODES:
                dept = candidate
        if not dept:
            raise EmployeeLifecycleError(
                "必须指定部门代码（FO/HK/FIN/MG/ENG），"
                "或岗位代码恰为受控部门代码以便服务端自动编号"
            )
        employee_no = generate_employee_no(property_obj=property_obj, department_code=dept)
        if PropertyStaffMembership.objects.filter(
            property=property_obj, employee_no=employee_no
        ).exists():
            raise EmployeeLifecycleError(f"工号 {employee_no} 已存在，不能重复建档")
        # C-03：员工手机号可能是家庭/紧急联系人，可共享；不得作为员工自然人唯一键。
        # 唯一身份仍由员工档案、物业任职和物业内工号建立。

        user = None
        account_username = (account_username or "").strip()
        if account_username:
            if User.objects.filter(username=account_username).exists():
                raise EmployeeLifecycleError(f"登录账号 {account_username} 已被占用")
            if not account_password or len(account_password) < 6:
                raise EmployeeLifecycleError("初始密码至少 6 位")
            _enforce_password_policy(account_password)  # P1-03：创建主路径同样走策略
            user = User.objects.create_user(
                username=account_username, password=account_password, first_name=full_name
            )
            # D-04：is_staff 仅用于 Django Admin；PMS 员工不再自动获得后台入口
            # D-07：初始密码一次性 → 首登强制改密（见下方 StaffProfile 创建参数）

        must_change = user is not None  # 开了账号就需要首登改密
        profile = StaffProfile.objects.create(
            user=user,
            full_name=full_name,
            phone=phone or "",
            employment_status=StaffProfile.EmploymentStatus.ACTIVE,
            must_change_password=must_change,
            last_password_change=timezone.now() if must_change else None,
            legacy_staff=legacy_staff,
        )
        membership = PropertyStaffMembership.objects.create(
            property=property_obj,
            staff_profile=profile,
            employee_no=employee_no,
            position_code=position_code or "UNASSIGNED",
            status=PropertyStaffMembership.Status.ACTIVE,
            started_at=started_at or timezone.localdate(),
        )

        for group in role_groups or []:
            cls.assign_role(
                membership=membership, group=group, granted_by=granted_by, reason="入职初始授权"
            )

        from apps.core.models import AuditEvent

        AuditEvent.record(
            action="identity.employee_create",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=granted_by,
            property_obj=property_obj,
            after={
                "employee_no": employee_no,
                "position_code": position_code,
                "account": account_username or None,
                "roles": [g.name for g in (role_groups or [])],
            },
            reason="入职建档",
        )
        audit.info(
            "员工创建 user=%s 员工=%s(profile %s) 工号=%s 账号=%s",
            getattr(granted_by, "username", "system"),
            full_name,
            profile.pk,
            employee_no,
            account_username or "未开通",
        )
        return profile, membership, user

    # ---------- 开通/关联账号（STAFF-02） ----------
    @classmethod
    @transaction.atomic
    def provision_account(cls, profile, *, username, password, granted_by=None, property_obj=None):
        """为已有档案开通登录账号（或修复缺失关联）；幂等：已绑定同账号直接返回。

        property_obj 仅用于审计事件归属（§10.1 迁移后员工中心统一走本方法）。
        """
        User = get_user_model()
        username = (username or "").strip()
        if not username:
            raise EmployeeLifecycleError("登录账号不能为空")
        if profile.user_id:
            if profile.user.username == username:
                return profile.user
            raise EmployeeLifecycleError("该员工已绑定其他登录账号")
        if User.objects.filter(username=username).exists():
            raise EmployeeLifecycleError(f"登录账号 {username} 已被占用")
        if not password or len(password) < 6:
            raise EmployeeLifecycleError("初始密码至少 6 位")
        _enforce_password_policy(password)
        user = User.objects.create_user(
            username=username, password=password, first_name=profile.full_name
        )
        # D-07：一次性初始密码，首次登录强制改密（set_password 换 session_auth_hash
        # 自动使该账号旧会话失效）
        profile.must_change_password = True
        profile.last_password_change = timezone.now()
        profile.user = user
        profile.save(
            update_fields=("user", "must_change_password", "last_password_change", "updated_at")
        )
        from apps.core.models import AuditEvent

        AuditEvent.record(
            action="identity.account_provision",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=granted_by,
            property_obj=property_obj,
            after={"account": username},
            reason="开通登录账号；初始密码不落审计",
        )
        audit.info(
            "账号开通 user=%s 员工=%s(profile %s) 账号=%s",
            getattr(granted_by, "username", "system"),
            profile.full_name,
            profile.pk,
            username,
        )
        return user

    # ---------- 绑定既有账号（迁移/运维入口统一走本服务） ----------
    @classmethod
    @transaction.atomic
    def bind_existing_user(cls, *, membership, user, group, password="", granted_by=None):
        """把既有 Django 用户绑定到员工任职并授予岗位；不写 is_staff / user.groups。"""
        profile = membership.staff_profile
        if not user.is_active:
            raise EmployeeLifecycleError("不能把员工绑定到已停用账号。")
        if profile.user_id and profile.user_id != user.pk:
            raise EmployeeLifecycleError("该员工档案已经绑定其他登录账号。")
        if StaffProfile.objects.filter(user=user).exclude(pk=profile.pk).exists():
            raise EmployeeLifecycleError("该登录账号已经绑定其他员工档案。")
        if password:
            _enforce_password_policy(password, user=user)
            user.set_password(password)
            user.save(update_fields=("password",))
        if profile.user_id != user.pk:
            profile.user = user
            profile.save(update_fields=("user", "updated_at"))
        assignment = cls.assign_role(
            membership=membership, group=group, granted_by=granted_by, reason="管理命令绑定岗位"
        )
        audit.info(
            "既有账号绑定 user=%s 员工=%s(profile %s) 岗位=%s",
            getattr(granted_by, "username", "system"),
            profile.full_name,
            profile.pk,
            group.name,
        )
        return profile, assignment

    # ---------- 角色分配 / 结束（STAFF-07/08：兼岗并集、调岗留历史） ----------
    @classmethod
    @transaction.atomic
    def assign_role(
        cls, *, membership, group, starts_at=None, ends_at=None, granted_by=None, reason=""
    ):
        """在任职下新增一条角色授权；重复的同组有效授权幂等返回。

        职责分离（方案 §6.2/§10.3）：操作者不能给自己分配任何角色（禁止自我提权）。
        """
        if group is None:
            raise EmployeeLifecycleError("请选择有效的角色")
        if group.name not in BUSINESS_ROLE_NAMES:
            raise EmployeeLifecycleError("所选角色不在允许的酒店岗位目录中")
        try:
            membership = (
                PropertyStaffMembership.objects.select_for_update()
                .select_related("staff_profile", "property")
                .get(pk=membership.pk)
            )
        except PropertyStaffMembership.DoesNotExist as exc:
            raise EmployeeLifecycleError("员工任职关系不存在") from exc
        if (
            membership.status != PropertyStaffMembership.Status.ACTIVE
            or membership.staff_profile.employment_status != StaffProfile.EmploymentStatus.ACTIVE
            or membership.property.status != Property.Status.ACTIVE
        ):
            raise EmployeeLifecycleError("当前任职或员工档案未处于有效状态，不能授予岗位")
        target_user = membership.staff_profile.user
        if granted_by is not None and target_user is not None and granted_by.pk == target_user.pk:
            raise EmployeeLifecycleError(
                "不能为自己分配角色（禁止自我提权）；如需调整请联系其他管理员"
            )
        now = timezone.now()
        if starts_at is None:
            starts_at = now
        elif not isinstance(starts_at, datetime):
            raise EmployeeLifecycleError("角色授权开始时间无效")
        elif timezone.is_naive(starts_at):
            starts_at = timezone.make_aware(starts_at, timezone.get_current_timezone())
        if ends_at is not None:
            if not isinstance(ends_at, datetime):
                raise EmployeeLifecycleError("角色授权结束时间无效")
            if timezone.is_naive(ends_at):
                ends_at = timezone.make_aware(ends_at, timezone.get_current_timezone())
            if ends_at <= starts_at:
                raise EmployeeLifecycleError("角色授权结束时间必须晚于开始时间")
        active = (
            StaffRoleAssignment.objects.filter(
                membership=membership, group=group, starts_at__lte=now
            )
            .filter(Q_ends_null_or_future())
            .first()
        )
        if active is not None:
            return active
        assignment = StaffRoleAssignment.objects.create(
            membership=membership,
            group=group,
            starts_at=starts_at,
            ends_at=ends_at,
            granted_by=granted_by,
        )
        from apps.core.models import AuditEvent

        # ARCH-04 H1：授权变更审计必须同时带**前后值**——只有 after 无法回答
        # 「授权前这个人是什么岗位」，也无法据此复原变更链。before 快照取
        # 变更生效时刻的**有效岗位集合**（变更后即含新岗位，因此 before 取
        # 授权前的集合 = 有效岗位集合去掉本次新增的岗位）。
        after_roles = sorted(_active_role_names(membership))
        before_roles = sorted(name for name in after_roles if name != group.name)
        AuditEvent.record(
            action="identity.role_assign",
            entity_type="StaffProfile",
            entity_id=membership.staff_profile.pk,
            actor=granted_by,
            property_obj=membership.property,
            before={
                "employee_no": membership.employee_no,
                "active_roles": before_roles,
            },
            after={
                "role": group.name,
                "ends_at": str(ends_at or "长期"),
                "employee_no": membership.employee_no,
                "active_roles": after_roles,
            },
            reason=reason or "-",
        )
        audit.info(
            "角色授权 user=%s 员工=%s 角色=%s 起=%s 止=%s 原因=%s",
            getattr(granted_by, "username", "system"),
            membership.staff_profile.full_name,
            group.name,
            starts_at.strftime("%Y-%m-%d %H:%M"),
            ends_at.strftime("%Y-%m-%d %H:%M") if ends_at else "长期",
            reason or "-",
        )
        return assignment

    @classmethod
    @transaction.atomic
    def end_assignment(cls, assignment, *, ended_by=None, reason=""):
        """结束一条角色授权（ends_at=now，不删除历史）。"""
        now = timezone.now()
        if assignment.ends_at and assignment.ends_at <= now:
            return assignment  # 已结束，幂等
        membership = assignment.membership
        # ARCH-04 H1：结束前的有效岗位集合必须在改 ends_at **之前**取，
        # 否则快照里已经没有本次结束的岗位（before 会自我抹掉）。
        before_roles = _active_role_names(membership)
        ends_at = now
        # Windows 时钟粒度（Python<3.13 time.time() 约 15.6ms）下，同一 tick 内
        # "创建授权→立即终止"会得到 ends_at == starts_at，违反
        # ck_staff_role_assignment_valid_period 的严格大于约束（全量回归偶发
        # IntegrityError）。这里钳制为 starts_at + 1µs，保证期间合法。
        if assignment.starts_at and ends_at <= assignment.starts_at:
            ends_at = assignment.starts_at + timedelta(microseconds=1)
        assignment.ends_at = ends_at
        assignment.save(update_fields=("ends_at",))
        from apps.core.models import AuditEvent

        # ARCH-04 H1：结束授权同样必须带前后值——after 记录「该岗位已失效」
        # 后的有效岗位集合，before 记录结束前的集合。
        after_roles = sorted(name for name in before_roles if name != assignment.group.name)
        AuditEvent.record(
            action="identity.role_end",
            entity_type="StaffProfile",
            entity_id=assignment.membership.staff_profile.pk,
            actor=ended_by,
            property_obj=assignment.membership.property,
            before={
                "role": assignment.group.name,
                "employee_no": assignment.membership.employee_no,
                "active_roles": before_roles,
            },
            after={
                "role": assignment.group.name,
                "ended_at": str(ends_at),
                "employee_no": assignment.membership.employee_no,
                "active_roles": after_roles,
            },
            reason=reason or "-",
        )
        audit.info(
            "角色结束 user=%s 员工=%s 角色=%s 原因=%s",
            getattr(ended_by, "username", "system"),
            assignment.membership.staff_profile.full_name,
            assignment.group.name,
            reason or "-",
        )
        return assignment

    # ---------- 档案资料维护（MC-005：不触碰任职/角色/工号事实） ----------
    @classmethod
    @transaction.atomic
    def update_profile(
        cls,
        profile,
        *,
        property_obj,
        full_name=None,
        phone=None,
        position_code=None,
        changed_by=None,
        reason="",
        request=None,
    ):
        """更新员工可纠错资料；工号、任职状态与角色授权必须走各自专用入口。"""
        from apps.core.models import AuditEvent

        reason = str(reason or "").strip()
        if not 2 <= len(reason) <= 240:
            raise EmployeeLifecycleError("资料更新必须填写 2 至 240 个字符的原因。")
        membership = cls.get_membership(profile, property_obj)
        if membership is None:
            raise EmployeeLifecycleError("员工不属于当前物业，不能维护档案资料。")
        if profile.employment_status == StaffProfile.EmploymentStatus.TERMINATED:
            raise EmployeeLifecycleError("已离职员工档案不能在日常工作台维护。")

        before = {}
        profile = type(profile).objects.select_for_update().get(pk=profile.pk)
        if full_name is not None:
            full_name = str(full_name).strip()
            if not full_name or len(full_name) > 120:
                raise EmployeeLifecycleError("姓名需为 1 至 120 个字符。")
            if profile.full_name != full_name:
                before["full_name"] = profile.full_name
                profile.full_name = full_name
        if phone is not None:
            phone = str(phone).strip()
            if len(phone) > 32:
                raise EmployeeLifecycleError("联系电话不得超过 32 个字符。")
            if profile.phone != phone:
                before["phone"] = profile.phone
                profile.phone = phone
        if before:
            profile.save(update_fields=(*before.keys(), "updated_at"))
        after = dict(before)

        membership = type(membership).objects.select_for_update().get(pk=membership.pk)
        if position_code is not None:
            position_code = str(position_code).strip()
            if not position_code or len(position_code) > 64:
                raise EmployeeLifecycleError("岗位编码需为 1 至 64 个字符。")
            if membership.position_code != position_code:
                before["membership.position_code"] = membership.position_code
                membership.position_code = position_code
                membership.save(update_fields=("position_code",))
                after["membership.position_code"] = position_code
        if not before:
            return profile, membership, False
        AuditEvent.record(
            action="identity.employee_profile_update",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=changed_by,
            property_obj=property_obj,
            before=before,
            after=after,
            reason=reason,
            request=request,
        )
        audit.info(
            "员工档案资料更新 operator=%s profile=%s fields=%s reason=%s",
            getattr(changed_by, "username", "system"),
            profile.pk,
            sorted(before),
            reason,
        )
        return profile, membership, True

    # ---------- 停用 / 离职（STAFF-09） ----------
    @classmethod
    @transaction.atomic
    def set_employment_status(
        cls, profile, *, property_obj, status, ended_by=None, reason="", deactivate_user=None
    ):
        """暂停（SUSPENDED）/ 离职（TERMINATED）/ 恢复（ACTIVE）。

        离职：终止全部当前授权、Membership 置 TERMINATED 并填 ended_at；
        deactivate_user=None 表示按策略自动处理（离职→停用账号，暂停→保留）。
        """
        # 页面使用 SUSPENDED 表示暂停任职；档案模型以 INACTIVE 记录停用状态。
        if status not in (
            StaffProfile.EmploymentStatus.ACTIVE,
            StaffProfile.EmploymentStatus.INACTIVE,
            StaffProfile.EmploymentStatus.TERMINATED,
            "SUSPENDED",
        ):
            raise EmployeeLifecycleError("员工状态无效")
        profile_status = StaffProfile.EmploymentStatus.INACTIVE if status == "SUSPENDED" else status
        # E-35：不能对自己执行暂停/离职（避免最后一个管理者把自己锁在门外）
        target_user = profile.user
        if (
            ended_by is not None
            and target_user is not None
            and ended_by.pk == target_user.pk
            and profile_status != StaffProfile.EmploymentStatus.ACTIVE
        ):
            raise EmployeeLifecycleError(
                "不能对自己执行停职/离职；如需移交请联系其他管理员或平台管理员"
            )
        profile.employment_status = profile_status
        profile.save(update_fields=("employment_status", "updated_at"))
        membership = cls.get_membership(profile, property_obj)
        # WB-041：离职（TERMINATED）→ACTIVE 是重新入职，不得直接恢复旧任职；
        # 必须走 rehire_employee 流程（新工号+新任职+重新分配岗位）。
        if (
            profile_status == StaffProfile.EmploymentStatus.ACTIVE
            and membership is not None
            and membership.status == PropertyStaffMembership.Status.TERMINATED
        ):
            raise EmployeeLifecycleError(
                "已离职员工不能直接恢复为在职；请走员工重新入职流程"
                "（DEC-EMP-01），创建新任职并重新分配岗位。"
            )
        ended_count = 0
        if membership is not None:
            if profile_status == StaffProfile.EmploymentStatus.ACTIVE:
                membership.status = PropertyStaffMembership.Status.ACTIVE
                membership.ended_at = None
            else:
                if profile_status == StaffProfile.EmploymentStatus.TERMINATED:
                    membership.status = PropertyStaffMembership.Status.TERMINATED
                    membership.ended_at = timezone.localdate()
                    for a in cls.active_assignments(membership):
                        cls.end_assignment(a, ended_by=ended_by, reason=reason or "员工离职")
                        ended_count += 1
                else:
                    membership.status = PropertyStaffMembership.Status.SUSPENDED
            membership.save()
        user = profile.user
        if user is not None:
            if deactivate_user is True or (
                deactivate_user is None
                and profile_status == StaffProfile.EmploymentStatus.TERMINATED
            ):
                user.is_active = False
                user.save(update_fields=("is_active",))
            elif profile_status == StaffProfile.EmploymentStatus.ACTIVE:
                user.is_active = True
                user.save(update_fields=("is_active",))
        from apps.core.models import AuditEvent

        AuditEvent.record(
            action="identity.employment_status",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=ended_by,
            after={"status": status, "ended_assignments": ended_count},
            reason=reason or "-",
        )
        audit.info(
            "员工状态变更 user=%s 员工=%s 状态=%s 终止授权=%d 原因=%s",
            getattr(ended_by, "username", "system"),
            profile.full_name,
            profile_status,
            ended_count,
            reason or "-",
        )
        return membership

    # ---------- 密码重置（审计只记事件，不记密码值） ----------
    @classmethod
    @transaction.atomic
    def reset_password(cls, profile, *, property_obj, new_password, reset_by=None):
        User = get_user_model()
        if (
            property_obj is None
            or not PropertyStaffMembership.objects.filter(
                staff_profile=profile, property=property_obj
            ).exists()
        ):
            raise EmployeeLifecycleError("员工不属于当前物业，不能重置账号密码。")
        if not profile.user_id:
            raise EmployeeLifecycleError("该员工尚未开通登录账号")
        # 离职守卫（用户报告：离职员工不应可重置密码）——
        # 离职 = 雇用状态 TERMINATED 且账号已停用；重置无意义并破坏生命周期语义
        if profile.employment_status == StaffProfile.EmploymentStatus.TERMINATED:
            raise EmployeeLifecycleError(
                "该员工已离职，不能重置密码；如需重新入职请先恢复其在职状态。"
            )
        _account = User.objects.filter(pk=profile.user_id).first()
        if _account is not None and not _account.is_active:
            raise EmployeeLifecycleError("该账号已停用，不能重置密码；请先恢复员工在职状态。")
        if not new_password or len(new_password) < 6:
            raise EmployeeLifecycleError("密码至少 6 位")
        user = User.objects.get(pk=profile.user_id)
        _enforce_password_policy(new_password, user=user)
        user.set_password(new_password)  # session_auth_hash 变更 → 旧会话自动失效
        user.save(update_fields=("password",))
        profile.must_change_password = True
        profile.last_password_change = timezone.now()
        profile.save(update_fields=("must_change_password", "last_password_change", "updated_at"))
        from apps.core.models import AuditEvent

        AuditEvent.record(
            action="identity.password_reset",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=reset_by,
            property_obj=property_obj,
            reason="管理员重置；审计不记录密码值",
        )
        audit.info(
            "密码重置 user=%s 目标账号=%s", getattr(reset_by, "username", "system"), user.username
        )

    # ---------- 邀请激活（AUTH-16，方案 §5.1 待激活态 / §5.4 密码策略） ----------
    @classmethod
    @transaction.atomic
    def provision_invitation_account(cls, profile, *, username, granted_by=None, property_obj=None):
        """为邀请激活预开通"待激活"账号：unusable password，激活前不可登录。

        方案 §5.1（待激活：不可登录、密码不可用）+ §5.4（邀请待激活账户使用
        unusable password）。幂等语义：
          * 档案未绑定账号 → 创建 User(password=None → unusable) 并绑定；
          * 已绑定且密码不可用（上次邀请未完成）→ 账号名一致直接复用；
            账号名不同视为管理员更正账号名（从未能登录过的账号可安全改名）；
          * 已绑定且密码可用 → 拒绝（账号已启用，走密码重置而非邀请）。
        must_change_password 保持 False：激活时员工自设密码，无需首登强制改密。
        审计复用 identity.account_provision（reason 标注邀请待激活）。
        """
        User = get_user_model()
        username = (username or "").strip()
        if not username:
            raise EmployeeLifecycleError("登录账号不能为空")
        if profile.user_id:
            user = profile.user
            if user.has_usable_password():
                raise EmployeeLifecycleError("该员工已开通登录账号，无需邀请激活")
            if user.username != username:
                if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
                    raise EmployeeLifecycleError(f"登录账号 {username} 已被占用")
                user.username = username
                user.save(update_fields=("username",))
        else:
            if User.objects.filter(username__iexact=username).exists():
                raise EmployeeLifecycleError(f"登录账号 {username} 已被占用")
            # password=None → set_unusable_password()：待激活账号没有可用凭据
            user = User.objects.create_user(
                username=username, password=None, first_name=profile.full_name
            )
            profile.user = user
            profile.save(update_fields=("user", "updated_at"))
        from apps.core.models import AuditEvent

        AuditEvent.record(
            action="identity.account_provision",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=granted_by,
            property_obj=property_obj,
            after={"account": user.username, "password": "unusable"},
            reason="邀请待激活：账号以不可用密码开通，激活前不可登录；不记录 token",
        )
        audit.info(
            "邀请待激活账号开通 user=%s 员工=%s(profile %s) 账号=%s",
            getattr(granted_by, "username", "system"),
            profile.full_name,
            profile.pk,
            user.username,
        )
        return user

    @classmethod
    @transaction.atomic
    def activate_invitation_account(cls, profile, *, password):
        """员工经邀请链接首次自行设置密码（§5.4：激活后 must_change_password=False）。

        前置：档案已绑定待激活账号（unusable password）且账号启用。
        密码策略统一走 _enforce_password_policy（携带 user 上下文做相似性校验），
        禁止任何自定义"至少 N 位"替代（§5.4）。
        审计由调用方（EmployeeInvitationService.consume）在同一事务写
        identity.invitation_accepted——本方法只负责账号与凭据。
        """
        User = get_user_model()
        if not profile.user_id:
            raise EmployeeLifecycleError("该员工还没有待激活的登录账号")
        user = User.objects.select_for_update().get(pk=profile.user_id)
        if not user.is_active:
            raise EmployeeLifecycleError("该登录账号已停用，请联系管理员重新邀请")
        if user.has_usable_password():
            raise EmployeeLifecycleError("该账号已设置密码，邀请链接不能再使用")
        _enforce_password_policy(password, user=user)
        user.set_password(password)  # session_auth_hash 变更 → 旧会话自动失效
        user.save(update_fields=("password",))
        profile.must_change_password = False
        profile.last_password_change = timezone.now()
        profile.save(update_fields=("must_change_password", "last_password_change", "updated_at"))
        audit.info(
            "邀请激活设密 员工=%s(profile %s) 账号=%s", profile.full_name, profile.pk, user.username
        )
        return user

    # ---------- 旧员工映射台账（ARCH-04 B1） ----------
    @classmethod
    @transaction.atomic
    def bind_legacy_staff_mapping(
        cls,
        profile,
        *,
        legacy_staff,
        property_obj,
        matched_by=None,
        match_method="人工确认",
        reason="",
    ):
        """登记旧 Staff 行 ↔ 新 StaffProfile 的持久映射（B1）。

        幂等：同一对（档案, 旧行）重复调用不产生第二条台账行，也不重复写审计。
        约束：
          * 映射唯一——旧行与档案各自一对一，重复绑定被库层唯一约束拒绝；
          * 一致性——台账行的 `legacy_pk` 必须等于旧行主键（模型 save() 守卫）；
          * 可追溯——记录匹配方式、状态、核验人与核验时间；
          * 删除保护——台账行不可删除；删除已映射的新档案被库层 PROTECT 拒绝。

        同时把 `StaffProfile.legacy_staff` 指向旧行（若尚未指向），保持
        「当前指向」与「映射事实」两处一致。
        """
        from apps.identity.models import LegacyStaffMapping

        if profile is None or legacy_staff is None:
            raise EmployeeLifecycleError("旧员工映射需要同时提供员工档案与旧员工记录")
        if property_obj is None:
            raise EmployeeLifecycleError("旧员工映射必须指定物业")
        existing = LegacyStaffMapping.objects.filter(staff_profile=profile).first()
        if existing is not None:
            if existing.legacy_pk != legacy_staff.pk:
                raise EmployeeLifecycleError(
                    "该员工档案已映射到另一条旧员工记录；请先处理冲突再重新映射"
                )
            return existing
        conflict = LegacyStaffMapping.objects.filter(legacy_staff=legacy_staff).first()
        if conflict is not None:
            raise EmployeeLifecycleError("该旧员工记录已映射到其他员工档案")
        mapping = LegacyStaffMapping.objects.create(
            property=property_obj,
            staff_profile=profile,
            legacy_staff=legacy_staff,
            legacy_pk=legacy_staff.pk,
            mapping_status=LegacyStaffMapping.MappingStatus.LINKED,
            match_method=match_method,
            reviewed_by=matched_by,
            reviewed_at=timezone.now(),
            note=(reason or "")[:240],
        )
        if profile.legacy_staff_id != legacy_staff.pk:
            profile.legacy_staff = legacy_staff
            profile.save(update_fields=("legacy_staff", "updated_at"))
        from apps.core.models import AuditEvent

        AuditEvent.record(
            action="identity.legacy_staff_mapped",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=matched_by,
            property_obj=property_obj,
            before={"legacy_staff_id": None},
            after={
                "legacy_staff_id": legacy_staff.pk,
                "mapping_id": mapping.pk,
                "match_method": match_method,
            },
            reason=reason or "旧员工映射登记",
        )
        return mapping

    @classmethod
    @transaction.atomic
    def recover_legacy_staff_mapping(
        cls, mapping, *, legacy_staff=None, property_obj=None, matched_by=None, reason=""
    ):
        """异常恢复：修复一条映射台账行（B1「异常可恢复」）。

        场景：映射台账行存在但 `legacy_staff` 指向被清空（旧行被清理过），
        或 `StaffProfile.legacy_staff` 指向丢失。

        **本方法不查旧表**：旧 `Staff` 行的读取只允许发生在离线映射命令
        （`manage.py link_legacy_staff`，C1 例外清单②）。调用方要么传入
        已取到的旧行实例，要么在旧行仍存在时直接用台账里的指向。
        """
        from apps.identity.models import LegacyStaffMapping

        if mapping is None:
            raise EmployeeLifecycleError("未找到要恢复的映射台账行")
        if mapping.legacy_staff_id is None:
            if legacy_staff is None:
                raise EmployeeLifecycleError(
                    "映射台账行的旧行指向已失效：请由映射命令重新提供旧员工记录后恢复"
                )
            mapping.legacy_staff = legacy_staff
            mapping.legacy_pk = legacy_staff.pk
        if property_obj is not None and mapping.property_id != property_obj.pk:
            mapping.property = property_obj
        mapping.mapping_status = LegacyStaffMapping.MappingStatus.LINKED
        mapping.reviewed_by = matched_by
        mapping.reviewed_at = timezone.now()
        if reason:
            mapping.note = reason[:240]
        mapping.save()
        profile = mapping.staff_profile
        if profile.legacy_staff_id != mapping.legacy_pk:
            profile.legacy_staff = mapping.legacy_staff
            profile.save(update_fields=("legacy_staff", "updated_at"))
        from apps.core.models import AuditEvent

        AuditEvent.record(
            action="identity.legacy_staff_mapping_recovered",
            entity_type="StaffProfile",
            entity_id=profile.pk,
            actor=matched_by,
            property_obj=mapping.property,
            before={"legacy_staff_id": None},
            after={
                "legacy_staff_id": mapping.legacy_staff_id,
                "mapping_id": mapping.pk,
            },
            reason=reason or "旧员工映射恢复",
        )
        return mapping


def Q_ends_null_or_future():
    """ends_at 为空或晚于当前的 Q 条件（避免循环 import 的小工具）。"""
    from django.db.models import Q

    now = timezone.now()
    return Q(ends_at__isnull=True) | Q(ends_at__gt=now)


# ---------- R5：自助改密二次认证（当前密码校验 + 防爆破冷却） ----------
# 背景：ARCH-05《辅助功能安全检查报告》AI 复核保留意见——自助改密缺二次认证。
# 两条改密入口（/force-password-change/ 与 /system/）在写入新密码之前，
# 必须先校验「当前密码」，避免仅凭一个已登录会话就能改掉账号凭据。
#
# 阈值与冷却时长的取值理由：
#   * 阈值 5 次：与仓库既有登录限流同口径（apps/core/rate_limit.py 的
#     DEFAULT_LIMITS["auth.login"] = IP+username 5 次 / 300 秒）。改密页不得成为
#     比登录页更廉价的口令猜测通道；5 次既能容忍员工连续手误，也把在线可猜次数
#     截断在可接受范围内。
#   * 冷却 300 秒：与登录限流窗口一致。冷却期内完全拒绝校验（即使密码正确），
#     猜测速率被压到 ≤1 次/300 秒；剩余秒数随提示返回，便于员工自行安排重试。
#
# 计数落点（2026-11-26 复核修正）：失败计数写在 RateLimitCounter（DB 内 F() 原子
# 自增），不再写在 session 里。原实现是「session 读-改-写 + 整行覆盖保存」，
# 并发请求各自读到同一旧值、各自写 fail+1，最后写者胜出 → 计数每批只 +1，
# 5 次阈值在并发下形同虚设；且会话级计数换会话即清零、无法跨会话防爆破。
# 改为 DB 原子计数后：并发下计数不丢、跨会话/跨进程有效、口径与登录限流一致。
PASSWORD_REAUTH_MAX_FAILURES = 5
PASSWORD_REAUTH_COOLDOWN_SECONDS = 300
PASSWORD_REAUTH_RATE_LIMIT_ACTION = "identity.pwd_reauth"


def _reauth_session_key(user, suffix):
    """二次认证的会话键（历史键，保留仅为兼容旧会话数据，不再作为判定依据）。"""
    return f"identity.pwd_reauth_{suffix}:{getattr(user, 'pk', 0)}"


def _audit_reauth_failure(request, user, reason):
    """二次认证失败落审计事件（不记录任何口令值）。

    reason 只取枚举值：wrong_current_password（当前密码不符）/ rate_limited（已达阈值）。
    """
    from apps.core.models import AuditEvent

    AuditEvent.record(
        action="auth.password_reauth_failure",
        entity_type="User",
        entity_id=str(getattr(user, "pk", "")),
        actor=user,
        reason=reason,
        request=request,
    )


def reset_current_password_failures(request, user):
    """清零该用户的当前密码失败计数（幂等）。

    改密成功后由视图显式调用：改密成功即视为身份重新自证，旧失败计数不得
    继续累积到下一次改密上。
    """
    from apps.core import rate_limit

    username = getattr(user, "username", "") or ""
    rate_limit.reset(request, PASSWORD_REAUTH_RATE_LIMIT_ACTION, username=username)
    session = getattr(request, "session", None)
    if session is None:
        return
    session.pop(_reauth_session_key(user, "fail"), None)
    session.pop(_reauth_session_key(user, "lock_until"), None)


def apply_password_change(request, user, new_password, *, reason):
    """改密写路径唯一入口（宪法 §3.2：User/StaffProfile 写路径收口到本服务）。

    历史实现里 `/force-password-change/`（D-07 强制改密）与 `/system/`（员工
    个人页自助改密）各自复制了一份「set_password → 清强制改密标记 → 审计 →
    保会话 → 清失败计数」写块（2026-11-28 结构审查 P1：业务规则散落在页面且
    双份重复，违反本模块头注自宣告的写边界）。两条路径现在都调用本函数。

    执行序（与历史两处实现逐字段等价，仅收敛双份重复；调用方须先通过
    validate_password_change 完成全部校验，本函数只负责落盘，不再校验）：
      ① user.set_password(new_password) + save(password)——session_auth_hash
        随之变更，其他会话全部失效；
      ② StaffProfile 清 must_change_password、记 last_password_change
        （无档案时跳过，不视为错误——与自助改密历史口径一致）；
      ③ identity.password_changed 审计——entity 取 StaffProfile 主键，
        缺档时退 User 主键；
      ④⑤ 会话侧副作用（保活 + 清失败计数）仅在 request 非空时执行：
        update_session_auth_hash 保持当前会话，
        reset_current_password_failures 清零当前密码失败计数（改密成功即
        身份重新自证）。
    """
    from django.contrib.auth import update_session_auth_hash

    from apps.core.models import AuditEvent

    user.set_password(new_password)  # session_auth_hash 变更 → 其他会话失效
    user.save(update_fields=("password",))
    profile = StaffProfile.objects.filter(user=user).first()
    if profile is not None:
        profile.must_change_password = False
        profile.last_password_change = timezone.now()
        profile.save(update_fields=("must_change_password", "last_password_change", "updated_at"))
    AuditEvent.record(
        action="identity.password_changed",
        entity_type="StaffProfile",
        entity_id=str(profile.pk if profile else user.pk),
        actor=user,
        reason=reason,
        request=request,
    )
    if request is not None:
        update_session_auth_hash(request, user)  # 保持当前会话
        # 改密成功即身份重新自证：清零本会话的当前密码失败计数
        reset_current_password_failures(request, user)


def check_new_password_not_reused(user, new_password):
    """改密口令策略：新密码不得与当前密码相同；通过返回 None，失败返回提示文案。

    背景（2026-11-27 独立复核实证）：改密入口原只校验「两次一致 + Django 校验器」，
    提交 current_password =[REDACTED] new_password1 时照样改密成功——口令实际未变，却回
    「密码已修改」并写 identity.password_changed 审计，变更记录与真实凭据不一致
    （用户以为已换新口令，实则旧口令继续有效）。二次认证已把当前密码握在手里，
    顺带做一次同值比对即可堵住。

    口径（**已裁定的强制业务政策**，负责人 2026-11-27 裁定，ARCH-05 决策表
    R1 由 WAITING_OWNER 改为 DONE）：
      * 同值改密**无条件拒绝**——原 `PASSWORD_POLICY_FORBID_REUSE` 运行时开关与
        `DJANGO_PASSWORD_FORBID_REUSE` 环境变量已移除：强制策略不得留「用环境变量
        就能在某个环境静默降级」的口子（与本项目治理的「静默策略降级」同口径）；
      * 失败文案统一为「新密码不能与当前密码相同」——不泄露任何口令内容；
      * 账号尚无可用口令（set_unusable_password）时 check_password 恒为 False，
        不误判为「相同」；
      * 回退路径：git revert 本次策略升级的提交（不保留运行时开关）。
    """
    if not new_password or user is None:
        return None
    if not getattr(user, "is_authenticated", False):
        return None
    if user.check_password(new_password):
        return "新密码不能与当前密码相同"
    return None


def validate_password_change(request, user, *, current_password, new_password1, new_password2):
    """改密统一校验入口（强制改密与自助改密共用同一套规则）。

    ARCH-05 技术整改：历史实现里 `/force-password-change/` 与 `/system/` 各自
    复制了一份「长度 → 两次一致 → Django 校验器」的分支（且强制改密多一段
    重复校验），两条路径的口径随时可能漂移。本函数是唯一入口，两条路径都调用
    它，保证「强制改密和普通改密使用一致规则」。

    校验顺序（任一不通过即返回面向用户的文案，全部通过返回 None）：
      ① 二次认证：当前密码必须正确（`check_current_password`，含冷却与审计）；
      ② 两次输入的新密码必须一致；
      ③ 最小长度（与 Django MinimumLengthValidator 同口径的 6 位下限）；
      ④ Django 密码校验器（相似度 / 常见口令 / 纯数字等）；
      ⑤ 同值策略：新密码不得与当前密码相同（强制业务政策，无运行时开关）。

    失败文案不包含任何口令内容，也不区分「账号不存在」与「密码错误」。
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return "当前密码不正确"
    # ① 二次认证闸门
    error = check_current_password(request, user, current_password)
    if error:
        return error
    new1 = new_password1 or ""
    new2 = new_password2 or ""
    # ③ 最小长度（与 Django MinimumLengthValidator 同口径的 6 位下限）
    if len(new1) < 6:
        return "密码至少 6 位"
    # ② 两次一致
    if new1 != new2:
        return "两次输入的密码不一致"
    # ④ Django 校验器
    try:
        validate_password(new1, user=user)
    except DjangoValidationError as exc:
        return "；".join(exc.messages)
    # ⑤ 同值策略（强制业务政策：无条件拒绝）
    return check_new_password_not_reused(user, new1)


def check_current_password(request, user, current_password):
    """改密二次认证：校验「当前密码」；通过返回 None，失败返回面向用户的提示文案。

    约定：
      * 失败文案统一为「当前密码不正确」——不区分账号不存在与密码错误，
        不泄露任何额外信息（用户名/账号状态/密码策略）；
      * 当前密码连续失败达 PASSWORD_REAUTH_MAX_FAILURES 次后进入
        PASSWORD_REAUTH_COOLDOWN_SECONDS 秒冷却，冷却期内即使密码正确也拒绝校验；
      * 计数是 DB 原子计数（跨会话、跨进程有效，并发不丢计数）；
      * 校验通过即清零该用户的失败计数（连续失败语义）。
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return "当前密码不正确"
    from apps.core import rate_limit

    username = getattr(user, "username", "") or ""
    # ① 先 peek：已达阈值即处于冷却期（此时即使密码正确也拒绝校验）。
    #    peek 不消耗配额——只有真正校验失败才计数（与登录限流同口径）。
    if not rate_limit.check(
        request, PASSWORD_REAUTH_RATE_LIMIT_ACTION, username=username, consume=False
    ):
        try:
            remain = rate_limit.remaining_seconds(
                request, PASSWORD_REAUTH_RATE_LIMIT_ACTION, username=username
            )
        except Exception:
            remain = 0
        if remain <= 0:
            # 无计数行却判定超限 → 只可能是计数存储异常（fail-closed）
            return "校验服务暂不可用，请稍后重试"
        _audit_reauth_failure(request, user, "rate_limited")
        return f"当前密码连续输入错误次数过多，请在 {remain} 秒后重试"
    current_password = current_password or ""
    if not current_password or not user.check_password(current_password):
        # ② 校验失败才消耗配额（DB 原子自增，并发下不丢计数）。
        rate_limit.check(request, PASSWORD_REAUTH_RATE_LIMIT_ACTION, username=username)
        _audit_reauth_failure(request, user, "wrong_current_password")
        return "当前密码不正确"
    reset_current_password_failures(request, user)
    return None
