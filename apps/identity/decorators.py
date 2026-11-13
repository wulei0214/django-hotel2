from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from .authorization import AuthorizationService


def pms_employee_required(view_func):
    """PMS 员工门禁，不使用 Django Admin 的 is_staff 标记。"""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        if not user.is_active or getattr(request, "staff_membership", None) is None:
            raise PermissionDenied("当前账号没有有效的酒店任职关系")
        return view_func(request, *args, **kwargs)

    return wrapped


def pms_permission_required(permission_code):
    """在员工门禁基础上检查当前物业的岗位权限。"""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
            if not user.is_active:
                raise PermissionDenied("账号已停用")
            property_obj = getattr(request, "property", None)
            if not AuthorizationService.has_permission(user, property_obj, permission_code):
                raise PermissionDenied("当前岗位没有执行此操作的权限")
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
