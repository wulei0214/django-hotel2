"""TASK-02：手机号规范化统一口径回归测试。

背景：客人档案侧（`apps.guests.services.normalize_phone`）曾保留 `+`，
预订侧（`apps.reservations.services.normalize_phone`）只保留数字。同一号码
在两条链路上得到不同 `phone_hash`，导致共享号码判定、PENDING 上限与
`PendingBookingGate.gate_key` 三处口径漂移。

本文件锁定的契约（改动需评审）：
  * 规范形态 = **纯数字**（`+` 只是可接受的输入分隔符，不进入存储形态）；
  * 有效性 = 数字位数 6–20，超出抛 `ValueError("请输入有效联系电话。")`；
  * 预订/入住必填（`require_phone`），客人档案允许为空；
  * **历史数据不改写**：迁移 0007 口径与存量 `+数字` 哈希保持原样，
    读侧靠 `phone_hash_candidates()` 兼容两种形态；
  * 限流维度先规范化再指纹化，防止同一号码换写法拿到多份配额。
"""

import importlib

from django.test import RequestFactory, TestCase

from apps.core import rate_limit
from apps.core.models import Property, RateLimitCounter
from apps.core.phone import (
    PHONE_MAX_DIGITS,
    PHONE_MIN_DIGITS,
    digit_count,
    is_valid_phone,
    legacy_phone_variants,
    normalize_phone,
    require_phone,
    validate_phone,
)
from apps.core.rate_limit import fingerprint
from apps.guests.models import Guest, GuestPhone
from apps.guests.services import (
    GuestService,
    GuestServiceError,
    decrypt_phone,
    phone_hash,
    phone_hash_candidates,
)
from apps.guests.services import normalize_phone as guest_normalize_phone
from apps.reservations.services import normalize_phone as reservation_normalize_phone

_MIGRATION_0007 = importlib.import_module(
    "apps.guests.migrations.0007_phone_pii_ciphertext_and_document_index"
)

#: 带国家码 + 各种分隔符的同一号码（人类常见输入形态）
MESSY_INTERNATIONAL = "+86 (138) 0000-1111"
INTERNATIONAL_DIGITS = "8613800001111"


class NormalizeContractTests(TestCase):
    """第 1–4 类：格式规范化只做"去非数字"，不做长度判断。"""

    def test_plain_local_number_is_unchanged(self):
        self.assertEqual(normalize_phone("13800001111"), "13800001111")

    def test_country_code_is_kept_as_digits(self):
        # 国际号码保留国家码数字，不把 86 当成"多余前缀"删掉：
        # 删前缀会让 +86 138... 与 138... 变成同一号码，属业务误判。
        self.assertEqual(normalize_phone("+86 13800001111"), "8613800001111")
        self.assertEqual(normalize_phone("0086 13800001111"), "008613800001111")

    def test_plus_sign_is_treated_as_separator(self):
        self.assertEqual(normalize_phone("+13800001111"), "13800001111")
        self.assertNotIn("+", normalize_phone("+13800001111"))

    def test_separators_are_stripped(self):
        for raw in (
            "138 0000 1111",
            "138-0000-1111",
            "(138) 0000 1111",
            "+86 (138) 0000-1111",
            "138.0000.1111",
            " 13800001111 ",
        ):
            with self.subTest(raw=raw):
                self.assertRegex(normalize_phone(raw), r"^\d+$")


class ValidityTests(TestCase):
    """第 5–8 类：位数边界、非数字、空值。"""

    def test_too_short_is_invalid(self):
        self.assertFalse(is_valid_phone("12345"))
        self.assertEqual(digit_count("12345"), 5)
        with self.assertRaisesMessage(ValueError, "请输入有效联系电话。"):
            validate_phone("12345")
        with self.assertRaises(ValueError):
            reservation_normalize_phone("12345")

    def test_too_long_is_invalid(self):
        too_long = "1" * (PHONE_MAX_DIGITS + 1)
        self.assertFalse(is_valid_phone(too_long))
        with self.assertRaises(ValueError):
            validate_phone(too_long)

    def test_boundary_lengths_are_valid(self):
        self.assertTrue(is_valid_phone("1" * PHONE_MIN_DIGITS))
        self.assertTrue(is_valid_phone("1" * PHONE_MAX_DIGITS))

    def test_non_digit_characters_are_dropped(self):
        # 号码里混入字母：字母不是数字，规范化时丢弃（不报错、不保留）
        self.assertEqual(normalize_phone("138abc00001111"), "13800001111")
        self.assertEqual(normalize_phone("13800001111转8001"), "138000011118001")
        # 纯非数字 → 空串（由调用方按场景决定"必填"还是"允许为空"）
        self.assertEqual(normalize_phone("abc-def"), "")

    def test_empty_values_normalize_to_empty(self):
        for raw in (None, "", "   ", "()", "-"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), "")
                self.assertFalse(is_valid_phone(raw))
                self.assertEqual(phone_hash_candidates(raw), ())

    def test_require_phone_rejects_blank_but_accepts_valid(self):
        with self.assertRaises(ValueError):
            require_phone("")
        with self.assertRaises(ValueError):
            require_phone("138")  # 有值但位数不足，同样拒绝
        self.assertEqual(require_phone(StringLike("+86 138-0000-1111")), INTERNATIONAL_DIGITS)


