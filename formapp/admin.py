from import_export import resources, fields
from import_export.admin import ImportExportMixin
from django import forms
from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    CardResubmitRequest,
    Name,
    Project,
    Submission,
    SystemSetting,
    WorkHour,
)
from .models import clean_spaces, normalize_name
from .views import _submit_is_locked


class NameResource(resources.ModelResource):
    """姓名库导入导出：以 name 字段作为唯一匹配键，避免重复。"""

    class Meta:
        model = Name
        fields = ("id", "name", "employee_no", "passport_no", "hourly_rate")
        export_order = ("id", "name", "employee_no", "passport_no", "hourly_rate")
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
    """提交记录导入导出（`name` 是姓名库的姓名，实时取值）。"""

    card_image_link = fields.Field(column_name="工卡图片链接")
    name = fields.Field(column_name="姓名")

    class Meta:
        model = Submission
        fields = ("id", "name", "project", "remark", "card_image_link", "created_at")
        export_order = ("id", "name", "project", "remark", "card_image_link", "created_at")

    def dehydrate_name(self, obj):
        return obj.person.name if obj.person_id else ""

    def dehydrate_card_image_link(self, obj):
        if not obj.card_image:
            return ""
        try:
            return obj.card_image.url
        except ValueError:
            return ""


class WorkHourResource(resources.ModelResource):
    """工时记录导入导出（`name` 是姓名库的姓名，实时取值）。"""

    name = fields.Field(column_name="姓名")

    class Meta:
        model = WorkHour
        fields = ("id", "name", "project", "date", "hours", "created_at")
        export_order = ("id", "name", "project", "date", "hours", "created_at")

    def dehydrate_name(self, obj):
        return obj.person.name if obj.person_id else ""


class NameAdminForm(forms.ModelForm):
    """姓名库表单：不允许改成「库里已有的名字」（忽略大小写 / 标点后同名也算）。

    否则会撞数据库的唯一约束报 500；放在表单里校验，改错了就在字段旁边红字提示。
    """

    class Meta:
        model = Name
        fields = "__all__"

    def clean_name(self):
        value = clean_spaces(self.cleaned_data.get("name"))
        clash = [
            other.name
            for other in Name.objects.exclude(pk=self.instance.pk).only("pk", "name")
            if normalize_name(other.name) == normalize_name(value)
        ]
        if clash:
            raise forms.ValidationError(
                f"姓名库里已经有「{clash[0]}」了（忽略大小写 / 标点后同名），"
                f"不能改成这个名字。要把两个人合并，请先删掉多余的那条姓名记录。"
            )
        return value


@admin.register(Name)
class NameAdmin(ImportExportMixin, admin.ModelAdmin):
    form = NameAdminForm
    list_display = (
        "name", "employee_no", "passport_no", "hourly_rate",
        "card_count", "hours_count",
    )
    list_editable = ("employee_no", "passport_no", "hourly_rate")
    search_fields = ("name", "employee_no", "passport_no")
    resource_class = NameResource

    @admin.display(description="工卡")
    def card_count(self, obj):
        return obj.submissions.count()

    @admin.display(description="工时")
    def hours_count(self, obj):
        return obj.work_hours.count()

    def save_model(self, request, obj, form, change):
        """改名后提示：他的工卡 / 工时会跟着显示新名字（外键关联，自动跟随）。"""
        old_name = None
        if change:
            old_name = (
                Name.objects.filter(pk=obj.pk).values_list("name", flat=True).first()
            )

        super().save_model(request, obj, form, change)

        if not old_name or normalize_name(old_name) == normalize_name(obj.name):
            return
        cards = obj.submissions.count()
        hours = obj.work_hours.count()
        if not (cards or hours):
            return
        messages.success(
            request,
            f"姓名已改为「{obj.name}」：他名下的 {cards} 条工卡记录、{hours} 条工时记录"
            f"都会跟着显示新名字（工卡 / 工时都用外键关联姓名库，无需手动同步）",
        )


@admin.register(Project)
class ProjectAdmin(ImportExportMixin, admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    resource_class = ProjectResource


@admin.register(Submission)
class SubmissionAdmin(ImportExportMixin, admin.ModelAdmin):
    list_display = ("name", "project", "remark", "card_thumbnail", "created_at")
    list_filter = ("project", "created_at")
    search_fields = ("person__name", "project", "remark")
    date_hierarchy = "created_at"
    readonly_fields = ("card_preview", "created_at")
    autocomplete_fields = ("person",)
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


@admin.register(SystemSetting)
class SystemSettingAdmin(admin.ModelAdmin):
    """首页展示哪个页面的开关：只保留一条记录，不允许新增 / 删除。"""

    list_display = ("form_enabled_label", "updated_at")
    fields = ("form_enabled",)
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not SystemSetting.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="首页状态")
    def form_enabled_label(self, obj):
        return "展示工卡填报表单（/ = form.html）" if obj.form_enabled else "展示工时查询页（/ = check.html）"


