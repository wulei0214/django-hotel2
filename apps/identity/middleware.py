from django.conf import settings

from apps.core.models import Property
from .models import PropertyStaffMembership


class PropertyContextMiddleware:
    """解析当前请求的物业上下文。

    单店版匿名顾客使用 DEFAULT 物业；员工请求按会话选择的物业查找有效任职，
    没有选择时默认使用其第一条有效任职。客户端提交的任意 property_id 不可信。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        property_obj = None
        membership = None

        if request.user.is_authenticated and request.user.is_active:
            memberships = (
                PropertyStaffMembership.objects.select_related(
                    "staff_profile", "property"
                )
                .filter(
                    staff_profile__user=request.user,
                    staff_profile__employment_status="ACTIVE",
                    status=PropertyStaffMembership.Status.ACTIVE,
                    property__status=Property.Status.ACTIVE,
                )
                .order_by("id")
            )
            selected_id = request.session.get("active_property_id")
            if selected_id is not None:
                try:
                    membership = memberships.filter(property_id=int(selected_id)).first()
                except (TypeError, ValueError):
                    membership = None
            if membership is None:
                membership = memberships.first()

        if membership is not None:
            property_obj = membership.property
        else:
            property_obj = Property.objects.filter(
                code=getattr(settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"),
                status=Property.Status.ACTIVE,
            ).first()

        request.property = property_obj
        request.staff_membership = membership
        request.staff_profile = (
            membership.staff_profile if membership is not None else None
        )
        return self.get_response(request)
