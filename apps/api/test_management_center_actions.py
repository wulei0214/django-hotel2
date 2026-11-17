"""MC-002 通用管理中心动作白名单（按权限显隐，不造通用 CRUD）。"""

from django.test import override_settings

from apps.api.test_e2e_flows import E2EFlowTestBase


@override_settings(DEBUG=True, DJANGO_ENV="test")
class ManagementCenterActionsTests(E2EFlowTestBase):
    def setUp(self):
        super().setUp()
        self.manager = self._make_staff("mc_action_manager", "店长")
        self.fd = self._make_staff("mc_action_front", "前台")
        self.finance = self._make_finance_view_only("mc_action_finance")

    def test_guest_center_exposes_only_actions_current_role_can_use(self):
        self.client.force_login(self.fd)
        response = self.client.get("/manage/guests/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pms-center-actions")
        self.assertContains(response, "guest_create")
        self.assertContains(response, "phone_add")

        self.client.force_login(self.finance)
        self.assertEqual(self.client.get("/manage/guests/").status_code, 403)

    def test_room_center_exposes_workflow_links_but_no_generic_crud(self):
        self.client.force_login(self.manager)
        response = self.client.get("/manage/rooms/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rate_close")
        self.assertContains(response, "/manage/housekeeping/")
        self.assertNotContains(response, "通用编辑")
        self.assertContains(response, "X-CSRFToken")

    def test_reservation_center_points_to_single_front_desk_workflow(self):
        self.client.force_login(self.fd)
        response = self.client.get("/manage/reservations/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "/checkin/")
        self.assertNotContains(response, "reservation_create")
