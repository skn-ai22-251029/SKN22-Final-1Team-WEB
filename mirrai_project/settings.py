import os
from pathlib import Path

import environ

from mirrai_project.settings_helpers import (
    build_allowed_hosts,
    build_cache_settings,
    cache_uses_redis,
    resolve_active_database_url,
    unique_values,
)


env = environ.Env(
    DEBUG=(bool, False),
    SUPABASE_USE_REMOTE_DB=(bool, False),
    SUPABASE_USE_REMOTE_STORAGE=(bool, False),
    REDIS_USE_FOR_SESSIONS=(bool, True),
    CACHE_DEFAULT_TIMEOUT=(int, 300),
    PARTNER_DASHBOARD_CACHE_SECONDS=(int, 30),
    PARTNER_LIST_CACHE_SECONDS=(int, 60),
    PARTNER_DETAIL_CACHE_SECONDS=(int, 45),
    PARTNER_HISTORY_CACHE_SECONDS=(int, 30),
    PARTNER_LOOKUP_CACHE_SECONDS=(int, 45),
    PARTNER_REPORT_CACHE_SECONDS=(int, 90),
    MIRRAI_PERSIST_CAPTURE_IMAGES=(bool, False),
    MIRRAI_LOCAL_MOCK_RESULTS=(bool, False),
    TREND_REFRESH_ENABLED=(bool, False),
    TREND_REFRESH_INTERVAL_MINUTES=(int, 0),
    TREND_REFRESH_SOURCES=(list, []),
    SUPABASE_BUCKET_PUBLIC=(bool, False),
    SUPABASE_SIGNED_URL_EXPIRES_IN=(int, 3600),
    MIRRAI_WATERMARK_IMAGE=(str, "static/branding/mirrai_wordmark_primary.png"),
    MIRRAI_WATERMARK_OPACITY=(float, 0.15),
    MIRRAI_WATERMARK_ANGLE=(float, -32.0),
    MIRRAI_WATERMARK_WIDTH_RATIO=(float, 0.34),
    MIRRAI_WATERMARK_SPACING_X_RATIO=(float, 0.38),
    MIRRAI_WATERMARK_SPACING_Y_RATIO=(float, 1.2),
    MIRRAI_WATERMARK_STAGGER_RATIO=(float, 0.48),
    SUPABASE_BUCKET_FILE_SIZE_LIMIT=(int, 10 * 1024 * 1024),
)

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    environ.Env.read_env(ENV_PATH, overwrite=False)

SECRET_KEY = env("SECRET_KEY", default="django-insecure-mock-key-for-dev")
DEBUG = env.bool("DEBUG", default=False)

DEFAULT_ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    "mirrai.shop",
    "www.mirrai.shop",
    ".elasticbeanstalk.com",
    ".elb.amazonaws.com",
]
ALLOWED_HOSTS = build_allowed_hosts(
    default_hosts=DEFAULT_ALLOWED_HOSTS,
    env_hosts=unique_values(
        env.list("ALLOWED_HOSTS", default=[]),
        [os.environ.get("EB_ENDPOINT_URL"), os.environ.get("HOSTNAME")],
    ),
)
CSRF_TRUSTED_ORIGINS = [
    "https://mirrai.shop",
    "https://www.mirrai.shop",
    "https://*.elasticbeanstalk.com",
]

DATABASE_URL = env("DATABASE_URL", default="")
SUPABASE_DB_URL = env("SUPABASE_DB_URL", default="")
LOCAL_DATABASE_URL = env("LOCAL_DATABASE_URL", default=DATABASE_URL or "sqlite:///db.sqlite3")
SUPABASE_USE_REMOTE_DB = env.bool("SUPABASE_USE_REMOTE_DB", default=False)
ACTIVE_DATABASE_URL = resolve_active_database_url(
    supabase_use_remote_db=SUPABASE_USE_REMOTE_DB,
    supabase_db_url=SUPABASE_DB_URL,
    local_database_url=LOCAL_DATABASE_URL,
    database_url=DATABASE_URL,
)
DATABASES = {
    "default": {
        **environ.Env.db_url_config(ACTIVE_DATABASE_URL),
        "CONN_MAX_AGE": 60,
    }
}

