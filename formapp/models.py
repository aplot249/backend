import uuid
from pathlib import Path

from django.db import models
from django.utils import timezone


class Name(models.Model):
    """从 Excel 导入的姓名数据。"""

    name = models.CharField("姓名", max_length=100, unique=True)

    class Meta:
        verbose_name = "姓名库"
        verbose_name_plural = "姓名库"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Project(models.Model):
    """从 Excel 导入的项目数据。"""

    name = models.CharField("项目名称", max_length=200, unique=True)

    class Meta:
        verbose_name = "项目库"
        verbose_name_plural = "项目库"
        ordering = ["name"]

    def __str__(self):
        return self.name


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
