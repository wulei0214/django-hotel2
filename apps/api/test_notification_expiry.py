"""通知过期与源业务销项语义回归。"""

from datetime import timedelta

from django.utils import timezone

from apps.api.test_notification_center import NotificationCenterTestBase
from apps.core.models import Notification, NotificationRead
from apps.core.services_notifications import refresh_operational_todos
from apps.reservations.models import Reservation


class NotificationExpiryTests(NotificationCenterTestBase):
    def test_expired_notification_is_excluded_from_all_read_views(self):
        notification = self._direct_notification(self.fd, title="过期通知")
        notification.expires_at = timezone.now() - timedelta(minutes=1)
        notification.save(update_fields=["expires_at"])

        listed = self.client.get("/api/v1/notifications")
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(notification.pk, {row["id"] for row in listed.json()["data"]})

        unread = self.client.get("/api/v1/notifications/unread-count")
        self.assertEqual(unread.json()["data"]["unread_count"], 0)

        marked = self.client.post(f"/api/v1/notifications/{notification.pk}/read")
        self.assertEqual(marked.status_code, 404)

        read_all = self.client.post("/api/v1/notifications/read-all")
        self.assertEqual(read_all.json()["data"]["marked_read"], 0)
        self.assertFalse(NotificationRead.objects.filter(notification=notification).exists())

    def test_resolved_todo_closes_without_marking_personal_read(self):
        reservation = self._reservation(status=Reservation.Status.PENDING, check_in_offset=5)
        refresh_operational_todos(self.property)
        todos = Notification.objects.filter(property=self.property, dedupe_key__isnull=False)
        self.assertTrue(todos.exists())

        Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CONFIRMED)
        refresh_operational_todos(self.property)

        self.assertFalse(NotificationRead.objects.filter(notification__in=todos).exists())
        self.assertTrue(all(row.resolved_at is not None for row in todos))
