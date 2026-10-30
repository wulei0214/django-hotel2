import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F, Q


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("core", "0001_initial"),
        ("guests", "0001_initial"),
        ("inventory", "0001_initial"),
    ]
    operations = [
        migrations.CreateModel(
            name="Reservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("booking_no", models.CharField(max_length=32, unique=True)),
                ("source", models.CharField(default="WEBSITE", max_length=24)),
                ("status", models.CharField(choices=[("PENDING", "待支付/待确认"), ("CONFIRMED", "已确认"), ("CANCELLED", "已取消"), ("EXPIRED", "已过期"), ("CHECKED_IN", "已入住"), ("CHECKED_OUT", "已退房"), ("NO_SHOW", "未到店")], default="PENDING", max_length=20)),
                ("check_in", models.DateField()),
                ("check_out", models.DateField()),
                ("room_count", models.PositiveSmallIntegerField(default=1)),
                ("adult_count", models.PositiveSmallIntegerField(default=1)),
                ("child_count", models.PositiveSmallIntegerField(default=0)),
                ("total_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("deposit_amount", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("currency", models.CharField(default="CNY", max_length=3)),
                ("idempotency_key", models.CharField(blank=True, max_length=128, null=True)),
                ("request_fingerprint", models.CharField(blank=True, max_length=64)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("guest", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reservations", to="guests.guest")),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reservations", to="core.property")),
            ],
            options={"verbose_name": "预订主单", "verbose_name_plural": "预订主单", "ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="ReservationRoom",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("check_in", models.DateField()),
                ("check_out", models.DateField()),
                ("nightly_rates", models.JSONField(default=list)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assigned_room", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reservation_rooms", to="inventory.room")),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reservation_rooms", to="core.property")),
                ("rate_plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reservation_rooms", to="inventory.rateplan")),
                ("reservation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reservation_rooms", to="reservations.reservation")),
                ("room_type", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reservation_rooms", to="inventory.roomtype")),
            ],
            options={"verbose_name": "预订房间明细", "verbose_name_plural": "预订房间明细", "ordering": ("id",)},
        ),
        migrations.CreateModel(
            name="Stay",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("actual_check_in", models.DateTimeField(blank=True, null=True)),
                ("actual_check_out", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("IN_HOUSE", "在住"), ("CHECKED_OUT", "已退房"), ("VOIDED", "已作废")], default="IN_HOUSE", max_length=16)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("property", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stays", to="core.property")),
                ("reservation_room", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="stay", to="reservations.reservationroom")),
                ("room", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="stays", to="inventory.room")),
            ],
            options={"verbose_name": "实际入住记录", "verbose_name_plural": "实际入住记录", "ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(model_name="reservation", constraint=models.UniqueConstraint(fields=("property", "idempotency_key"), name="uq_reservation_property_idempotency")),
        migrations.AddConstraint(model_name="reservation", constraint=models.CheckConstraint(check=Q(check_out__gt=F("check_in")), name="ck_reservation_checkout_after_checkin")),
        migrations.AddConstraint(model_name="reservation", constraint=models.CheckConstraint(check=Q(room_count__gte=1), name="ck_reservation_rooms_positive")),
        migrations.AddConstraint(model_name="reservation", constraint=models.CheckConstraint(check=Q(adult_count__gte=1), name="ck_reservation_adults_positive")),
        migrations.AddConstraint(model_name="reservationroom", constraint=models.CheckConstraint(check=Q(check_out__gt=F("check_in")), name="ck_reservation_room_checkout_after_checkin")),
        migrations.AddConstraint(model_name="reservationroom", constraint=models.CheckConstraint(check=Q(amount__gte=0), name="ck_reservation_room_amount_nonnegative")),
        migrations.AddIndex(model_name="reservation", index=models.Index(fields=["property", "status", "check_in"], name="reservations_reserv_propert_77727c_idx")),
        migrations.AddIndex(model_name="stay", index=models.Index(fields=["property", "status"], name="reservations_stay_propert_1fc1d7_idx")),
    ]
