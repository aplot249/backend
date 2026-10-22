"""把工时统一挂到「人 + 项目」最新提交的工卡上，并按「人 + 项目 + 日期」去重。

用于修复：工时录在了历史工卡上，导致整理页合计与 Excel 导出把它过滤掉（显示为 0）。

用法：
    python manage.py consolidate_work_hours          # 只预览，不写库
    python manage.py consolidate_work_hours --apply  # 实际执行
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from formapp.models import WorkHour
from formapp.views import _latest_submission_map


class Command(BaseCommand):
    help = "把工时归并到最新工卡并去重（默认只预览）"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="实际写入数据库")

    def handle(self, *args, **options):
        latest = _latest_submission_map()

        # 1) 历史工卡上的工时改挂到最新工卡
        moved = 0
        for name, project in {(w.name, w.project) for w in WorkHour.objects.all()}:
            latest_id = latest.get((name, project))
            if not latest_id:
                continue
            queryset = WorkHour.objects.filter(name=name, project=project).exclude(
                submission_id=latest_id
            )
            count = queryset.count()
            if count:
                moved += count
                if options["apply"]:
                    WorkHour.objects.filter(name=name, project=project).exclude(
                        submission_id=latest_id
                    ).update(submission_id=latest_id)

        # 2) 同一「人 + 项目 + 日期」只保留一条（保留 id 最大的那条）
        duplicates = (
            WorkHour.objects.values("name", "project", "date")
            .annotate(c=Count("id"))
            .filter(c__gt=1)
        )
        dropped = 0
        for group in duplicates:
            rows = list(
                WorkHour.objects.filter(
                    name=group["name"], project=group["project"], date=group["date"]
                ).order_by("id")
            )
            keep = rows[-1]
            dropped += len(rows) - 1
            if options["apply"]:
                WorkHour.objects.filter(
                    name=group["name"], project=group["project"], date=group["date"]
                ).exclude(pk=keep.pk).delete()

        self.stdout.write(f"需要改挂到最新工卡的工时行：{moved}")
        self.stdout.write(f"重复的「人+项目+日期」多余行：{dropped}")
        if options["apply"]:
            self.stdout.write("已写入数据库")
        else:
            self.stdout.write("预览模式，未改动数据；确认无误后加 --apply 执行")
