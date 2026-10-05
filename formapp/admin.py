from import_export import resources, fields
from import_export.widgets import ForeignKeyWidget
from import_export.admin import ImportExportMixin
from django.contrib import admin

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

    class Meta:
        model = Submission
        fields = ("id", "name", "project", "remark", "created_at")


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
    list_display = ("name", "project", "remark", "created_at")
    list_filter = ("project", "created_at")
    search_fields = ("name", "project", "remark")
    date_hierarchy = "created_at"
    resource_class = SubmissionResource
