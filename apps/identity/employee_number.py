"""DEC-EMP-02 员工工号自动生成服务 + DEC-EMP-01 员工重新入职生命周期。

工号格式：部门代码 + 四位流水号（如 FO-0001、HK-0001）。
同一物业同部门原子递增（WB-101：EmployeeNumberCounter 行锁，不再 MAX+1）；
离职工号永久保留不回收（DEC-EMP-01）。
"""

import datetime
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.identity.models import (
    EmployeeNumberCounter,
    PropertyStaffMembership,
    StaffProfile,
    StaffRoleAssignment,
)

audit = logging.getLogger("hotel.audit")

DEPARTMENT_CODES = {"FO", "HK", "FIN", "MG", "ENG"}
MAX_SEQUENCE = 9999


def _seed_next_seq(property_obj, code):
    """WB-101：计数器行缺失时的一次性初始化种子（旧 MAX+1 语义仅作种子）。

    只在 get_or_create 新建计数器行的同一事务内计算一次：按已有工号前缀
    查最大流水号 +1，保证存量手工工号的物业无缝切换到计数器递增；计数器
    行一旦存在即不再回读 Membership（避免 MAX+1 竞态，编号只由行锁递增）。
    种子计算发生在计数器行锁之内，且并发下只有真正建行的那个事务会执行
    本函数（get_or_create 唯一约束保证），重复执行也是幂等同值。
    """
    prefix = f"{code}-"
    max_seq = 0
    existing = PropertyStaffMembership.objects.filter(
        property=property_obj, employee_no__startswith=prefix
    ).values_list("employee_no", flat=True)
    for employee_no in existing:
        suffix = employee_no[len(prefix) :]
        try:
            seq = int(suffix)
        except ValueError as exc:
            raise ValueError(
                f"已有工号 {employee_no} 不符合 {prefix}XXXX 格式，" "无法初始化编号计数器。"
            ) from exc
        max_seq = max(max_seq, seq)
    return max_seq + 1


@transaction.atomic
def generate_employee_no(*, property_obj, department_code):
    """原子分配下一个工号（部门代码 + 四位流水号）。

    WB-101：事务内对 EmployeeNumberCounter 行 select_for_update 行锁——
    get_or_create 只保证行存在，随后立即以 select_for_update 重取行锁，
    锁定后再读 next_seq → 递增落库，与外层 Membership 创建同事务提交；
    并发分配由行锁串行化，不再依赖 MAX+1 查询。由外层事务保证建档失败
    时序列随事务回滚（编号不消耗）。
    （SQLite 后端无 SELECT ... FOR UPDATE，为 no-op；写串行化由该库的
    单写者语义承担，逻辑路径与 MySQL/PostgreSQL 一致。）
    """
    code = str(department_code or "").strip().upper()
    if code not in DEPARTMENT_CODES:
        raise ValueError(f"部门代码 {code} 不在受控目录中；允许值：{sorted(DEPARTMENT_CODES)}")
    prefix = f"{code}-"
    # WB-101 收尾：空部门首次并发分配时，两个事务可能同时 get_or_create
    # 计数器行，后者撞唯一约束抛 IntegrityError。用 savepoint 隔离后重取
    # 已存在的计数器行，继续走同一把行锁，避免「一个成功、另一个整单失败」。
    try:
        with transaction.atomic():
            counter, created = EmployeeNumberCounter.objects.get_or_create(
                property=property_obj, department_code=code, defaults={"next_seq": 1}
            )
    except IntegrityError:
        counter = EmployeeNumberCounter.objects.get(property=property_obj, department_code=code)
        created = False
    # 锁必须落在读取 next_seq 之前：重取加锁后才开始读-增-写
    counter = EmployeeNumberCounter.objects.select_for_update().get(pk=counter.pk)
    if created:
        # 一次性种子：计数器行首建时从存量工号最大流水号接续
        seed = _seed_next_seq(property_obj, code)
        if seed > counter.next_seq:
            counter.next_seq = seed
    next_seq = counter.next_seq
    if next_seq > MAX_SEQUENCE:
        raise ValueError(f"部门 {code} 工号流水号已达到 {MAX_SEQUENCE} 上限。")
    counter.next_seq = next_seq + 1
    counter.save(update_fields=("next_seq", "updated_at"))
    return f"{prefix}{next_seq:04d}"


