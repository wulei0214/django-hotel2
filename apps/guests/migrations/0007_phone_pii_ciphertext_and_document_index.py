# -*- coding: utf-8 -*-
"""WB-102/103：手机号密文+HMAC 全面迁移 与 证件物业级占用索引。

数据迁移策略（WB-103，PII 基线 11.3-E：不留明文 normalized 旁路）：
  1. 加列 phone_ciphertext（Fernet）/ phone_hash（HMAC-SHA256）+ 索引与
     (guest, phone_hash) 唯一约束；建 GuestDocumentIndex（WB-102）；
  2. RunPython 回填：从明文列（phone/phone_normalized）计算密文与哈希，
     任一行失败 → RuntimeError 显式失败（事务回滚，该行保留原值）；
  3. 明文列放宽为可空；
  4. RunPython 清空明文列——迁移后 phone/phone_normalized 为空，
     列仅保留兼容历史 DDL，业务读写一律走密文/哈希列。

密钥与算法必须与 apps/guests/services.py 完全一致；此处内联实现，
避免数据迁移依赖业务模块的真实模型注册表。
"""
import base64
import hashlib
import hmac
import re

from django.conf import settings
from cryptography.fernet import Fernet
from django.db import migrations, models


def _pii_key_bytes():
    # 与 apps.guests.services._pii_key_bytes 同口径（WB-123）：
    # 优先 settings.DJANGO_PII_KEY（生产由 Q7 启动校验保证已配置）；
    # 生产缺失时 fail-fast 禁止回退 SECRET_KEY；开发/测试允许回退。
    key = getattr(settings, "DJANGO_PII_KEY", "")
    if not key:
        if getattr(settings, "IS_PRODUCTION", False):
            raise RuntimeError(
                "生产环境必须配置 DJANGO_PII_KEY（WB-123），"
                "禁止回退 SECRET_KEY 执行 PII 数据迁移。")
        key = settings.SECRET_KEY
    return hashlib.sha256(str(key).encode("utf-8")).digest()


def _normalize_phone(value):
    # 与 apps.guests.services.normalize_phone 同口径：保留数字与 +。
    return re.sub(r"[^\d+]", "", (value or "").strip())


def _phone_hash(normalized):
    return hmac.new(_pii_key_bytes(), (normalized or "").strip().encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _encrypt_phone(normalized):
    fernet = Fernet(base64.urlsafe_b64encode(_pii_key_bytes()))
    return fernet.encrypt((normalized or "").strip().encode("utf-8")).decode("ascii")


def _rows_with_phone_plaintext(model):
    """WB-122（第 31 轮）：统一「存在非空手机号明文」的判定条件。

    空字符串与 NULL 一律视为无号码——此前 exclude(isnull=True) 会把
    phone=""/phone_normalized="" 的空串记录也查出来：回填对它们跳过
    （标准化后无号码），清明文守卫又用同一条件把它们当作"待回填记录"
    检查密文，最终因密文缺失误判迁移失败。回填、守卫、清理三方必须
    采用同一筛选口径，Guest 与 GuestPhone 完全一致。
    """
    return model.objects.filter(
        models.Q(phone__isnull=False) & ~models.Q(phone="")
        | models.Q(phone_normalized__isnull=False)
        & ~models.Q(phone_normalized=""))


def _iter_guests_with_phone(Guest):
    """WB-122：覆盖 phone 或 phone_normalized 任一字段非空的记录。

    历史数据存在 phone 为空但 phone_normalized 有值的 Guest——只扫 phone
    会漏回填这类行，随后的清明文步骤却会清掉它们唯一的号码，造成不可逆
    数据丢失。标准化优先取 phone_normalized，为空时再从 phone 标准化；
    空字符串与 NULL 均按无号码处理（见 _rows_with_phone_plaintext）。
    """
    return _rows_with_phone_plaintext(Guest).iterator()


def backfill_phone_pii(apps, schema_editor):
    """明文行回填 ciphertext+hash；失败该行保留原值并让迁移显式失败。"""
    Guest = apps.get_model("guests", "Guest")
    GuestPhone = apps.get_model("guests", "GuestPhone")

    failures = []
    for guest in _iter_guests_with_phone(Guest):
        raw_phone = (guest.phone or "").strip()
        normalized = ((guest.phone_normalized or "").strip()
                      or _normalize_phone(raw_phone))
        if not normalized:
            # 两个字段均为空（仅理论可达）：按无电话记录处理，不伪造号码
            continue
        try:
            Guest.objects.filter(pk=guest.pk).update(
                phone_ciphertext=_encrypt_phone(normalized),
                phone_hash=_phone_hash(normalized))
        except Exception as exc:  # noqa: BLE001 —— 显式失败收集器
            failures.append(f"Guest#{guest.pk}: {exc}")

    occupied = {}
    for row in _rows_with_phone_plaintext(GuestPhone).iterator():
        raw_phone = (row.phone or "").strip()
        normalized = ((row.phone_normalized or "").strip()
                      or _normalize_phone(raw_phone))
        if not normalized:
            # 有原始值但无法标准化：数据异常，显式失败（不清明文，不丢数据）
            raise ValueError(
                f"GuestPhone#{row.pk} 原始值无法标准化为有效号码，"
                "需人工核对后再执行迁移")
        try:
            digest = _phone_hash(normalized)
            key = (row.guest_id, digest)
            if key in occupied:
                raise ValueError(
                    f"同一客人存在重复号码行（与 GuestPhone#{occupied[key]} "
                    "哈希冲突，违反 (guest, phone_hash) 唯一约束），需人工合并")
            occupied[key] = row.pk
            GuestPhone.objects.filter(pk=row.pk).update(
                phone_ciphertext=_encrypt_phone(normalized),
                phone_hash=digest)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"GuestPhone#{row.pk}: {exc}")

    if failures:
        raise RuntimeError(
            "WB-103 手机号 PII 回填失败（该批行保留原值，迁移显式终止）："
            + "; ".join(failures[:20]))


