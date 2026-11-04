import logging
# 请求上下文提示：当前实现按 DEFAULT_PROPERTY_CODE 单店上下文工作（P1-06 边界）。
# Property 模型可关联多物业不代表登录/切换链路已支持多物业；
# 不得把无效物业选择静默解释为另一个物业。

from django.conf import settings

from apps.core.models import Property
from .models import PropertyStaffMembership
from .session_middleware import is_admin_request

logger = logging.getLogger("hotel.audit")


class PropertyContextMiddleware:
    """解析当前请求的物业上下文（P1-06 单店边界，方案 §4.2）。

    - 匿名顾客：使用部署配置的 DEFAULT 物业提供公开面。
    - 员工：取其在 DEFAULT 物业的有效任职；不接受会话/客户端切换物业。
    - 任职在其他物业（含伪造指向别店）：明确拒绝——本次请求不提供物业上下文
      （后续授权判定必然失败），并写安全审计；禁止静默回退 DEFAULT 的半状态。
    - `active_property_id` 会话切换已移除：多物业登录/切换须另立专项。
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        property_obj = None
        membership = None

        if is_admin_request(request):
            # 平台路径（/admin/、/platform/、/permissions/）不属于 PMS 物业上下文域
            # （缺口 F-10）：平台超管无 DEFAULT 任职是正常状态，跳过任职解析，
            # 不产生 security.property_context_denied 审计噪声。
            request.property = None
            request.staff_membership = None
            request.staff_profile = None
            return self.get_response(request)

        if request.user.is_authenticated and request.user.is_active:
            membership = (
                PropertyStaffMembership.objects.select_related(
                    "staff_profile", "property"
                )
                .filter(
                    staff_profile__user=request.user,
                    staff_profile__employment_status="ACTIVE",
                    status=PropertyStaffMembership.Status.ACTIVE,
                    property__status=Property.Status.ACTIVE,
                    property__code=getattr(
                        settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"),
                )
                .order_by("id")
                .first()
            )
            if membership is None:
                # 员工没有 DEFAULT 有效任职（含任职在别店）：明确拒绝并审计
                from apps.core.models import AuditEvent
                logger.warning(
                    "SCOPE 拒绝：用户 %s 无 DEFAULT 有效任职，本次请求不提供物业上下文",
                    request.user.username)
                AuditEvent.record(
                    action="security.property_context_denied",
                    entity_type="User",
                    entity_id=str(request.user.pk),
                    reason="no_default_membership",
                    request=request)

        if membership is not None:
            property_obj = membership.property
        elif not request.user.is_authenticated:
            # 匿名客人按部署配置的 DEFAULT 物业提供公开面
            property_obj = Property.objects.filter(
                code=getattr(settings, "DEFAULT_PROPERTY_CODE", "DEFAULT"),
                status=Property.Status.ACTIVE,
            ).first()
        # 已认证但无 DEFAULT 任职：property_obj 保持 None（视图层拒绝）

        request.property = property_obj
        request.staff_membership = membership
        request.staff_profile = (
            membership.staff_profile if membership is not None else None
        )
        return self.get_response(request)
