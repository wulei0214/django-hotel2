# -*- coding: utf-8 -*-
"""DEC-EMP-02 员工工号自动生成服务。

工号格式：部门代码 + 四位流水号（如 FO-0001、HK-0001）。
同一物业同部门原子递增；离职工号永久保留不回收。
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.identity.models import PropertyStaffMembership

DEPARTMENT_CODES = {"FO", "HK", "FIN", "MG", "ENG"}


@transaction.atomic
def generate_employee_no(*, property_obj, department_code):
    """原子分配下一个工号（部门代码 + 四位流水号）。

    在同一事务中查 MAX(employee_no) 并递增，避免 MAX+1 竞态；
    由外层事务保证创建失败时序列回滚。
    """
    code = str(department_code or "").strip().upper()
    if code not in DEPARTMENT_CODES:
        raise ValueError(
            f"部门代码 {code} 不在受控目录中；允许值：{sorted(DEPARTMENT_CODES)}")
    prefix = f"{code}-"
    last = (
        PropertyStaffMembership.objects
        .select_for_update()
        .filter(property=property_obj, employee_no__startswith=prefix)
        .order_by("-employee_no")
        .values_list("employee_no", flat=True)
        .first()
    )
    if last is None:
        next_seq = 1
    else:
        try:
            next_seq = int(last[len(prefix):]) + 1
        except ValueError:
            raise ValueError(
                f"已有工号 {last} 不符合 {prefix}XXXX 格式，无法自动递增。")
    if next_seq > 9999:
        raise ValueError(f"部门 {code} 工号流水号已达到 9999 上限。")
    return f"{prefix}{next_seq:04d}"


@transaction.atomic
def rehire_employee(*, property_obj, staff_profile, department_code,
                    position_code, started_at=None, granted_by=None):
    """DEC-EMP-01 员工重新入职：同一 StaffProfile 在同一物业创建新任职。

    前提：旧任职已离职（status=TERMINATED）；新任职不继承旧权限。
    工号由自动编号服务分配（不继承旧工号，旧工号保留给历史记录）。
    返回新 PropertyStaffMembership。
    """
    from apps.identity.services import EmployeeLifecycleService
    old_membership = PropertyStaffMembership.objects.filter(
        property=property_obj, staff_profile=staff_profile).order_by("-started_at").first()
    if old_membership is None:
        raise ValueError("该员工在本物业没有历史任职记录，请使用 create_employee 创建。")
    if old_membership.status != PropertyStaffMembership.Status.TERMINATED:
        raise ValueError(
            f"旧任职状态为 {old_membership.status}，只有已离职（TERMINATED）的任职才能重新入职。")
    # 确保旧角色分配已终止
    from apps.identity.models import StaffRoleAssignment
    active_roles = StaffRoleAssignment.objects.filter(
        membership=old_membership, ends_at__isnull=True)
    for ra in active_roles:
        ra.ends_at = timezone.now()
        ra.save(update_fields=("ends_at",))
    # 自动生成新工号（不复用旧工号）
    new_no = generate_employee_no(
        property_obj=property_obj, department_code=department_code)
    new_membership = PropertyStaffMembership.objects.create(
        property=property_obj, staff_profile=staff_profile,
        employee_no=new_no, position_code=position_code,
        status=PropertyStaffMembership.Status.ACTIVE,
        started_at=started_at or timezone.now())
    AuditEvent_record_rehire(new_membership, old_membership, granted_by)
    return new_membership


def AuditEvent_record_rehire(new_membership, old_membership, granted_by):
    from apps.core.models import AuditEvent
    AuditEvent.record(
        action="identity.employee_rehired", entity_type="PropertyStaffMembership",
        entity_id=new_membership.pk, actor=granted_by,
        property_obj=new_membership.property,
        after={"employee_no": new_membership.employee_no,
               "position_code": new_membership.position_code,
               "old_membership_id": old_membership.pk,
               "old_employee_no": old_membership.employee_no},
        reason="员工重新入职（DEC-EMP-01）；旧任职保留，新工号自动生成")