@admin.register(WorkHour)
class WorkHourAdmin(ImportExportMixin, admin.ModelAdmin):
    list_display = ("name", "project", "date", "hours", "updated_at")
    list_filter = ("project", "date")
    search_fields = ("person__name", "project")
    date_hierarchy = "date"
    autocomplete_fields = ("person",)
    resource_class = WorkHourResource


@admin.register(CardResubmitRequest)
class CardResubmitRequestAdmin(admin.ModelAdmin):
    """工人「申请再次提交工卡」：同意后放行**一次**，他提交完自动重新锁定。

    记录是工人在表单页被锁定时自动生成的，后台只能处理（同意 / 拒绝），不能新建。
    """

    list_display = (
        "person_name",
        "project",
        "period_key",
        "status_label",
        "lock_state",
        "attempts",
        "attempt_thumbnail",
        "remark",
        "created_at",
        "updated_at",
        "handled_by",
    )
    list_filter = ("status", "consumed", "period_key")
    search_fields = ("person__name", "project")
    readonly_fields = (
        "person",
        "project",
        "period_key",
        "attempts",
        "attempt_preview",
        "created_at",
        "updated_at",
        "handled_by",
        "handled_at",
    )
    actions = ("approve_requests", "reject_requests")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        """只能由工人提交时自动生成，后台不手工新增。"""
        return False

    @admin.display(description="姓名", ordering="person__name")
    def person_name(self, obj):
        return obj.person.name

    @admin.display(description="处理状态")
    def status_label(self, obj):
        label = obj.status_label
        color = {
            "待处理": "#e67e22",
            "已拒绝": "#e74c3c",
            "已同意·待提交": "#2d7ff9",
            "已同意·已提交": "#27ae60",
        }.get(label, "#666")
        return format_html('<b style="color:{}">{}</b>', color, label)

    @admin.display(description="当前是否锁定")
    def lock_state(self, obj):
        """实时判定：这个组合现在还能不能再交卡。"""
        if _submit_is_locked(obj.person, obj.project, obj.period_key):
            return format_html('<span style="color:#e74c3c">锁定中</span>')
        return format_html('<span style="color:#27ae60">已解锁（可再交一次）</span>')

    @admin.display(description="本次想提交的工卡")
    def attempt_thumbnail(self, obj):
        if not obj.attempt_image:
            return "-"
        try:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="height:56px;border-radius:6px;object-fit:cover;" /></a>',
                obj.attempt_image.url,
            )
        except ValueError:
            return "-"

    @admin.display(description="工卡大图")
    def attempt_preview(self, obj):
        if not obj.attempt_image:
            return "未上传"
        try:
            return format_html(
                '<a href="{0}" target="_blank">'
                '<img src="{0}" style="max-height:320px;max-width:100%;border-radius:8px;" /></a>',
                obj.attempt_image.url,
            )
        except ValueError:
            return "未上传"

    @admin.action(description="同意申请（放行一次提交）")
    def approve_requests(self, request, queryset):
        now = timezone.now()
        count = 0
        for item in queryset:
            # 再点一次「同意」也能把「已用掉」的重新打开（consumed 复位）
            item.status = CardResubmitRequest.STATUS_APPROVED
            item.consumed = False
            item.handled_by = request.user
            item.handled_at = now
            item.save(update_fields=["status", "consumed", "handled_by", "handled_at", "updated_at"])
            count += 1
        self.message_user(request, f"已同意 {count} 条申请，对应工人可重新提交一次工卡。")

    @admin.action(description="拒绝申请（保持锁定）")
    def reject_requests(self, request, queryset):
        now = timezone.now()
        count = 0
        for item in queryset:
            item.status = CardResubmitRequest.STATUS_REJECTED
            item.consumed = False
            item.handled_by = request.user
            item.handled_at = now
            item.save(update_fields=["status", "consumed", "handled_by", "handled_at", "updated_at"])
            count += 1
        self.message_user(request, f"已拒绝 {count} 条申请，对应工人仍处于锁定状态。")
