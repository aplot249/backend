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
    """归一化：去首尾与连续空白、统一大写，并把 `. : , ; _ - /` 也当成空格。

    用在两个地方：
      ① **姓名**：工人填的姓名要匹配到姓名库（库里 `MD.SUMON`、工人写 `MD SUMON`），
         只比空格和大小写会把同一个人当成两个人
      ② **项目名**：同一个项目被写成两种样子时算作同一个（`_latest_submission_map`）
    """
    text = re.sub(r"[.:,;_\-/]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().upper()


class Name(models.Model):
    """从 Excel 导入的姓名数据。

    工卡提交（`Submission`）与工时记录（`WorkHour`）都用**外键指向这张表**，
    所以：
      - 后台改这里的姓名，所有相关记录自动跟着显示新名字（数据库外键天然跟随，
        不需要任何同步代码）
      - 不可能再出现「姓名对不上库」的记录 —— 工卡一定属于库里的某个人
      - 删人时被引用会拦下（`on_delete=PROTECT`），避免误删带走他的工卡 / 工时
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
        super().save(*args, **kwargs)


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
    """用户填写的表单提交记录（一张工卡）。

    姓名用**外键指向姓名库**（`person`），不存文本副本：
    后台改名自动跟随，也不可能出现「姓名对不上库」的工卡。
    """

    person = models.ForeignKey(
        Name,
        on_delete=models.PROTECT,
        related_name="submissions",
        verbose_name="姓名库",
        help_text="这张工卡属于姓名库里的哪个人（改姓名库的姓名，这里自动跟着变）",
    )
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

    @property
    def name(self):
        """姓名一律**实时从姓名库取**（只读，不存副本），改名后立刻生效。"""
        return self.person.name if self.person_id else ""

    def __str__(self):
        return f"{self.name} - {self.project}"

    def save(self, *args, **kwargs):
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
    """工天数据：某工人在某天的工作小时数（从工卡图片人工录入）。

    与 `Submission` 一样用外键指向姓名库（`person`），不存文本副本。
    """

    submission = models.ForeignKey(
        Submission,
        on_delete=models.CASCADE,
        related_name="work_hours",
        verbose_name="所属提交记录",
    )
    person = models.ForeignKey(
        Name,
        on_delete=models.PROTECT,
        related_name="work_hours",
        verbose_name="姓名库",
        help_text="这条工时属于姓名库里的哪个人（默认跟着所属工卡走）",
    )
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
        unique_together = ("person", "project", "date")

    @property
    def name(self):
        """姓名**实时从姓名库取**（只读），改名后立刻生效。"""
        return self.person.name if self.person_id else ""

    def __str__(self):
        return f"{self.name} ({self.project}) {self.date} {self.hours}h"

    def save(self, *args, **kwargs):
        self.project = clean_spaces(self.project)
        # 没指定人就跟所属工卡走，保证工时和工卡属于同一个人
        if self.person_id is None and self.submission_id:
            self.person_id = self.submission.person_id
        super().save(*args, **kwargs)
