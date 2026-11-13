# Generated for front-desk lifecycle phase 2.
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q, F
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0002_alter_property_address_alter_property_code_and_more"),
        ("inventory", "0002_alter_dailyrate_closed_alter_dailyrate_created_at_and_more"),
        ("reservations", "0002_alter_reservation_adult_count_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="StayRoomSegment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("started_at", models.DateTimeField(verbose_name="入住该房间时间")),
                (
                    "ended_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="离开该房间时间"),
                ),
                (
                    "move_reason",
                    models.CharField(blank=True, max_length=240, verbose_name="变更原因"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "moved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="stay_room_segments_moved",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="操作人",
                    ),
                ),
                (
                    "property",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="stay_room_segments",
                        to="core.property",
                        verbose_name="所属酒店",
                    ),
                ),
                (
                    "room",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="stay_room_segments",
                        to="inventory.room",
                        verbose_name="实际房间",
                    ),
                ),
                (
                    "stay",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="room_segments",
                        to="reservations.stay",
                        verbose_name="实际入住",
                    ),
                ),
            ],
            options={
                "verbose_name": "入住换房历史",
                "verbose_name_plural": "入住换房历史",
                "ordering": ("started_at", "id"),
            },
        ),
        migrations.AddConstraint(
            model_name="stayroomsegment",
            constraint=models.UniqueConstraint(
                condition=Q(ended_at__isnull=True),
                fields=("stay",),
                name="uq_stay_one_active_room_segment",
            ),
        ),
        migrations.AddConstraint(
            model_name="stayroomsegment",
            constraint=models.CheckConstraint(
                check=Q(ended_at__isnull=True) | Q(ended_at__gt=F("started_at")),
                name="ck_stay_room_segment_time_order",
            ),
        ),
        migrations.CreateModel(
            name="NoShowRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("reason", models.CharField(max_length=240, verbose_name="未到店处理原因")),
                ("processed_at", models.DateTimeField(auto_now_add=True, verbose_name="处理时间")),
                (
                    "inventory_released_from",
                    models.DateField(blank=True, null=True, verbose_name="释放库存起始日"),
                ),
                (
                    "released_nights",
                    models.PositiveSmallIntegerField(default=0, verbose_name="释放住宿晚数"),
                ),
                (
                    "processed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="processed_no_show_records",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="处理人",
                    ),
                ),
                (
                    "property",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="no_show_records",
                        to="core.property",
                        verbose_name="所属酒店",
                    ),
                ),
                (
                    "reservation_room",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="no_show_record",
                        to="reservations.reservationroom",
                        verbose_name="预订房间明细",
                    ),
                ),
            ],
            options={
                "verbose_name": "未到店处理记录",
                "verbose_name_plural": "未到店处理记录",
                "ordering": ("-processed_at",),
            },
        ),
    ]
