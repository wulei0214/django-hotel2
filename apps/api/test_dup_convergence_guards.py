"""TD-2 重复逻辑收敛护栏：钉住「唯一权威」引用关系，防止副本再分叉。

phase0 代码精简（70-cleanup-services.md）将同语义实现收敛到唯一权威后，
用对象同一性（is）断言锁定跨模块引用；任何人重新复制实现会导致这些
断言失败，从而强制走收敛而非复制。
"""

from django.test import TestCase


class DuplicateConvergenceGuardsTests(TestCase):
    """同一性护栏：消费方必须引用权威实现，而非本地副本。"""

    def test_property_today_single_authority_in_inventory_services(self):
        from apps.api import front_desk, report_center
        from apps.inventory import services as inventory_services
        from apps.reservations import front_desk_services, lifecycle_services

        authority = inventory_services._property_today
        self.assertIs(front_desk._property_today, authority)
        self.assertIs(report_center._property_today, authority)
        self.assertIs(front_desk_services._property_today, authority)
        self.assertIs(lifecycle_services._property_today, authority)

    def test_body_pairs_converged(self):
        from apps.api import front_desk, housekeeping_center, night_audit, views

        # ValueError 口径对：front_desk 复用 views._body
        self.assertIs(front_desk._body, views._body)
        # ApiError 口径对：housekeeping 复用 night_audit._body
        self.assertIs(housekeeping_center._body, night_audit._body)

    def test_body_api_error_centers_use_core_parse_json_object(self):
        """TASK-04：四个 ApiError 口径中心的请求体解析收敛到 core 唯一实现。

        收敛对象：guest_subresources、housekeeping_workflow_center、
        inventory_admin_center、task_center —— 这四份 `_body` 曾逐字相同。
        不并入本权威的两处属**有意的口径差异**，不得为「减重复」合并：
        - `apps/api/views.py::_body` 抛 `ValueError`（未包 ApiError 封套）；
        - `apps/api/night_audit.py::_body` 允许空体回落且文案为「有效 JSON」。
        """
        from apps.api import (
            guest_subresources,
            housekeeping_workflow_center,
            inventory_admin_center,
            task_center,
        )
        from apps.core.api_contract import parse_json_object

        for module in (
            guest_subresources,
            housekeeping_workflow_center,
            inventory_admin_center,
            task_center,
        ):
            with self.subTest(module=module.__name__):
                self.assertFalse(
                    hasattr(module, "_body"),
                    f"{module.__name__} 不得再定义本地 _body 副本",
                )
                self.assertIs(module.parse_json_object, parse_json_object)

    def test_time_boundary_helpers_single_authority_in_report_center(self):
        from apps.api import audit_center, report_center

        self.assertIs(audit_center._property_tz_bounds, report_center._property_tz_bounds)
        self.assertIs(audit_center._parse_day, report_center._parse_day)

    def test_money_str_single_authority_in_billing_center(self):
        from apps.api import billing_center, report_center

        self.assertIs(report_center._money_str, billing_center._money_str)

    def test_models_q_valid_to_single_authority_in_inventory_services(self):
        from apps.inventory import services as inventory_services
        from apps.reservations import services as reservation_services

        self.assertIs(reservation_services.models_q_valid_to, inventory_services.models_q_valid_to)
