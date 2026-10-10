"""
LedgerGuard Django settings.

All environment-specific values (secrets, database credentials, hosts) are read
from environment variables. See `.env.example` for the full list.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, []),
    DJANGO_CSRF_TRUSTED_ORIGINS=(list, []),
    DJANGO_SECURE_COOKIES=(bool, True),
    DJANGO_SECURE_SSL_REDIRECT=(bool, False),
    DJANGO_HSTS_SECONDS=(int, 0),
    CELERY_TASK_ALWAYS_EAGER=(bool, False),
    LEDGERGUARD_MAX_UPLOAD_MB=(int, 25),
    LEDGERGUARD_LOGIN_MAX_FAILURES=(int, 5),
    LEDGERGUARD_LOGIN_LOCKOUT_SECONDS=(int, 900),
    LEDGERGUARD_SESSION_AGE_SECONDS=(int, 1800),
)
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "rest_framework.authtoken",
    "apps.core",
    "apps.organizations",
    "apps.accounts",
    "apps.approvals",
    "apps.notifications",
    "apps.accounting",
    "apps.imports",
    "apps.reconciliation",
    "apps.settlements",
    "apps.suspense",
    "apps.audit",
    "apps.reporting",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.middleware.RequestContextMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.MFAEnforcementMiddleware",
    "apps.core.middleware.InstitutionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.ledgerguard",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["CONN_MAX_AGE"] = 60

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "reporting:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
MEDIA_URL = "media/"
MEDIA_ROOT = Path(env("LEDGERGUARD_MEDIA_ROOT", default=str(BASE_DIR / "media")))

# --- Security -------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env("DJANGO_SECURE_COOKIES")
SESSION_COOKIE_AGE = env("LEDGERGUARD_SESSION_AGE_SECONDS")
SESSION_SAVE_EVERY_REQUEST = True  # sliding idle timeout
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_SECURE = env("DJANGO_SECURE_COOKIES")
CSRF_COOKIE_HTTPONLY = True
SECURE_SSL_REDIRECT = env("DJANGO_SECURE_SSL_REDIRECT")
SECURE_HSTS_SECONDS = env("DJANGO_HSTS_SECONDS")
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_PERMISSIONS = 0o640

CACHES = {
    "default": (
        env.cache("CACHE_URL")
        if env("CACHE_URL", default="")
        else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    )
}

# --- REST framework -------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
    "COERCE_DECIMAL_TO_STRING": True,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "2000/hour"},
}

# --- Celery ---------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=CELERY_BROKER_URL)
CELERY_TASK_ALWAYS_EAGER = env("CELERY_TASK_ALWAYS_EAGER")
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "suspense-escalations-daily": {
        "task": "apps.suspense.tasks.run_escalations",
        "schedule": 60 * 60 * 24,
    },
}

# --- LedgerGuard ----------------------------------------------------------
LEDGERGUARD = {
    "MAX_UPLOAD_BYTES": env("LEDGERGUARD_MAX_UPLOAD_MB") * 1024 * 1024,
    "ALLOWED_IMPORT_EXTENSIONS": [".csv", ".xlsx"],
    "LOGIN_MAX_FAILURES": env("LEDGERGUARD_LOGIN_MAX_FAILURES"),
    "LOGIN_LOCKOUT_SECONDS": env("LEDGERGUARD_LOGIN_LOCKOUT_SECONDS"),
    "MAX_IMPORT_ROWS": env.int("LEDGERGUARD_MAX_IMPORT_ROWS", default=500_000),
}

# --- Logging --------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "()": "apps.core.logging.StructuredFormatter",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "structured"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
    },
}
