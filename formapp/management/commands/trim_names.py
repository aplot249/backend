"""一次性清理：把姓名库、工卡提交、工时记录里的姓名 / 项目名前后空格去掉。

同时把中间连续空格合并成一个空格（`'ABDUL  MOJID'` -> `'ABDUL MOJID'`）。
清理后「姓名库」与「工卡 / 工时」里的姓名就是同一个字符串，不再依赖归一化匹配。

用法：
    python manage.py trim_names            # 只预览，不写库
    python manage.py trim_names --apply    # 实际执行
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from formapp.models import Name, Project, Submission, WorkHour
from formapp.models import clean_spaces


class Command(BaseCommand):
    help = "去掉姓名 / 项目名前后的空格（默认只预览）"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="实际写入数据库")

    def _report(self, text):
        self.stdout.write(text)

    def handle(self, *args, **options):
        apply = options["apply"]

        # ---------- 1. 姓名库 ----------
        self._report("【姓名库】")
        changed = []
        for person in Name.objects.all():
            cleaned = clean_spaces(person.name)
            if cleaned != person.name:
                changed.append((person, cleaned))
        self._report(f"  需要修整空格：{len(changed)} 人")

        # 修整后可能与已有姓名重复（姓名库 name 唯一），需要先处理冲突
        existing = {}
        for person in Name.objects.all():
            existing.setdefault(clean_spaces(person.name).upper(), []).append(person)

        merged = 0
        plain = []
        for person, cleaned in changed:
            others = [p for p in existing.get(cleaned.upper(), []) if p.pk != person.pk]
            if others:
                merged += 1
                other = others[0]
                self._report(
                    f"  冲突：{person.name!r} 与 {other.name!r} 修整后同名 -> "
                    f"保留信息更全的一条"
                )
                if apply:
                    keep, drop = (
                        (other, person)
                        if _score(other) >= _score(person)
                        else (person, other)
                    )
                    for field in ("employee_no", "passport_no"):
                        if not getattr(keep, field) and getattr(drop, field):
                            setattr(keep, field, getattr(drop, field))
                    if not keep.hourly_rate and drop.hourly_rate:
                        keep.hourly_rate = drop.hourly_rate
                    keep.name = cleaned
                    keep.save()
                    drop.delete()
            else:
                plain.append((person, cleaned))

        if apply:
            with transaction.atomic():
                for person, cleaned in plain:
                    person.name = cleaned
                    person.save()
        self._report(f"  实际改名：{len(plain) if apply else 0} 人（冲突合并 {merged} 人）")

        # ---------- 2. 工卡提交（提交记录） ----------
        self._report("【提交记录（工卡）】")
        sub_changed = []
        for submission in Submission.objects.all():
            cleaned_name = clean_spaces(submission.name)
            cleaned_project = clean_spaces(submission.project)
            if cleaned_name != submission.name or cleaned_project != submission.project:
                sub_changed.append((submission, cleaned_name, cleaned_project))
        self._report(f"  需要修整空格：{len(sub_changed)} 条")
        if apply:
            with transaction.atomic():
                for submission, cleaned_name, cleaned_project in sub_changed:
                    submission.name = cleaned_name
                    submission.project = cleaned_project
                    submission.save()

        # ---------- 3. 工时记录 ----------
        self._report("【工时记录】")
        hour_changed = []
        for row in WorkHour.objects.all():
            cleaned_name = clean_spaces(row.name)
            cleaned_project = clean_spaces(row.project)
            if cleaned_name != row.name or cleaned_project != row.project:
                hour_changed.append((row, cleaned_name, cleaned_project))
        self._report(f"  需要修整空格：{len(hour_changed)} 条")
        if apply:
            with transaction.atomic():
                for row, cleaned_name, cleaned_project in hour_changed:
                    row.name = cleaned_name
                    row.project = cleaned_project
                    row.save()

            # 修整后可能撞上「人 + 项目 + 日期」唯一约束，去掉多余行
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
                WorkHour.objects.filter(
                    name=group["name"], project=group["project"], date=group["date"]
                ).exclude(pk=keep.pk).delete()
            self._report(f"  修整后重复的「人+项目+日期」多余行：{dropped}（已保留 1 条）")

        # ---------- 4. 项目库 ----------
        self._report("【项目库】")
        proj_changed = []
        for project in Project.objects.all():
            cleaned = clean_spaces(project.name)
            if cleaned != project.name:
                proj_changed.append((project, cleaned))
        self._report(f"  需要修整空格：{len(proj_changed)} 条")
        if apply:
            with transaction.atomic():
                for project, cleaned in proj_changed:
                    project.name = cleaned
                    project.save()

        self._report("")
        if apply:
            self._report("已写入数据库。之后新增数据会自动去空格（模型 save 里统一处理）")
        else:
            self._report("预览模式，未改动数据；确认无误后加 --apply 执行")


def _score(person):
    """信息完整度：工号、护照、单价齐全的优先保留。"""
    return sum(
        [
            1 if (person.employee_no or "").strip() else 0,
            1 if (person.passport_no or "").strip() else 0,
            1 if person.hourly_rate else 0,
        ]
    )
