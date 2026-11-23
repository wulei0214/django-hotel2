"""ARCH-07 P0 / Q7 生产安全配置负向测试（任务书 7.6 强制负向测试补齐）。

对 `hotel.settings._validate_production_settings` 的四个校验维度逐一做
负向验证，并断言错误信息只指出变量名/原因、不回显变量值（任务书 7.5）。
另覆盖 PII 密文被篡改/密钥错误时的确定性 ValueError（Q1 决策要求）。

本文件为纯函数级测试，不依赖数据库，不改变任何生产逻辑；仅补齐既有
Q7 决策已实现校验的测试缺口（DEBUG / SECRET_KEY / ALLOWED_HOSTS 维度
此前无负向测试，见 PMS2-ARCH-GOV-001 审计报告 §7.4）。
"""

import importlib
import os
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.utils.crypto import get_random_string

import hotel.settings as project_settings
from hotel.settings import _validate_production_settings

PRODUCTION_TEST_SECRET = get_random_string(48)
PRODUCTION_TEST_PII_KEY = get_random_string(48)


class DjangoEnvironmentTests(SimpleTestCase):
    """非规范环境名必须拒绝启动，不能落到开发默认值。"""

    def setUp(self):
        self.settings_snapshot = {
            key: value for key, value in vars(project_settings).items() if key.isupper()
        }

    def tearDown(self):
        for key, value in self.settings_snapshot.items():
            setattr(project_settings, key, value)
        self.settings_snapshot.clear()

    def _import_settings(self, django_env):
        env = {
            "DJANGO_ENV": django_env,
            "DJANGO_DEBUG": "1",
            "DJANGO_SECRET_KEY": PRODUCTION_TEST_SECRET,
            "DJANGO_PII_KEY": PRODUCTION_TEST_PII_KEY,
            "PAYMENT_WEBHOOK_SECRET": PRODUCTION_TEST_SECRET,
            "DJANGO_ALLOWED_HOSTS": "pms.example.com",
            "DJANGO_CACHE_BACKEND": "locmem",
            "DJANGO_DB_ENGINE": "sqlite",
            "DJANGO_DB_NAME": ":memory:",
            "DJANGO_PLATFORM_ADMIN_USERNAME": "",
            "DJANGO_PLATFORM_ADMIN_PASSWORD": "",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                importlib.reload(project_settings)
            self.assertIn("DJANGO_ENV", str(ctx.exception))
            self.assertNotIn(django_env, str(ctx.exception))

    def test_production_spelling_fails_closed(self):
        self._import_settings("production")

    def test_prd_spelling_fails_closed(self):
        self._import_settings("prd")

    def test_unknown_spelling_fails_closed(self):
        self._import_settings("unexpected-invalid-environment")


class ProductionConfigFailFastTests(SimpleTestCase):
    """Q7 四维度 fail-fast 负向测试。"""

    OK = {
        "debug": False,
        "secret_key": "s" * 40,
        "pii_key": "p" * 40,
        "allowed_hosts_raw": "pms.example.com",
        "secure_ssl_redirect": True,
        "hsts_seconds": 31536000,
        "session_cookie_secure": True,
        "csrf_cookie_secure": True,
        "db_engine": "mysql",
        "is_production": True,
    }

    def test_https_and_hsts_are_required(self):
        for field in ("secure_ssl_redirect", "hsts_seconds"):
            with self.subTest(field=field), self.assertRaises(ImproperlyConfigured):
                _validate_production_settings(**{**self.OK, field: False})

    def test_secure_cookies_are_required(self):
        for field in ("session_cookie_secure", "csrf_cookie_secure"):
            with self.subTest(field=field), self.assertRaises(ImproperlyConfigured):
                _validate_production_settings(**{**self.OK, field: False})

    def test_mysql_is_required(self):
        with self.assertRaises(ImproperlyConfigured):
            _validate_production_settings(**{**self.OK, "db_engine": "sqlite"})

    def test_mock_payments_are_rejected_in_production(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _validate_production_settings(**{**self.OK, "debug": True})
        self.assertIn("Mock", str(ctx.exception))

    def test_nonproduction_mock_state_is_rejected(self):
        for field in ("is_production", "debug"):
            with self.subTest(field=field), self.assertRaises(ImproperlyConfigured):
                _validate_production_settings(**{**self.OK, field: not self.OK[field]})

    def test_all_valid_config_passes(self):
        """正向基线：全部合规时校验通过（证明负向断言非假阴性）。"""
        _validate_production_settings(**self.OK)

    def test_debug_true_is_rejected(self):
        """DEBUG 意外开启 → 拒绝启动。"""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _validate_production_settings(**{**self.OK, "debug": True})
        self.assertIn("DEBUG", str(ctx.exception))

    def test_missing_secret_key_is_rejected(self):
        """缺少生产 SECRET_KEY → 拒绝启动。"""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _validate_production_settings(**{**self.OK, "secret_key": ""})
        self.assertIn("DJANGO_SECRET_KEY", str(ctx.exception))

    def test_short_secret_key_is_rejected(self):
        """SECRET_KEY 长度不足 32 → 拒绝启动。"""
        with self.assertRaises(ImproperlyConfigured):
            _validate_production_settings(**{**self.OK, "secret_key": "short"})

    def test_insecure_default_secret_key_is_rejected(self):
        """含 django-insecure 的默认密钥 → 拒绝启动。"""
        with self.assertRaises(ImproperlyConfigured):
            _validate_production_settings(
                **{**self.OK, "secret_key": "django-insecure-" + "s" * 40}
            )

    def test_wildcard_allowed_hosts_is_rejected(self):
        """ALLOWED_HOSTS 含通配符 → 拒绝启动。"""
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _validate_production_settings(**{**self.OK, "allowed_hosts_raw": "*,pms.example.com"})
        self.assertIn("DJANGO_ALLOWED_HOSTS", str(ctx.exception))

    def test_empty_allowed_hosts_is_rejected(self):
        """ALLOWED_HOSTS 为空 → 拒绝启动。"""
        with self.assertRaises(ImproperlyConfigured):
            _validate_production_settings(**{**self.OK, "allowed_hosts_raw": ""})

    def test_error_message_never_echoes_secret_value(self):
        """配置错误信息只指出变量名与原因，不得回显变量值（任务书 7.5）。"""
        sentinel = "TOPSECRET_SENTINEL_DO_NOT_ECHO_12345"
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _validate_production_settings(**{**self.OK, "secret_key": sentinel, "debug": True})
        self.assertNotIn(sentinel, str(ctx.exception))


class PiiCiphertextTamperTests(SimpleTestCase):
    """Q1：错误密钥 / 篡改密文必须确定性失败，不得静默返回错值。"""

    def test_tampered_ciphertext_raises_value_error(self):
        """密文被篡改 → decrypt 抛 ValueError（不返回伪造明文）。"""
        from apps.guests.services import decrypt_phone, encrypt_phone

        cipher = encrypt_phone("13800009999")
        # 翻转密文尾部一个字符，制造篡改
        tampered = cipher[:-2] + ("A" if cipher[-2] != "A" else "B") + cipher[-1:]
        with self.assertRaises(ValueError):
            decrypt_phone(tampered)

    def test_wrong_pii_key_raises_value_error(self):
        """用错误 PII 密钥解密 → 抛 ValueError（密钥隔离/错误密钥防护）。"""
        from apps.guests.services import decrypt_phone, encrypt_phone

        with override_settings(DJANGO_PII_KEY="k" * 40):
            cipher = encrypt_phone("13800008888")
        with override_settings(DJANGO_PII_KEY="x" * 40), self.assertRaises(ValueError):
            decrypt_phone(cipher)
