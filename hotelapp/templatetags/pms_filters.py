# -*- coding: utf-8 -*-
"""敏感字段遮罩过滤器（RBAC V2 P1-1，对应方案 §7.1/§7.3）。

设计要点：
  * 在服务端渲染阶段输出遮罩值——响应体/HTML 中不含完整敏感值（DATA-03），
    而不是用 CSS/CSS 遮罩糊弄
  * 完整值通过单独的 reveal 动作按需获取（views.py），并写审计日志
  * 车牌随停车模块隐藏，暂无遮罩需求；导出层的遮罩在 export_module 内单独处理
"""
from django import template

register = template.Library()


@register.filter
def mask_idcard(value):
    """身份证号遮罩：保留前 2 后 2（V2 §7.1 示例格式 32**************18）。"""
    v = (value or "").strip()
    if len(v) < 6:
        return v
    return v[:2] + "*" * (len(v) - 4) + v[-2:]


@register.filter
def mask_tel(value):
    """手机号遮罩：保留前 3 后 4（138****5678）。"""
    v = (value or "").strip()
    if len(v) < 8:
        return v
    return v[:3] + "****" + v[-4:]
