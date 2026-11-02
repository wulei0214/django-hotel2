from django.db.models import Q
from django.utils import timezone

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
        return {"pms_perms": permissions, "pms_role_names": role_names}

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

    return {"pms_perms": permissions, "pms_role_names": role_names}
