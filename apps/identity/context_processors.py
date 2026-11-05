from django.db.models import Q
from django.utils import timezone

from apps.core.models import Notification
from apps.identity.models import StaffRoleAssignment


def pms_context(request):
    """Expose current-property effective PMS permissions to templates.
    Django's global perms wrapper is not used for PMS menus because User.groups
    is not a property-scoped authorization source.
    """
    permissions = {}
    role_names = []
    user = getattr(request, "user", None)
    membership = getattr(request, "staff_membership", None)
    if not user or not user.is_authenticated or not user.is_active or membership is None:
        return {"pms_perms": permissions, "pms_role_names": role_names,
                "pms_is_staff": False, "pms_unread_notifications": 0}

    now = timezone.now()
    assignments = (
        StaffRoleAssignment.objects.filter(membership=membership, starts_at__lte=now)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .select_related("group")
        .prefetch_related("group__permissions__content_type")
        .order_by("group__name")
    )
    for assignment in assignments:
        role_names.append(assignment.group.name)
        for permission in assignment.group.permissions.all():
            app_label = permission.content_type.app_label
            permissions.setdefault(app_label, {})[permission.codename] = True

    # §4.6 通知与待办中心：侧边栏未读徽标——与通知 API 同一"我的范围"口径
    #（当前物业 + 定向本人 或 岗位广播组∈当前有效任职组），单条 COUNT 查询。
    active_group_ids = [assignment.group_id for assignment in assignments]
    unread_notifications = (
        Notification.objects.filter(
            property_id=membership.property_id, is_read=False)
        .filter(Q(recipient_id=membership.staff_profile_id)
                | Q(role_group_id__in=active_group_ids))
        .count())

    return {"pms_perms": permissions, "pms_role_names": role_names,
            "pms_is_staff": True, "pms_unread_notifications": unread_notifications}
