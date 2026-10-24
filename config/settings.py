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
        "USER": "root" if platform.system() in ['Windows'] else 'labormanage',
        "PASSWORD": "qq1788lover",
        "HOST": "127.0.0.1",
        # "HOST": "127.0.0.1" if platform.system() in ['Windows'] else '49.51.166.98',
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

USE_TZ = False

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# 上传文件（工卡图片）
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# 单次请求允许的最大上传体积（10MB 工卡图片 + 表单开销）
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

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

# ---------- AI 工卡识别（整理页「AI 识别」按钮用） ----------
# 任何 OpenAI 兼容的「视觉模型」接口都可以，用环境变量配置（不配则按钮提示未配置）：
#   set AI_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1   （阿里云百炼，模型 qwen-vl-max）
#   set AI_VISION_API_KEY=sk-xxxx
#   set AI_VISION_MODEL=qwen-vl-max
# 其他可选：智谱 https://open.bigmodel.cn/api/paas/v4 (glm-4v-plus)、
#          OpenAI https://api.openai.com/v1 (gpt-4o-mini)、
#          Gemini https://generativelanguage.googleapis.com/v1beta/openai (gemini-2.0-flash)
AI_VISION_BASE_URL = os.getenv("AI_VISION_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
AI_VISION_API_KEY = os.getenv("AI_VISION_API_KEY", "9cd15fc5535c49729d9180ef53f88492.Svvy0VkREsZUdce4")
AI_VISION_MODEL = os.getenv("AI_VISION_MODEL", "glm-4.6v")
AI_VISION_TIMEOUT = int(os.getenv("AI_VISION_TIMEOUT", "180"))
# 智谱 GLM-4.5V / 4.6V 是推理模型，默认会先「想」很久才回答（读一张卡可能 90 秒以上）。
# 数格子这种任务不需要推理，默认关掉思考（实测快很多）；想让它推理就设 AI_VISION_DISABLE_THINKING=0
AI_VISION_DISABLE_THINKING = os.getenv("AI_VISION_DISABLE_THINKING", "1") == "1"
