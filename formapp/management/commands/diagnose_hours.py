"""诊断某工人的工时在「整理页合计 / 打卡弹窗 / 导出」三个口径下是否一致。

用法：
    python manage.py diagnose_hours                       # 列出所有有工时的人
    python manage.py diagnose_hours --name "ABDUL MOJID"  # 只看某个人

输出里重点看三处：
1. 姓名库是否精确匹配（前后空格 / 大小写不一致会导致整理页合计为 0）
2. 工时挂在的工卡是不是「人 + 项目」的最新卡（挂在历史卡上会被合计与导出过滤掉）
3. 「全部合计」与「页面口径合计」是否一致
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum

from formapp.models import Name, Submission, WorkHour
from formapp.views import _latest_submission_ids, _normalize_name


class Command(BaseCommand):
    help = "诊断工时在整理页合计 / 弹窗 / 导出三个口径下的差异"

    def add_arguments(self, parser):
        parser.add_argument("--name", default="", help="工人姓名，留空则检查所有有工时的人")

    def handle(self, *args, **options):
        latest_ids = _latest_submission_ids()
        keyword = (options["name"] or "").strip()

        if keyword:
            names = sorted({w.name for w in WorkHour.objects.filter(name__icontains=keyword)})
            if not names:
                names = [keyword]
        else:
            names = sorted({w.name for w in WorkHour.objects.all()})

        if not names:
            self.stdout.write("没有任何工时记录")
            return

        for name in names:
            rows = WorkHour.objects.filter(name=name)
            total_all = rows.aggregate(t=Sum("hours"))["t"] or Decimal("0")
            total_page = (
                rows.filter(submission_id__in=latest_ids).aggregate(t=Sum("hours"))["t"]
                or Decimal("0")
            )
            person = Name.objects.filter(name=name).first()
            person_n = Name.objects.filter(name__iexact=name.strip()).first()

            self.stdout.write("=" * 72)
            self.stdout.write(f"工时里的姓名        : {name!r}")
            self.stdout.write(f"姓名库精确匹配      : {person.name!r}" if person else "姓名库精确匹配      : 无（!! 整理页合计会显示 0）")
            self.stdout.write(
                f"姓名库归一化后匹配  : {person_n.name!r}"
                if person_n
                else "姓名库归一化后匹配  : 无"
            )
            self.stdout.write(f"全部工时合计        : {total_all}")
            self.stdout.write(f"整理页口径合计      : {total_page}（仅最新工卡）")
            if total_all != total_page:
                self.stdout.write("  !! 差异来自「工时挂在非最新工卡上」，合计与导出会漏掉这部分")

            for submission_id in rows.order_by().values_list(
                "submission_id", flat=True
            ).distinct():
                submission = Submission.objects.get(pk=submission_id)
                sub_total = rows.filter(submission_id=submission_id).aggregate(
                    t=Sum("hours")
                )["t"]
                latest_id = (
                    Submission.objects.filter(
                        name=submission.name, project=submission.project
                    )
                    .order_by("-created_at", "-id")
                    .first()
                    .pk
                )
                self.stdout.write(
                    f"  工卡#{submission_id} 项目={submission.project!r} "
                    f"提交={submission.created_at} 行数={rows.filter(submission_id=submission_id).count()} "
                    f"合计={sub_total} 是否最新卡={submission_id == latest_id}"
                    + ("" if submission_id == latest_id else "  <-- 历史卡，页面/导出不计")
                )
