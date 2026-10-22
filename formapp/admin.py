from import_export import resources, fields
from import_export.admin import ImportExportMixin
from django.contrib import admin
from django.utils.html import format_html

from .models import Name, Project, Submission


class NameResource(resources.ModelResource):
    """姓名库导入导出：以 name 字段作为唯一匹配键，避免重复。"""

    class Meta:
        model = Name
        fields = ("id", "name")
        import_id_fields = ("name",)
        skip_unchanged = True
        report_skipped = True


class ProjectResource(resources.ModelResource):
    """项目库导入导出：以 name 字段作为唯一匹配键，避免重复。"""

    class Meta:
        model = Project
        fields = ("id", "name")
        import_id_fields = ("name",)
        skip_unchanged = True
        report_skipped = True


class SubmissionResource(resources.ModelResource):
    """提交记录导入导出。"""

    card_image_link = fields.Field(column_name="工卡图片链接")

    class Meta:
        model = Submission
        fields = ("id", "name", "project", "remark", "card_image_link", "created_at")
        export_order = ("id", "name", "project", "remark", "card_image_link", "created_at")

    def dehydrate_card_image_link(self, obj):
        if not obj.card_image:
            return ""
        try:
            return obj.card_image.url
        except ValueError:
            return ""


@admin.register(Name)
class NameAdmin(ImportExportMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    resource_class = NameResource


@admin.register(Project)
class ProjectAdmin(ImportExportMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    resource_class = ProjectResource


@admin.register(Submission)
class SubmissionAdmin(ImportExportMixin, admin.ModelAdmin):
    list_display = ("name", "project", "remark", "card_thumbnail", "created_at")
    list_filter = ("project", "created_at")
    search_fields = ("name", "project", "remark")
    date_hierarchy = "created_at"
    readonly_fields = ("card_preview", "created_at")
    resource_class = SubmissionResource

    @admin.display(description="工卡图片")
    def card_thumbnail(self, obj):
        if not obj.card_image:
            return "-"
        try:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="height:56px;border-radius:6px;object-fit:cover;" /></a>',
                obj.card_image.url,
            )
        except ValueError:
            return "-"

    @admin.display(description="工卡图片预览")
    def card_preview(self, obj):
        if not obj.card_image:
            return "未上传"
        try:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:320px;max-width:100%;border-radius:8px;" /></a>',
                obj.card_image.url,
            )
        except ValueError:
            return "未上传"
