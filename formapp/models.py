import re
import uuid
from pathlib import Path

from django.db import models
from django.utils import timezone


def clean_spaces(value):
    """去掉首尾空白，并把中间的连续空白合并成一个空格。

    姓名库、工卡提交、工时记录里的姓名 / 项目名统一走这个处理，
    避免 `' ABDUL MOJID'` 与 `'ABDUL MOJID'` 被当成两个人。
    """
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_name(value):
    """姓名归一化：去首尾与连续空白、统一大写，并把 `. : , ; _ - /` 也当成空格。

    姓名库与工卡里的姓名常见这几种差异（例如库里 `MD.SUMON`、工卡上 `MD SUMON`，
    库里 `MD: Johurul islam`、工卡上 `MD JOHURUL ISLAM`），只比空格和大小写会把同一个人
    当成两个人：整理页看不到他的工卡（也就没法裁剪 / 录工时），工时也统计不到。
    各处「按姓名匹配」都用这个函数，改名同步（`rename_history`）也用它判断是否实质改名。
    """
    text = re.sub(r"[.:,;_\-/]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().upper()


def _rows_with_name(model, old_name):
    """找出表里「归一化后等于该姓名」的所有行（姓名是文本字段，不能用 SQL 直接比）。"""
    key = normalize_name(old_name)
    if not key:
        return []
    return [obj for obj in model.objects.all() if normalize_name(obj.name) == key]


def rename_history(old_name, new_name):
    """把某个姓名在**工卡提交 / 工时记录**里的历史写法统一改成姓名库的新写法。

    后台改「姓名库」里的名字时会自动调用（`Name.save()`），这样改名不会把已有的
    工卡、工时记录甩在后面 —— 否则整理页、工时统计、PDF 导出、工人查询都会立刻
    看不到这个人（它们都靠姓名匹配）。

    匹配用 `normalize_name`，所以只改大小写 / 标点 / 空格（归一化后一样）不会重写数据。

    返回 `(改写的工卡条数, 改写的工时条数, 因重复而删掉的工时条数)`。
    """
    new_name = clean_spaces(new_name)
    if not new_name or normalize_name(old_name) == normalize_name(new_name):
        return (0, 0, 0)

    submissions = _rows_with_name(Submission, old_name)
    hours = _rows_with_name(WorkHour, old_name)

    # 工时表有「姓名 + 项目 + 日期」唯一约束：改名后可能撞上同名项目同日的已有记录，
    # 冲突时保留工时更大的一条（一样大就留后更新的），删掉另一条，避免报错丢整批
    incoming = {
        (normalize_name(new_name), _normalize_project(h.project), h.date)
        for h in hours
    }
    conflicts = {
        h
        for h in WorkHour.objects.all()
        if normalize_name(h.name) == normalize_name(new_name)
        and (normalize_name(h.name), _normalize_project(h.project), h.date) in incoming
        and normalize_name(h.name) != normalize_name(old_name)
    }
    dropped = 0
    for row in conflicts:
        rival = next(
            (
                h
                for h in hours
                if _normalize_project(h.project) == _normalize_project(row.project)
                and h.date == row.date
            ),
            None,
        )
        if rival is None:
            continue
        loser, keeper = (
            (row, rival)
            if (rival.hours, rival.updated_at) >= (row.hours, row.updated_at)
            else (rival, row)
        )
        loser.delete()
        dropped += 1
        if loser is rival:
            hours.remove(rival)

    for submission in submissions:
        submission.name = new_name
        submission.save(update_fields=["name"])
    for row in hours:
        row.name = new_name
        row.save(update_fields=["name"])

    return (len(submissions), len(hours), dropped)


def _normalize_project(value):
    """项目名归一化：与姓名同一套规则（大小写 / 空格 / 标点不敏感）。"""
    return normalize_name(value)


class Name(models.Model):
    """从 Excel 导入的姓名数据。

    **改名会自动同步**：改 `name` 时，`Submission` / `WorkHour` 里同样姓名的记录
    会一并改成新名字（见 `rename_history`），不会把历史数据甩在后面。
    """

    name = models.CharField("姓名", max_length=100, unique=True)
    employee_no = models.CharField("工号", max_length=50, blank=True, default="")
    passport_no = models.CharField("护照号", max_length=50, blank=True, default="")
    hourly_rate = models.DecimalField(
        "工时系数",
        max_digits=8,
        decimal_places=2,
        default=0,
        help_text="每小时金额（元/小时），用于与工作小时数相乘计算总金额",
    )

    class Meta:
        verbose_name = "姓名库"
        verbose_name_plural = "姓名库"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = clean_spaces(self.name)
        self.employee_no = clean_spaces(self.employee_no)
        self.passport_no = clean_spaces(self.passport_no)

        # 改名前的旧名字（新增记录时为 None）
        old_name = None
        if self.pk:
            old_name = (
                Name.objects.filter(pk=self.pk).values_list("name", flat=True).first()
            )

        super().save(*args, **kwargs)

        # 实质性改名（不只是大小写 / 标点 / 空格变化）时，把工卡 / 工时里的姓名一起改掉
        self.rename_report = (
            rename_history(old_name, self.name) if old_name else (0, 0, 0)
        )


class Project(models.Model):
    """从 Excel 导入的项目数据。"""

    name = models.CharField("项目名称", max_length=200, unique=True)

    class Meta:
        verbose_name = "项目库"
        verbose_name_plural = "项目库"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = clean_spaces(self.name)
        super().save(*args, **kwargs)


def work_card_upload_to(instance, filename):
    """工卡图片保存路径：work_cards/年/月/随机文件名.扩展名。"""
    suffix = Path(filename).suffix.lower() or ".jpg"
    return f"work_cards/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{suffix}"


class Submission(models.Model):
    """用户填写的表单提交记录。"""

    name = models.CharField("姓名", max_length=100)
    project = models.CharField("项目", max_length=200)
    remark = models.TextField("工作内容", blank=True, default="")
    card_image = models.ImageField(
        "工卡图片",
        upload_to=work_card_upload_to,
        blank=True,
        null=True,
        help_text="支持 jpg / png / webp 等常见图片格式，单张不超过 10MB",
    )
    created_at = models.DateTimeField("提交时间", auto_now_add=True)

    class Meta:
        verbose_name = "提交记录"
        verbose_name_plural = "提交记录"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.project}"

    def save(self, *args, **kwargs):
        self.name = clean_spaces(self.name)
        self.project = clean_spaces(self.project)
        super().save(*args, **kwargs)


