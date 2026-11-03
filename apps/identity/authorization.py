from django.core.exceptions import FieldDoesNotExist
# 安全边界提示：业务权限必须结合认证用户、当前物业、有效员工档案、有效任职和岗位分配计算。菜单隐藏不是授权；页面与 API 都必须服务端复核。平台 superuser 不自动等同于门店岗位。
from django.db.models import Q
from django.utils import timezone

from .models import PropertyStaffMembership, StaffRoleAssignment


class AuthorizationService:
    """统一处理员工身份、物业范围与岗位权限。

    Django User.groups / User.has_perm 不包含物业上下文，因此 PMS 岗位组
    只挂在 StaffRoleAssignment 上，由本服务按当前物业解析。
    """

    @staticmethod
    def get_active_membership(user, property_obj):
        if (
            not getattr(user, "is_authenticated", False)
            or not getattr(user, "is_active", False)
            or property_obj is None
            or property_obj.status != "ACTIVE"
        ):
            return None

        return (
            PropertyStaffMembership.objects.select_related("staff_profile", "property")
            .filter(
                staff_profile__user=user,
                staff_profile__employment_status="ACTIVE",
                property=property_obj,
                status=PropertyStaffMembership.Status.ACTIVE,
            )
            .first()
        )

    @classmethod
    def has_permission(cls, user, property_obj, permission_code, obj=None):
        """检查 user 在 property 上是否拥有 app_label.codename 权限。

        superuser 不自动绕过业务岗位授权；平台技术权限与酒店经营权限分离。
        """
        membership = cls.get_active_membership(user, property_obj)
        if membership is None:
            return False

        if obj is not None and hasattr(obj, "property_id"):
            if obj.property_id != property_obj.pk:
                return False

        try:
            app_label, codename = permission_code.split(".", 1)
        except (AttributeError, ValueError):
            return False
        if not app_label or not codename:
            return False

        now = timezone.now()
        return StaffRoleAssignment.objects.filter(
            membership=membership,
            starts_at__lte=now,
        ).filter(
            Q(ends_at__isnull=True) | Q(ends_at__gt=now)
        ).filter(
            group__permissions__content_type__app_label=app_label,
            group__permissions__codename=codename,
        ).exists()

    @classmethod
    def filter_queryset_for_property(cls, queryset, user, property_obj):
        """给物业级业务 QuerySet 加服务端数据范围过滤。"""
        if cls.get_active_membership(user, property_obj) is None:
            return queryset.none()

        try:
            queryset.model._meta.get_field("property")
        except FieldDoesNotExist as exc:
            raise ValueError(
                f"{queryset.model.__name__} is not a property-scoped model"
            ) from exc
        return queryset.filter(property_id=property_obj.pk)
