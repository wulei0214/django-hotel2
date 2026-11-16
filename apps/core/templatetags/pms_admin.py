"""Django Admin 首页使用的 PMS2 业务快照模板标签。"""

from django import template

from apps.core.admin_views import _snapshot

register = template.Library()


@register.simple_tag
def pms_business_snapshot():
    """返回前台业务事实的实时快照，供后台首页替代空的 admin action log。"""
    return _snapshot()