class StringLike:
    """非 str 输入（模拟前端传数字/对象）——验证规范化对任意输入稳健。"""

    def __init__(self, value):
        self._value = value

    def __str__(self):
        return self._value


class CrossModuleConsistencyTests(TestCase):
    """核心回归：两条链路必须算出同一个 `phone_hash`。"""

    def setUp(self):
        self.property = Property.objects.create(code="PHN1", name="号码口径测试酒店")

    def test_both_paths_normalize_identically(self):
        for raw in (
            "13800001111",
            "+13800001111",
            MESSY_INTERNATIONAL,
            "138 0000 1111",
            "138-0000-1111",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(guest_normalize_phone(raw), reservation_normalize_phone(raw))

    def test_both_paths_produce_same_hash(self):
        raw = MESSY_INTERNATIONAL
        self.assertEqual(
            phone_hash(guest_normalize_phone(raw)),
            phone_hash(reservation_normalize_phone(raw)),
        )

    def test_guest_created_with_plus_is_hashable_by_plain_digits(self):
        """从档案侧带 `+` 建档，预订侧按纯数字必须能命中同一哈希。

        这是修复前必然失败的用例：旧实现在档案侧存 HMAC("+86...")，
        预订侧按 HMAC("86...") 查 PENDING 上限与共享判定 → 永久漏判。
        """
        guest = GuestService.create_guest(
            property_obj=self.property,
            full_name="带加号建档客人",
            phone=MESSY_INTERNATIONAL,
        )
        self.assertEqual(guest.phone_hash, phone_hash(INTERNATIONAL_DIGITS))
        self.assertEqual(
            Guest.objects.filter(phone_hash=phone_hash(INTERNATIONAL_DIGITS)).count(), 1
        )
        # 密文存的也是纯数字形态（解密后不含分隔符/加号）
        self.assertEqual(decrypt_phone(guest.phone_ciphertext), INTERNATIONAL_DIGITS)

    def test_rate_limit_dimension_is_normalized(self):
        """限流维度：同一号码换写法必须落进同一个桶。"""
        self.assertEqual(
            fingerprint(normalize_phone(MESSY_INTERNATIONAL)),
            fingerprint(normalize_phone(INTERNATIONAL_DIGITS)),
        )
        self.assertEqual(
            fingerprint(normalize_phone("+13800001111")),
            fingerprint(normalize_phone("13800001111")),
        )
        # 空号码不得形成共享桶（P0-02 保留）
        self.assertEqual(fingerprint(normalize_phone("")), "")


class RateLimitBucketTests(TestCase):
    """限流桶实测：`rate_limit.check` 内部完成规范化（不依赖调用方）。"""

    def test_phone_dimension_shares_bucket_across_notations(self):
        factory = RequestFactory()
        request = factory.post("/api/v1/reservations", REMOTE_ADDR="10.20.30.40")
        action = "public.reservation_create"
        # 两次不同写法的同一号码：IP 维度共用 1 行；phone 维度若同桶也只 1 行
        self.assertTrue(rate_limit.check(request, action, phone="+86 138-0000-1111"))
        self.assertTrue(rate_limit.check(request, action, phone="8613800001111"))
        self.assertEqual(
            RateLimitCounter.objects.filter(action=action).count(),
            2,
            "同一号码的不同写法必须落进同一个 phone 桶（否则可成倍放大配额）",
        )

    def test_blank_phone_does_not_create_shared_bucket(self):
        factory = RequestFactory()
        request = factory.post("/api/v1/reservations", REMOTE_ADDR="10.20.30.41")
        action = "public.reservation_create"
        self.assertTrue(rate_limit.check(request, action, phone=""))
        # 空号码只产生 IP 维度 1 行，不产生空串共享桶（P0-02）
        self.assertEqual(RateLimitCounter.objects.filter(action=action).count(), 1)


class SharedAndDuplicatePhoneTests(TestCase):
    """第 9–10 类：共享号码标记与同档案重复号码拒绝。"""

    def setUp(self):
        self.property = Property.objects.create(code="PHN2", name="共享号码测试酒店")

    def test_shared_phone_marks_review_without_merging(self):
        # 同一号码的不同写法：带国家码 + 分隔符  vs  纯数字
        first = GuestService.create_guest(
            property_obj=self.property, full_name="共享甲", phone="+86 138-0000-2222"
        )
        second = GuestService.create_guest(
            property_obj=self.property, full_name="共享乙", phone="8613800002222"
        )
        # 两个档案都存在（不自动合并），且哈希一致（同一号码）
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.phone_hash, second.phone_hash)
        # 两行电话都被标记为待人工核验
        rows = GuestPhone.objects.filter(phone_hash=phone_hash("8613800002222"))
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(row.shared_review_required for row in rows))

    def test_country_code_is_not_stripped_deliberately(self):
        """不带国家码与带国家码视为不同号码（不做前缀剥离）。

        刻意保留的边界：剥离 `86` 可能把 `8613...` 与 `13...` 误并成同一人，
        属于不可自动化的业务判断。此处锁定"不剥离"，作为已知限制留档。
        """
        with_code = GuestService.create_guest(
            property_obj=self.property, full_name="带国家码", phone="+86 138 0000 2999"
        )
        without_code = GuestService.create_guest(
            property_obj=self.property, full_name="不带国家码", phone="138 0000 2999"
        )
        self.assertNotEqual(with_code.phone_hash, without_code.phone_hash)
        self.assertEqual(
            GuestPhone.objects.filter(phone_hash=phone_hash("8613800002999")).count(), 1
        )

    def test_same_guest_duplicate_phone_is_rejected(self):
        guest = GuestService.create_guest(
            property_obj=self.property, full_name="重复号码客人", phone="13800003333"
        )
        GuestService.add_phone(guest=guest, phone="13800003344", is_primary=False)
        # 再加同号（换了写法）→ 唯一约束冲突转业务错误
        with self.assertRaises(GuestServiceError):
            GuestService.add_phone(guest=guest, phone="+13800003344", is_primary=False)

    def test_update_guest_phone_with_plus_keeps_single_form(self):
        guest = GuestService.create_guest(
            property_obj=self.property, full_name="改号客人", phone="13800004444"
        )
        GuestService.update_guest(
            guest_id=guest.pk, property_obj=self.property, phone=MESSY_INTERNATIONAL
        )
        guest.refresh_from_db()
        self.assertEqual(guest.phone_hash, phone_hash(INTERNATIONAL_DIGITS))
        self.assertEqual(
            GuestPhone.objects.filter(
                guest=guest, phone_hash=phone_hash(INTERNATIONAL_DIGITS)
            ).count(),
            1,
        )


