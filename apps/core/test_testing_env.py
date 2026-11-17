"""证据生成器的测试子进程环境净化测试（防止 .env 平台白名单污染回归结论）。"""

from django.test import SimpleTestCase

from apps.core.testing_env import STRIP_KEYS, test_process_env


class TestProcessEnvTests(SimpleTestCase):
    def test_strips_platform_admin_whitelist_keys(self):
        env = test_process_env(
            {
                "DJANGO_DB_USER": "ci_user",
                "DJANGO_PLATFORM_ADMIN_USERNAME": "admin",
                "DJANGO_PLATFORM_ADMIN_PASSWORD": "admin",
            }
        )
        for key in STRIP_KEYS:
            self.assertNotIn(key, env)
        self.assertEqual(env["DJANGO_DB_USER"], "ci_user", "其余环境变量须照常透传")

    def test_forces_test_django_env(self):
        env = test_process_env({"DJANGO_ENV": "dev", "DJANGO_DB_ENGINE": "mysql"})
        self.assertEqual(env["DJANGO_ENV"], "test")
        self.assertEqual(env["DJANGO_DB_ENGINE"], "mysql")

    def test_does_not_mutate_source_mapping(self):
        source = {
            "DJANGO_ENV": "dev",
            "DJANGO_PLATFORM_ADMIN_USERNAME": "admin",
        }
        test_process_env(source)
        self.assertEqual(source["DJANGO_ENV"], "dev")
        self.assertEqual(source["DJANGO_PLATFORM_ADMIN_USERNAME"], "admin")

    def test_whitelist_keys_are_the_documented_pair(self):
        """白名单键集合冻结：扩容须同步 settings 与本项目记忆口径。"""
        self.assertEqual(
            set(STRIP_KEYS),
            {"DJANGO_PLATFORM_ADMIN_USERNAME", "DJANGO_PLATFORM_ADMIN_PASSWORD"},
        )
