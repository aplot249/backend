"""URL configuration for the project."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path
from formapp import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.form_view, name="form"),
    path("api/search-name/", views.search_name, name="search-name"),
    path("api/search-project/", views.search_project, name="search-project"),
    path("api/list-project/", views.list_project, name="list-project"),
    path("api/submit/", views.submit_form, name="submit-form"),
]

if settings.DEBUG:
    # 开发环境提供上传文件访问（生产环境请交给 Nginx 等 Web 服务器）
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
