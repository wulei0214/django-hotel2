"""Django settings for hotel project.
Environment-driven configuration for the legacy UI and PMS 2.0 foundation.
"""
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DJANGO_ENV = os.environ.get("DJANGO_ENV", "dev").strip().lower()
DEBUG = env_bool("DJANGO_DEBUG", default=(DJANGO_ENV != "prod"))
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("生产环境必须设置 DJANGO_SECRET_KEY。")
    SECRET_KEY = get_random_secret_key()

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "simpleui",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.core.apps.CoreConfig",
    "apps.identity.apps.IdentityConfig",
    "apps.guests.apps.GuestsConfig",
    "apps.inventory.apps.InventoryConfig",
    "apps.reservations.apps.ReservationsConfig",
    "apps.billing.apps.BillingConfig",
    "apps.api.apps.ApiConfig",
    "hotelapp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.identity.middleware.PropertyContextMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "hotelapp.audit_middleware.AuditMiddleware",
]

ROOT_URLCONF = "hotel.urls"
LOGIN_URL = "/login/"
# API 路径的 CSRF 失败返回 JSON（保持接口错误契约），页面路径仍走默认 HTML
CSRF_FAILURE_VIEW = "apps.api.views.csrf_failure"
DEFAULT_PROPERTY_CODE = os.environ.get("DEFAULT_PROPERTY_CODE", "DEFAULT")
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
if not PAYMENT_WEBHOOK_SECRET:
    if not DEBUG:
        raise ImproperlyConfigured("生产环境必须设置 PAYMENT_WEBHOOK_SECRET。")
    PAYMENT_WEBHOOK_SECRET = get_random_secret_key()

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [str(BASE_DIR / "templates")],
        "APP_DIRS": False,
        "OPTIONS": {
            "loaders": [
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ],
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.identity.context_processors.pms_context",
            ],
        },
    },
]

WSGI_APPLICATION = "hotel.wsgi.application"

DB_ENGINE = os.environ.get("DJANGO_DB_ENGINE", "mysql").strip().lower()
DB_ENGINES = {
    "mysql": "django.db.backends.mysql",
    "postgresql": "django.db.backends.postgresql",
    "postgres": "django.db.backends.postgresql",
    "sqlite": "django.db.backends.sqlite3",
}
if DB_ENGINE not in DB_ENGINES:
    raise ImproperlyConfigured("DJANGO_DB_ENGINE 仅支持 mysql、postgresql 或 sqlite。")

if DB_ENGINE == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINES[DB_ENGINE],
            "NAME": os.environ.get("DJANGO_DB_NAME", ":memory:"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": DB_ENGINES[DB_ENGINE],
            "NAME": os.environ.get("DJANGO_DB_NAME", "hotel"),
            "USER": os.environ.get("DJANGO_DB_USER", ""),
            "PASSWORD": os.environ.get("DJANGO_DB_PASSWORD", ""),
            "HOST": os.environ.get("DJANGO_DB_HOST", "localhost"),
            "PORT": os.environ.get("DJANGO_DB_PORT", "3306" if DB_ENGINE == "mysql" else "5432"),
            "CONN_MAX_AGE": int(os.environ.get("DJANGO_DB_CONN_MAX_AGE", "60")),
        }
    }
    if DB_ENGINE == "mysql":
        DATABASES["default"]["OPTIONS"] = {"charset": "utf8mb4"}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_L10N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = str(BASE_DIR / "staticfiles")
STATICFILES_DIRS = [str(BASE_DIR / "static")]

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
DATA_UPLOAD_MAX_NUMBER_FIELDS = 2000
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=False)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "0"))
if env_bool("DJANGO_TRUST_PROXY_SSL", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "hotel-cache",
    }
}

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "std": {"format": "{asctime} [{levelname}] {message}", "style": "{"},
    },
    "handlers": {
        "audit_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "audit.log"),
            "maxBytes": 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "std",
        },
        "request_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "request.log"),
            "maxBytes": 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "formatter": "std",
        },
    },
    "loggers": {
        "hotel.audit": {"handlers": ["audit_file"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["request_file"], "level": "WARNING", "propagate": False},
    },
}

SIMPLEUI_DEFAULT_THEME = "admin.lte.css"
SIMPLEUI_HOME_INFO = False   # 隐藏 simpleui 自带的英文信息卡（Gitee/Github 面板）
SIMPLEUI_HOME_TITLE = "酒店管理控制台"
SIMPLEUI_LOGO = "/static/images/t1.jpg"

# 多物业策略（RBAC 决策 8）：单店运行，仅 DEFAULT 物业生效；
# PMS2 架构（物业/库存/预订/账务等 app）代码保留但菜单隐藏——URL 仍可直达供开发期排查，
# 后续多物业启用时删除本配置即恢复完整菜单。
SIMPLEUI_CONFIG = {
    "system_name": "酒店管理系统",
    "menus": [
        {
            "name": "酒店业务模块",
            "icon": "fas fa-hotel",
            "models": [
                {"name": "员工信息管理", "icon": "far fa-user", "url": "/admin/hotelapp/staff/"},
                {"name": "顾客管理", "icon": "far fa-address-book", "url": "/admin/hotelapp/client/"},
                {"name": "会员信息管理", "icon": "far fa-star", "url": "/admin/hotelapp/vip/"},
                {"name": "客房信息管理", "icon": "fas fa-bed", "url": "/admin/hotelapp/room/"},
                {"name": "客房订单管理", "icon": "fas fa-book", "url": "/admin/hotelapp/accommodation/"},
                {"name": "财务流水", "icon": "fas fa-yen-sign", "url": "/admin/hotelapp/financerecord/"},
                {"name": "夜审记录", "icon": "far fa-bookmark", "url": "/admin/hotelapp/nightaudit/"},
                {"name": "账单流水", "icon": "fas fa-shopping-bag", "url": "/admin/hotelapp/folio/"},
            ],
        },
        {
            "name": "PMS 身份与权限",
            "icon": "fas fa-id-badge",
            "models": [
                {"name": "员工档案", "icon": "far fa-id-card", "url": "/admin/identity/staffprofile/"},
                {"name": "员工酒店任职关系", "icon": "fas fa-briefcase", "url": "/admin/identity/propertystaffmembership/"},
                {"name": "员工岗位授权", "icon": "fas fa-key", "url": "/admin/identity/staffroleassignment/"},
            ],
        },
        {
            "name": "认证和授权",
            "icon": "fas fa-shield-alt",
            "models": [
                {"name": "用户", "icon": "far fa-user", "url": "/admin/auth/user/"},
                {"name": "组", "icon": "fas fa-users-cog", "url": "/admin/auth/group/"},
            ],
        },
    ],
}