@transaction.atomic
def rehire_employee(
    *, property_obj, staff_profile, department_code, position_code, started_at=None, granted_by=None
):
    """DEC-EMP-01/WB-099 员工重新入职：同一 StaffProfile 在同一物业创建新任职。

    完整生命周期在同一事务内完成：
      ① 只有档案 TERMINATED 的离职员工才能再入职（否则 ValueError 指向
         EmployeeLifecycleService.set_employment_status——暂停/恢复不归本流程）；
      ② 恢复档案 ACTIVE 与登录账号 is_active=True（无账号允许只建档）；
      ③ 旧任职的生效授权统一经 EmployeeLifecycleService.end_assignment 关闭
         （复用其审计与 Windows 时钟钳制边界，不绕过服务层直改 ends_at）；
      ④ started_at 为 DateField：缺省 timezone.localdate()，传 datetime 归一化为日期；
      ⑤ 旧工号永久保留，新工号由编号服务按部门自动分配（不继承旧工号）；
      ⑥ 审计 identity.employee_rehire 完整记录档案/账号/新旧任职/授权变更。
    返回新 PropertyStaffMembership。
    """
    from apps.core.models import AuditEvent
    from apps.identity.services import EmployeeLifecycleService

    if staff_profile.employment_status != StaffProfile.EmploymentStatus.TERMINATED:
        raise ValueError(
            "只有离职（TERMINATED）员工才能重新入职；当前档案状态为 "
            f"{staff_profile.employment_status}，如需暂停/恢复请使用 "
            "EmployeeLifecycleService.set_employment_status。"
        )
    old_membership = (
        PropertyStaffMembership.objects.select_for_update()
        .filter(property=property_obj, staff_profile=staff_profile)
        .order_by("-started_at", "-id")
        .first()
    )
    if old_membership is None:
        raise ValueError("该员工在本物业没有历史任职记录，请使用 create_employee 创建。")
    if old_membership.status != PropertyStaffMembership.Status.TERMINATED:
        raise ValueError(
            f"旧任职状态为 {old_membership.status}，" "只有已离职（TERMINATED）的任职才能重新入职。"
        )
    # ③ 旧授权统一结束（服务层：ends_at 落定 + identity.role_end 审计 + 历史保留）
    old_assignments = EmployeeLifecycleService.active_assignments(old_membership)
    all_old_assignment_ids = list(
        StaffRoleAssignment.objects.filter(membership=old_membership).values_list("pk", flat=True)
    )
    for assignment in old_assignments:
        EmployeeLifecycleService.end_assignment(
            assignment, ended_by=granted_by, reason="员工重新入职：旧任职授权终止（DEC-EMP-01）"
        )
    # ⑤ 新工号由编号服务分配（旧工号保留给历史记录，不复用）
    new_no = generate_employee_no(property_obj=property_obj, department_code=department_code)
    # ④ started_at 是 DateField：缺省今天；显式传入 datetime 时归一化为日期
    if started_at is None:
        started_at = timezone.localdate()
    elif isinstance(started_at, datetime.datetime):
        started_at = (
            timezone.localtime(started_at).date()
            if timezone.is_aware(started_at)
            else started_at.date()
        )
    new_membership = PropertyStaffMembership.objects.create(
        property=property_obj,
        staff_profile=staff_profile,
        employee_no=new_no,
        position_code=position_code,
        status=PropertyStaffMembership.Status.ACTIVE,
        started_at=started_at,
    )
    # ② 恢复档案与账号可用状态（无账号时允许只建档）
    staff_profile.employment_status = StaffProfile.EmploymentStatus.ACTIVE
    staff_profile.save(update_fields=("employment_status", "updated_at"))
    user = staff_profile.user
    account_username = None
    if user is not None:
        account_username = user.username
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=("is_active",))
    # ⑥ 完整审计：档案/账号/新旧任职/授权变更一次记录
    AuditEvent.record(
        action="identity.employee_rehire",
        entity_type="StaffProfile",
        entity_id=staff_profile.pk,
        actor=granted_by,
        property_obj=property_obj,
        before={
            "profile_status": StaffProfile.EmploymentStatus.TERMINATED,
            "account": account_username,
            "account_active": False,
            "old_membership": {
                "id": old_membership.pk,
                "employee_no": old_membership.employee_no,
                "status": old_membership.status,
                "ended_at": str(old_membership.ended_at or ""),
            },
            # 旧任职全部授权历史；ended_assignment_ids 为本次再入职终止的部分
            # （若离职时已统一终止则为空，防御性兜底历史数据遗漏）
            "old_assignment_ids": all_old_assignment_ids,
            "ended_assignment_ids": [a.pk for a in old_assignments],
        },
        after={
            "profile_status": StaffProfile.EmploymentStatus.ACTIVE,
            "account": account_username,
            "account_active": True,
            "new_membership": {
                "id": new_membership.pk,
                "employee_no": new_membership.employee_no,
                "position_code": new_membership.position_code,
                "status": new_membership.status,
                "started_at": str(new_membership.started_at),
            },
        },
        reason="员工重新入职（DEC-EMP-01/WB-099）：旧任职与旧工号保留，"
        "新工号自动生成，旧授权统一终止",
    )
    audit.info(
        "员工重新入职 user=%s 员工=%s(profile %s) 旧工号=%s 新工号=%s 账号=%s 终止授权=%d",
        getattr(granted_by, "username", "system"),
        staff_profile.full_name,
        staff_profile.pk,
        old_membership.employee_no,
        new_membership.employee_no,
        account_username or "未开通",
        len(old_assignments),
    )
    return new_membership