class LegacyCompatibilityTests(TestCase):
    """历史数据不改写：读侧必须能命中 `+数字` 形态的存量哈希。"""

    def setUp(self):
        self.property = Property.objects.create(code="PHN3", name="历史号码测试酒店")

    def test_migration_0007_keeps_legacy_normalization(self):
        """迁移脚本属历史资产：口径冻结，不得跟随服务层一起改。

        若有人"顺手统一"迁移里的 `_normalize_phone`，已上线库重跑迁移会
        改写存量哈希，属不可逆数据变更。
        """
        self.assertEqual(_MIGRATION_0007._normalize_phone("+86 138-0000-1111"), "+8613800001111")

    def test_candidates_cover_legacy_plus_form(self):
        legacy_guest = Guest.objects.create(
            property=self.property,
            full_name="历史带加号档案",
            phone_hash=phone_hash("+13800005555"),
        )
        candidates = phone_hash_candidates("13800005555")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0], phone_hash("13800005555"))
        self.assertEqual(candidates[1], phone_hash("+13800005555"))
        self.assertEqual(
            Guest.objects.filter(phone_hash__in=candidates, pk=legacy_guest.pk).count(), 1
        )

    def test_candidates_are_stable_and_deduplicated(self):
        # 同一号码的不同写法（含 `+` 前缀、空格分隔）产生同一组候选，
        # 顺序固定 → 可直接作为 SQL 过滤参数，SQLite/MySQL 行为一致。
        self.assertEqual(
            phone_hash_candidates("13800006666"), phone_hash_candidates("+13800006666")
        )
        self.assertEqual(
            phone_hash_candidates("13800006666"), phone_hash_candidates(" 138 0000 6666 ")
        )
        self.assertEqual(len(set(phone_hash_candidates("13800006666"))), 2)

    def test_legacy_variants_helper(self):
        self.assertEqual(legacy_phone_variants("13800007777"), ("13800007777", "+13800007777"))
        self.assertEqual(legacy_phone_variants(""), ())
