"""Django settings for the form statistics project."""
from pathlib import Path
import os
import platform

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-form-statistics-dev-key-change-in-production"

if platform.system() in ["Windows", "Darwin"]:
    DEBUG = True
    STATICFILES_DIRS = [
        BASE_DIR.joinpath("static"),
    ]
else:
    DEBUG = False   # 正式环境，关闭debug
    STATIC_ROOT = BASE_DIR.joinpath('static')

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "simpleui",
    "import_export",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "formapp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": "labormanage",
        "USER": "root" if platform in ['windows'] else 'labormanage',
        "PASSWORD": "qq1788lover",
        "HOST": "127.0.0.1",
        "PORT": "3306",
        "OPTIONS": {
            "charset": "utf8mb4",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "zh-hans"

TIME_ZONE = "Asia/Shanghai"

USE_I18N = True

USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# SimpleUI 配置
SIMPLEUI_HOME_TITLE = "表单统计后台"
SIMPLEUI_HOME_ICON = "fa fa-clipboard-list"
SIMPLEUI_LOGO = "表单统计"
SIMPLEUI_DEFAULT_THEME = "admin.lte.css"
SIMPLEUI_HOME_INFO = False
SIMPLEUI_ANALYSIS = False
SIMPLEUI_ICON = {
    "姓名库": "fa fa-user",
    "项目库": "fa fa-folder-open",
    "提交记录": "fa fa-list-alt",
}
SIMPLEUI_HOME_QUICK = True
SIMPLEUI_HOME_ACTION = True
