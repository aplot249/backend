"""URL configuration for the project."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import path, re_path
from django.views.static import serve
from formapp import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.form_view, name="form"),
    path("api/search-name/", views.search_name, name="search-name"),
    path("api/search-project/", views.search_project, name="search-project"),
    path("api/list-project/", views.list_project, name="list-project"),
    path("api/submit/", views.submit_form, name="submit-form"),
    path("summary/", views.summary_view, name="summary"),
    path(
        "api/submission/<int:submission_id>/update-card/",
        views.update_card_image,
        name="update-card",
    ),
]

if settings.DEBUG:
    # 开发环境：直接由 Django 提供上传文件访问
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # 生产环境：若未用 Nginx 托管 /media/，则由 Django 提供访问，
    # 工卡图片属于敏感信息，仅允许已登录的后台用户访问。
    media_prefix = settings.MEDIA_URL.strip("/")

    @staff_member_required
    def protected_media(request, path):
        return serve(request, path, document_root=settings.MEDIA_ROOT, show_indexes=False)

    urlpatterns.append(
        re_path(
            rf"^{media_prefix}/(?P<path>.*)$",
            protected_media,
            name="protected-media",
        )
    )
