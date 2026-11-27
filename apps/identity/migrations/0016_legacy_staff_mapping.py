# -*- coding: utf-8 -*-
"""ARCH-04 B1：旧 Staff 行 ↔ 新 StaffProfile 的持久映射台账。

本迁移除建表外，还把**历史上已经建立**的 `StaffProfile.legacy_staff` 外键映射
回填进台账——否则台账只对新映射生效，历史映射仍然「只存在于一个外键里」，
不满足 B1「可靠持久关联 + 可追溯」。

回填口径：
  * 映射状态 = LINKED（历史外键本身即人工确认过的指向）；
  * 匹配方式 = 「历史外键回填」；
  * 物业 = 该员工档案最早的一条任职所属物业；无任职时取 DEFAULT 物业
    （DEFAULT 不存在则跳过该行——不凭空造物业）。

**唯一约束与索引随建表内联创建**（不单独 AddConstraint / AddIndex）：
MySQL 会把 `(property_id, legacy_pk)` 上的唯一索引当作 `property_id` 外键的
支撑索引；单独 AddConstraint 的反向迁移要 `DROP CONSTRAINT`，而 MySQL 里它
其实是索引 → 报 1091（Can't DROP ... check that column/key exists）；
若改成单独 AddIndex 又会在外键仍在索引里时报 1553。内联创建后反向迁移直接
DROP TABLE，两种问题都不出现（与 identity/0015 的处理口径一致）。

反向：清空回填行（台账表随 CreateModel 的反向一起删除）。
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def _backfill(apps, schema_editor):
    """把既有 StaffProfile.legacy_staff 映射写入台账（幂等）。"""
    StaffProfile = apps.get_model("identity", "StaffProfile")
    Membership = apps.get_model("identity", "PropertyStaffMembership")
    Property = apps.get_model("core", "Property")
    Mapping = apps.get_model("identity", "LegacyStaffMapping")

    default_property = Property.objects.filter(code="DEFAULT").first()
    for profile in StaffProfile.objects.exclude(legacy_staff_id=None).iterator():
        membership = (
            Membership.objects.filter(staff_profile=profile)
            .order_by("id")
            .select_related("property")
            .first()
        )
        prop = membership.property if membership is not None else default_property
        if prop is None:
            continue
        Mapping.objects.get_or_create(
            staff_profile=profile,
            defaults={
                "property": prop,
                "legacy_staff_id": profile.legacy_staff_id,
                "legacy_pk": profile.legacy_staff_id,
                "mapping_status": "LINKED",
                "match_method": "历史外键回填",
            },
        )


def _unbackfill(apps, schema_editor):
    """反向：清空台账（表随后由 CreateModel 的反向删除）。"""
    Mapping = apps.get_model("identity", "LegacyStaffMapping")
    Mapping.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0020_outboxevent"),
        ("hotelapp", "0022_release_legacy_staff_foreign_keys"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("identity", "0015_legacy_permission_code_grant"),
    ]

    operations = [
        migrations.CreateModel(
            name="LegacyStaffMapping",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("legacy_pk", models.IntegerField(verbose_name="旧员工主键")),
                (
                    "mapping_status",
                    models.CharField(
                        choices=[
                            ("UNREVIEWED", "待核验"),
                            ("MATCH_CANDIDATE", "候选匹配"),
                            ("LINKED", "已链接"),
                            ("CONFLICT", "冲突待人工"),
                            ("IGNORED", "忽略"),
                        ],
                        default="UNREVIEWED",
                        max_length=20,
                        verbose_name="映射状态",
                    ),
                ),
                (
                    "match_method",
                    models.CharField(
                        blank=True,
                        help_text="人工确认 / 工号映射 / 业务键验证",
                        max_length=32,
                        verbose_name="匹配方式",
                    ),
                ),
                (
                    "reviewed_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="核验时间"),
                ),
                ("note", models.CharField(blank=True, max_length=240, verbose_name="核验备注")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                (
                    "legacy_staff",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pms_staff_mapping",
                        to="hotelapp.staff",
                        verbose_name="旧员工记录",
                    ),
                ),
                (
                    "property",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_staff_mappings",
                        to="core.property",
                        verbose_name="物业",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_staff_mappings_reviewed",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="核验人",
                    ),
                ),
                (
                    "staff_profile",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_staff_mapping",
                        to="identity.staffprofile",
                        verbose_name="新员工档案",
                    ),
                ),
            ],
            options={
                "verbose_name": "旧员工映射台账",
                "verbose_name_plural": "旧员工映射台账",
                "ordering": ("id",),
                # 约束与索引内联（不单独 AddConstraint / AddIndex）：MySQL 反向迁移
                # DROP TABLE 即可，避免 1091 / 1553（见文件头说明）。
                "constraints": [
                    models.UniqueConstraint(
                        fields=("property", "legacy_pk"),
                        name="uq_legacy_staff_mapping_legacy_pk",
                    ),
                ],
                "indexes": [
                    models.Index(fields=["property", "mapping_status"], name="idx_lsm_prop_status"),
                ],
            },
        ),
        # RunPython 置于操作列表末尾：正向先建表再回填，反向按操作倒序撤销
        # （先清回填数据，再删表）。
        migrations.RunPython(_backfill, _unbackfill),
    ]
