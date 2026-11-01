import logging

from django.conf import settings

from apps.core.models import Property
from .models import PropertyStaffMembership

logger = logging.getLogger("hotel.audit")


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

        # V2 SCOPE-01/P1-6：单店阶段仅 DEFAULT 物业生效——
        # 员工绑定到其他物业（或伪造会话 active_property_id 指向别店）时，
        # 忽略该任职并回退 DEFAULT 上下文，同时记录告警审计
        if membership is not None and membership.property.code != getattr(
                settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"):
            logger.warning(
                "SCOPE 告警：用户 %s 的任职物业 %s 非 DEFAULT，已拒绝并回退单店上下文",
                request.user.username, membership.property.code)
            membership = None
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
