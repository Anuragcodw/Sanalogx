import os
from pathlib import Path
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / '.env')

# Bypass Django 6.1 MySQL 8.4+ version restriction for local MySQL 8.0 compat
from django.db.backends.base.base import BaseDatabaseWrapper
BaseDatabaseWrapper.check_database_version_supported = lambda self: None

# Quick-start development settings - unsuitable for production
SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-default-key-change-in-production')
DEBUG = os.getenv('DEBUG', 'True').lower() in ('true', '1', 't')

ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',') if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]

INSTALLED_APPS = [
    # Django core apps
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party apps
    'rest_framework',
    'corsheaders',
    'drf_spectacular',

    # Project apps
    'accounts',
    'medicines',
    'inventory',
    'dispensing',
    'suppliers',
    'purchases',
    'ai_engine',
    'notifications',
    'analytics',
    'reports',
    'sales',
    'integrations',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'accounts.middleware.NoCacheAuthenticatedMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Custom User Model
AUTH_USER_MODEL = 'accounts.User'

# Database Configuration
db_options = {
    'charset': 'utf8mb4',
}

# SSL Configuration for Cloud MySQL (e.g., Aiven, Azure, AWS RDS)
db_ssl_ca = os.getenv('DB_SSL_CA')
if db_ssl_ca:
    ca_path = Path(db_ssl_ca)
    if not ca_path.is_absolute():
        ca_path = BASE_DIR / ca_path
    if ca_path.exists():
        db_options['ssl'] = {'ca': str(ca_path)}
elif os.getenv('DB_SSL_REQUIRE', 'False').lower() in ('true', '1', 't'):
    db_options['ssl'] = {}

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.getenv('DB_NAME', 'medivault'),
        'USER': os.getenv('DB_USER', 'root'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', '127.0.0.1'),
        'PORT': os.getenv('DB_PORT', '3306'),
        'OPTIONS': db_options,
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Media files (Uploaded images & documents)
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CORS Configuration
CORS_ALLOW_ALL_ORIGINS = os.getenv('CORS_ALLOW_ALL_ORIGINS', 'False').lower() in ('true', '1', 't') if not DEBUG else True

# Security & Session Hardening
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True').lower() in ('true', '1', 't')
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Upload Size Limits
DATA_UPLOAD_MAX_MEMORY_SIZE = 5242880  # 5MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 5242880  # 5MB

# Django REST Framework Configuration
REST_FRAMEWORK = {
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
}

# DRF Spectacular Settings (OpenAPI documentation)
SPECTACULAR_SETTINGS = {
    'TITLE': 'Sanalogx API Documentation',
    'DESCRIPTION': 'Hospital Pharmacy Stock Optimization & Inventory Management API',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}

# Redis & Celery Configuration
REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/1')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# ── Sanalogx Platform Identity ─────────────────────────────────────────────
SANALOGX_SITE_NAME = os.getenv('SANALOGX_SITE_NAME', os.getenv('MEDIVAULT_SITE_NAME', 'Sanalogx'))
MEDIVAULT_SITE_NAME = SANALOGX_SITE_NAME
SANALOGX_BASE_URL = os.getenv('SANALOGX_BASE_URL', os.getenv('MEDIVAULT_BASE_URL', 'http://127.0.0.1:8000')).rstrip('/')
MEDIVAULT_BASE_URL = SANALOGX_BASE_URL

# ── Email Configuration ────────────────────────────────────────────────────
_env_backend = os.getenv('EMAIL_BACKEND')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com').strip()
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))

_raw_ssl = os.getenv('EMAIL_USE_SSL')
_raw_tls = os.getenv('EMAIL_USE_TLS')

if _raw_ssl is not None:
    EMAIL_USE_SSL = _raw_ssl.lower() in ('true', '1', 't')
else:
    EMAIL_USE_SSL = (EMAIL_PORT == 465)

if _raw_tls is not None:
    EMAIL_USE_TLS = _raw_tls.lower() in ('true', '1', 't')
else:
    EMAIL_USE_TLS = not EMAIL_USE_SSL

# Enforce mutual exclusivity to prevent smtplib / Django ValueError
if EMAIL_USE_SSL and EMAIL_USE_TLS:
    if EMAIL_PORT == 465:
        EMAIL_USE_TLS = False
    else:
        EMAIL_USE_SSL = False

EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '').strip()

# Clean password: strip surrounding whitespace and quotes if present
_raw_pwd = os.getenv('EMAIL_HOST_PASSWORD', '').strip()
if (_raw_pwd.startswith('"') and _raw_pwd.endswith('"')) or (_raw_pwd.startswith("'") and _raw_pwd.endswith("'")):
    _raw_pwd = _raw_pwd[1:-1].strip()
EMAIL_HOST_PASSWORD = _raw_pwd

# Sender Identity:
_env_from = os.getenv('DEFAULT_FROM_EMAIL', '').strip()
if _env_from:
    if '<' in _env_from and '>' in _env_from:
        DEFAULT_FROM_EMAIL = _env_from
    else:
        DEFAULT_FROM_EMAIL = f"{SANALOGX_SITE_NAME} | Hospital Pharmacy Intelligence <{_env_from}>"
elif EMAIL_HOST_USER:
    DEFAULT_FROM_EMAIL = f"{SANALOGX_SITE_NAME} | Hospital Pharmacy Intelligence <{EMAIL_HOST_USER}>"
else:
    DEFAULT_FROM_EMAIL = f"{SANALOGX_SITE_NAME} | Hospital Pharmacy Intelligence <noreply@medivault.com>"

SERVER_EMAIL = os.getenv('SERVER_EMAIL', EMAIL_HOST_USER or 'noreply@medivault.com').strip()
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', '10'))  # seconds

# Select backend: use explicit env backend if provided; otherwise fallback to console in dev if no credentials
if _env_backend:
    EMAIL_BACKEND = _env_backend
elif not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'

# ── Email Verification ─────────────────────────────────────────────────────
EMAIL_VERIFICATION_TIMEOUT_HOURS = int(os.getenv('EMAIL_VERIFICATION_TIMEOUT_HOURS', '24'))

# ── Authentication URLs ────────────────────────────────────────────────────
LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/auth/approved/'
LOGOUT_REDIRECT_URL = '/'

# ── Logging ────────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'accounts': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# ── Medicine Expiry Configuration ─────────────────────────────────────────
NEAR_EXPIRY_THRESHOLD_DAYS = 90

# ── Demo Registration Configuration ──────────────────────────────────────────
# In development/demo environments, pharmacist registrations accept demo reference numbers:
# PH- followed by 4 digits (pattern: ^PH-[0-9]{4}$).
# Set DEMO_REGISTRATION_MODE=False in production .env to disable demo registrations.
DEMO_REGISTRATION_MODE = os.getenv(
    'DEMO_REGISTRATION_MODE',
    str(DEBUG)
).lower() in ('true', '1', 't')

# ── Session & Cookie Security Configuration ───────────────────────────────────
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_AGE = int(os.getenv('SESSION_COOKIE_AGE', 43200))  # 12 hours
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = not DEBUG and os.getenv('SESSION_COOKIE_SECURE', 'True').lower() in ('true', '1', 't')
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = not DEBUG and os.getenv('CSRF_COOKIE_SECURE', 'True').lower() in ('true', '1', 't')


