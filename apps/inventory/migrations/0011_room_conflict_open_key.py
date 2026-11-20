# V12-07：RoomAssignmentConflict 并发扫描防护——可空唯一键 open_key。
#
# MySQL 8 不支持条件唯一约束（"仅当 status=OPEN 时唯一"），因此沿用本仓库
# 既有口径：用可空唯一键表达同一（物理房, 住宿日）只允许一条 OPEN 差异，
# DISMISSED 置 NULL 释放键位（NULL 不参与唯一性，可保留多条历史）。
#
# 回填策略（历史数据兼容）：
#   * 同一（房, 日）只有一条 OPEN → 直接写入复合键；
#   * 同组存在多条 OPEN（并发上线前的重复）→ 只给 pk 最小的一条写键，
#     其余保持 NULL。不在迁移里自动关闭或合并——"冲突持续存在不得自动关闭"
#     是负责人裁定边界，重复行的合并须由人工处置（见 V12-07 报告 §遗留）。
from django.db import migrations, models


def backfill_open_key(apps, schema_editor):
    Conflict = apps.get_model("inventory", "RoomAssignmentConflict")
    seen = set()
    rows = (
        Conflict.objects.filter(status="OPEN")
        .order_by("room_id", "stay_date", "pk")
        .values_list("pk", "room_id", "stay_date")
    )
    for pk, room_id, stay_date in rows.iterator():
        if room_id is None or stay_date is None:  # pragma: no cover - 防御
            continue
        key = f"{room_id}:{stay_date.isoformat()}"
        if key in seen:
            continue
        seen.add(key)
        Conflict.objects.filter(pk=pk).update(open_key=key)


def clear_open_key(apps, schema_editor):
    apps.get_model("inventory", "RoomAssignmentConflict").objects.update(open_key=None)


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0010_inventoryreconciliationrun_room_conflict_count_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="roomassignmentconflict",
            name="open_key",
            field=models.CharField(
                blank=True,
                default=None,
                help_text="OPEN 时=房/日复合键，其余为 NULL；并发扫描的唯一性仲裁依据",
                max_length=64,
                null=True,
                verbose_name="待处理唯一键",
            ),
        ),
        migrations.RunPython(backfill_open_key, clear_open_key),
        migrations.AddConstraint(
            model_name="roomassignmentconflict",
            constraint=models.UniqueConstraint(
                fields=("open_key",), name="uq_room_conflict_open_key"
            ),
        ),
    ]