class SystemSetting(models.Model):
    """系统开关（全局只有一条记录，pk 固定为 1）。"""

    form_enabled = models.BooleanField(
        "展示填报表单",
        default=True,
        help_text="勾选：首页 / 展示工卡填报表单；取消勾选：首页改为展示工时查询页（暂停填报）",
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "系统开关"
        verbose_name_plural = "系统开关"

    def __str__(self):
        return f"系统开关（{'填报中' if self.form_enabled else '仅查询'}）"

    def save(self, *args, **kwargs):
        self.pk = 1  # 始终只有一条记录
        super().save(*args, **kwargs)

    @classmethod
    def get_setting(cls):
        """取系统开关（没有就按默认「展示表单」创建一条）。"""
        setting, _ = cls.objects.get_or_create(pk=1)
        return setting


class WorkHour(models.Model):
    """工天数据：某工人在某天的工作小时数（从工卡图片人工录入）。"""

    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="work_hours",
        verbose_name="所属提交记录",
    )
    name = models.CharField("工人姓名", max_length=100, db_index=True)
    project = models.CharField("项目", max_length=200, default="", db_index=True)
    date = models.DateField("工作日期", db_index=True)
    hours = models.DecimalField("工作小时数", max_digits=5, decimal_places=2, default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "工时记录"
        verbose_name_plural = "工时记录"
        ordering = ["date"]
        # 一人 + 一个项目 + 一天 只保留一条记录，工时统一挂在最新提交的工卡上
        unique_together = ("name", "project", "date")

    def __str__(self):
        return f"{self.name} ({self.project}) {self.date} {self.hours}h"

    def save(self, *args, **kwargs):
        self.name = clean_spaces(self.name)
        self.project = clean_spaces(self.project)
        super().save(*args, **kwargs)