REDIS_URL = env("REDIS_URL", default="")
REDIS_KEY_PREFIX = env("REDIS_KEY_PREFIX", default="mirrai")
REDIS_USE_FOR_SESSIONS = env.bool("REDIS_USE_FOR_SESSIONS", default=True)
CACHE_DEFAULT_TIMEOUT = env.int("CACHE_DEFAULT_TIMEOUT", default=300)
PARTNER_DASHBOARD_CACHE_SECONDS = env.int("PARTNER_DASHBOARD_CACHE_SECONDS", default=30)
PARTNER_LIST_CACHE_SECONDS = env.int("PARTNER_LIST_CACHE_SECONDS", default=60)
PARTNER_DETAIL_CACHE_SECONDS = env.int("PARTNER_DETAIL_CACHE_SECONDS", default=45)
PARTNER_HISTORY_CACHE_SECONDS = env.int("PARTNER_HISTORY_CACHE_SECONDS", default=30)
PARTNER_LOOKUP_CACHE_SECONDS = env.int("PARTNER_LOOKUP_CACHE_SECONDS", default=45)
PARTNER_REPORT_CACHE_SECONDS = env.int("PARTNER_REPORT_CACHE_SECONDS", default=90)
CACHES = build_cache_settings(
    redis_url=REDIS_URL,
    timeout=CACHE_DEFAULT_TIMEOUT,
    key_prefix=REDIS_KEY_PREFIX,
)

SUPABASE_URL = env("SUPABASE_URL", default="")
SUPABASE_ANON_KEY = env("SUPABASE_ANON_KEY", default="")
SUPABASE_SECRET_KEY = env("SUPABASE_SECRET_KEY", default="")
SUPABASE_SERVICE_ROLE_KEY = env("SUPABASE_SERVICE_ROLE_KEY", default="")
SUPABASE_SERVER_KEY = SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY
SUPABASE_BUCKET = env("SUPABASE_BUCKET", default="mirrai-assets")
SUPABASE_USE_REMOTE_STORAGE = env.bool("SUPABASE_USE_REMOTE_STORAGE", default=False)
SUPABASE_BUCKET_PUBLIC = env.bool("SUPABASE_BUCKET_PUBLIC", default=False)
SUPABASE_SIGNED_URL_EXPIRES_IN = env.int("SUPABASE_SIGNED_URL_EXPIRES_IN", default=3600)
MIRRAI_WATERMARK_IMAGE = env("MIRRAI_WATERMARK_IMAGE", default="static/branding/mirrai_wordmark_primary.png")
MIRRAI_WATERMARK_OPACITY = env.float("MIRRAI_WATERMARK_OPACITY", default=0.15)
MIRRAI_WATERMARK_ANGLE = env.float("MIRRAI_WATERMARK_ANGLE", default=-32.0)
MIRRAI_WATERMARK_WIDTH_RATIO = env.float("MIRRAI_WATERMARK_WIDTH_RATIO", default=0.34)
MIRRAI_WATERMARK_SPACING_X_RATIO = env.float("MIRRAI_WATERMARK_SPACING_X_RATIO", default=0.38)
MIRRAI_WATERMARK_SPACING_Y_RATIO = env.float("MIRRAI_WATERMARK_SPACING_Y_RATIO", default=1.2)
MIRRAI_WATERMARK_STAGGER_RATIO = env.float("MIRRAI_WATERMARK_STAGGER_RATIO", default=0.48)
SUPABASE_BUCKET_FILE_SIZE_LIMIT = env.int("SUPABASE_BUCKET_FILE_SIZE_LIMIT", default=10 * 1024 * 1024)
SUPABASE_ALLOWED_MIME_TYPES = [
    item.strip()
    for item in env.list("SUPABASE_ALLOWED_MIME_TYPES", default=["image/jpeg", "image/png", "image/webp"])
    if item.strip()
]
MIRRAI_PERSIST_CAPTURE_IMAGES = env.bool("MIRRAI_PERSIST_CAPTURE_IMAGES", default=False)
MIRRAI_LOCAL_MOCK_RESULTS = env.bool("MIRRAI_LOCAL_MOCK_RESULTS", default=False)
TREND_REFRESH_ENABLED = env.bool("TREND_REFRESH_ENABLED", default=False)
TREND_REFRESH_INTERVAL_MINUTES = env.int("TREND_REFRESH_INTERVAL_MINUTES", default=0)
TREND_REFRESH_SOURCE = env("TREND_REFRESH_SOURCE", default="ai_repo_refresh_trends")
TREND_REFRESH_SOURCES = [item.strip() for item in env.list("TREND_REFRESH_SOURCES", default=[]) if item.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
    "app.apps.AppConfig",
]