def clear_phone_plaintext(apps, schema_editor):
    """清空明文列（不可逆步骤，WB-124）。

    清空前守卫（WB-122/124）：任何「明文非空但密文/哈希缺失」的记录都会
    让本步骤显式失败——回填必须先完整成功，才允许清除明文；反向迁移不
    提供明文恢复（迁移声明为不可逆），部署前必须备份并验证可恢复。
    """
    Guest = apps.get_model("guests", "Guest")
    GuestPhone = apps.get_model("guests", "GuestPhone")
    missing = []
    for guest in _rows_with_phone_plaintext(Guest).iterator():
        if not ((guest.phone_ciphertext or "") and (guest.phone_hash or "")):
            missing.append(f"Guest#{guest.pk}")
    for row in _rows_with_phone_plaintext(GuestPhone).iterator():
        if not ((row.phone_ciphertext or "") and (row.phone_hash or "")):
            missing.append(f"GuestPhone#{row.pk}")
    if missing:
        raise RuntimeError(
            "WB-124 清明文前守卫：以下记录明文非空但密文/哈希缺失，"
            "禁止清空明文（必须先修复回填）：" + "; ".join(missing[:20]))
    Guest.objects.update(phone=None, phone_normalized=None)
    GuestPhone.objects.update(phone=None, phone_normalized=None)


class Migration(migrations.Migration):
    dependencies = [
        ("guests", "0006_mysql_nullable_unique_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="guest",
            name="phone_ciphertext",
            field=models.TextField(blank=True, help_text="Fernet(phone_ciphertext)=标准化号码；揭示需独立权限并写审计", null=True, verbose_name="手机号密文"),
        ),
        migrations.AddField(
            model_name="guest",
            name="phone_hash",
            field=models.CharField(blank=True, help_text="HMAC-SHA256(PII_KEY, normalized)，仅用于检索与共享判定，不可逆、不代表自然人唯一身份", max_length=64, null=True, verbose_name="手机号查询哈希"),
        ),
        migrations.AddField(
            model_name="guestphone",
            name="phone_ciphertext",
            field=models.TextField(blank=True, help_text="Fernet(标准化号码)；完整值揭示需独立权限并写审计", null=True, verbose_name="号码密文"),
        ),
        migrations.AddField(
            model_name="guestphone",
            name="phone_hash",
            field=models.CharField(blank=True, help_text="HMAC-SHA256(PII_KEY, normalized)，检索与共享号码判定专用", max_length=64, null=True, verbose_name="号码查询哈希"),
        ),
        migrations.AddIndex(
            model_name="guest",
            index=models.Index(fields=["property", "phone_hash"],
                               name="idx_guest_prop_hash"),
        ),
        migrations.AddIndex(
            model_name="guestphone",
            index=models.Index(fields=["phone_hash"], name="idx_guestphone_hash"),
        ),
        migrations.AddConstraint(
            model_name="guestphone",
            constraint=models.UniqueConstraint(
                fields=("guest", "phone_hash"),
                name="uq_guestphone_guest_hash",
            ),
        ),
        migrations.CreateModel(
            name="GuestDocumentIndex",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("doc_no_hash", models.CharField(help_text="HMAC-SHA256，与 GuestDocument.doc_no_hash 同源", max_length=64, verbose_name="证件号哈希")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("guest", models.ForeignKey(on_delete=models.PROTECT, related_name="document_indexes", to="guests.guest", verbose_name="占用客人")),
                ("property", models.ForeignKey(on_delete=models.PROTECT, related_name="guest_document_indexes", to="core.property", verbose_name="所属酒店")),
            ],
            options={
                "verbose_name": "证件物业级占用索引",
                "verbose_name_plural": "证件物业级占用索引",
                "constraints": [
                    models.UniqueConstraint(fields=("property", "doc_no_hash"),
                                            name="uq_guestdocindex_property_hash"),
                ],
            },
        ),
        migrations.RunPython(backfill_phone_pii, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="guest",
            name="phone",
            field=models.CharField(blank=True, help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文；完整号码经 phone_ciphertext 加密存储", max_length=24, null=True, verbose_name="联系电话（历史列）"),
        ),
        migrations.AlterField(
            model_name="guest",
            name="phone_normalized",
            field=models.CharField(blank=True, help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文；检索/去重使用 phone_hash", max_length=24, null=True, verbose_name="标准化手机号（历史列）"),
        ),
        migrations.AlterField(
            model_name="guestphone",
            name="phone",
            field=models.CharField(blank=True, help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文", max_length=24, null=True, verbose_name="原始号码（历史列）"),
        ),
        migrations.AlterField(
            model_name="guestphone",
            name="phone_normalized",
            field=models.CharField(blank=True, help_text="已迁移（WB-103）：留列兼容历史 DDL，新数据不再写入明文；检索使用 phone_hash", max_length=24, null=True, verbose_name="标准化号码（历史列）"),
        ),
        # WB-124：清明文是**不可逆**数据操作——不提供 reverse_code，任何
        # migrate guests 0006 回退尝试都会被 Django 显式拒绝，而不是用
        # noop 伪装成可逆迁移静默成功（回滚方案依赖迁移前备份 + 应用层
        # 兼容读密文，见迁移 docstring）。
        migrations.RunPython(clear_phone_plaintext),
    ]
