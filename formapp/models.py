from django.db import models


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


class Submission(models.Model):
    """用户填写的表单提交记录。"""

    name = models.CharField("姓名", max_length=100)
    project = models.CharField("项目", max_length=200)
    remark = models.TextField("工作内容", blank=True, default="")
    created_at = models.DateTimeField("提交时间", auto_now_add=True)

    class Meta:
        verbose_name = "提交记录"
        verbose_name_plural = "提交记录"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.project}"