MIDDLEWARE = [
    "app.middleware.ElasticBeanstalkHealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "app.middleware.BrowserSessionCleanupMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mirrai_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "mirrai_project.wsgi.application"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = env("TIME_ZONE", default="Asia/Seoul")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 86400 * 7
SESSION_SAVE_EVERY_REQUEST = env.bool("SESSION_SAVE_EVERY_REQUEST", default=True)
SESSION_ENGINE = env("SESSION_ENGINE", default="django.contrib.sessions.backends.db")
SESSION_CACHE_ALIAS = env("SESSION_CACHE_ALIAS", default="default")
if (
    REDIS_USE_FOR_SESSIONS
    and SESSION_ENGINE == "django.contrib.sessions.backends.db"
    and cache_uses_redis(cache_settings=CACHES, alias=SESSION_CACHE_ALIAS)
):
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

if DEBUG:
    STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
else:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "storage")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "MirrAI API",
    "DESCRIPTION": "MirrAI Django-first backend with Supabase-ready storage and data contracts.",
    "VERSION": "1.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CORS_ALLOW_ALL_ORIGINS = True

RUNPOD_BASE_URL = env("RUNPOD_BASE_URL", default="https://api.runpod.ai/v2").rstrip("/")
RUNPOD_API_KEY = env("RUNPOD_API_KEY", default="")
RUNPOD_ENDPOINT_ID = env("RUNPOD_ENDPOINT_ID", default="")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
TREND_REFINER_MODEL = env("TREND_REFINER_MODEL", default="gemini-2.5-flash")
TREND_SCHEDULER_ENABLED = env.bool("ENABLE_TREND_SCHEDULER", default=False)
TREND_SCHEDULER_TIMEZONE = env("TREND_SCHEDULER_TIMEZONE", default=TIME_ZONE)
TREND_SCHEDULER_WEEKLY_DAY = env("TREND_SCHEDULER_WEEKLY_DAY", default="fri")
TREND_SCHEDULER_WEEKLY_HOUR = env.int("TREND_SCHEDULER_WEEKLY_HOUR", default=8)
TREND_SCHEDULER_WEEKLY_MINUTE = env.int("TREND_SCHEDULER_WEEKLY_MINUTE", default=0)
TREND_SCHEDULER_STEPS = env("TREND_SCHEDULER_STEPS", default="crawl,refine,llm_refine,vectorize,rebuild_ncs,rebuild_styles")
TREND_SCHEDULER_INCLUDE_NCS = env.bool("TREND_SCHEDULER_INCLUDE_NCS", default=False)
TREND_SCHEDULER_INCLUDE_STYLES = env.bool("TREND_SCHEDULER_INCLUDE_STYLES", default=False)
TREND_SCHEDULER_TIMEOUT = env.int("TREND_SCHEDULER_TIMEOUT", default=1800)
TREND_SCHEDULER_POLL_INTERVAL = env.float("TREND_SCHEDULER_POLL_INTERVAL", default=5.0)
TREND_SCHEDULER_SLEEP_INTERVAL = env.float("TREND_SCHEDULER_SLEEP_INTERVAL", default=15.0)
TREND_SCHEDULER_TEST_AT = env("TREND_SCHEDULER_TEST_AT", default="")
TREND_LATEST_REMOTE_ENABLED = env.bool("TREND_LATEST_REMOTE_ENABLED", default=False)
TREND_LATEST_RUNPOD_TIMEOUT = env.int("TREND_LATEST_RUNPOD_TIMEOUT", default=8)
TREND_LATEST_RUNPOD_POLL_INTERVAL = env.float("TREND_LATEST_RUNPOD_POLL_INTERVAL", default=2.0)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "app": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
