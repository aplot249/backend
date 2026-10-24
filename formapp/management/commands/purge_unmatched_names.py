"""按人列出 / 删除工卡记录（**默认只预览**，删之前逐条给你看清楚）。

> 工卡（`Submission`）与工时（`WorkHour`）现在都用**外键指向姓名库**，
> 所以已经不存在「姓名在姓名库里找不到」的记录了 —— 这一类数据不会再产生。
> 这个命令保留下来，用来**按人核对 / 清理工卡**：先列出明细，确认后再删。

**删除前先看清楚再决定**：默认只列出不删；加 `--delete` 会再要求手输 DELETE 才真删。
会一并把工卡图片文件和挂在它们上面的工时记录删掉（不可恢复）。

用法：
    python manage.py purge_unmatched_names                   # 只列出，不删（默认）
    python manage.py purge_unmatched_names --name SHILON      # 只看姓名含该关键字的
    python manage.py purge_unmatched_names --skip-vacation    # 不列出「在休假」的记录
    python manage.py purge_unmatched_names --delete           # 列出后要手输 DELETE 才删
    python manage.py purge_unmatched_names --delete --yes     # 跳过确认（慎用）
"""
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import transaction

from formapp.models import Name, Submission, WorkHour
from formapp.views import VACATION_PROJECT


def _human_size(num):
    """字节数转成人能看的大小。"""
    try:
        num = float(num or 0)
    except (TypeError, ValueError):
        return "-"
    if num < 1024:
        return f"{num:.0f} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / 1024 / 1024:.2f} MB"


def _file_size(submission):
    """工卡图片大小（文件丢了就当 0）。"""
    if not submission.card_image:
        return 0
    try:
        return submission.card_image.size or 0
    except Exception:
        return 0


class Command(BaseCommand):
    help = "按人列出 / 删除工卡记录（默认只预览）"

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true", help="确认后真正删除")
        parser.add_argument(
            "--yes", action="store_true", help="跳过手输 DELETE 那一步（慎用）"
        )
        parser.add_argument(
            "--name", type=str, default="", help="只看姓名包含该关键字的记录"
        )
        parser.add_argument(
            "--skip-vacation", action="store_true", help="不处理「在休假」的记录"
        )

    def handle(self, *args, **options):
        keyword = (options["name"] or "").strip().upper()

        # 按人分组（工卡用外键指向姓名库，直接按人取）
        groups = {}
        queryset = (
            Submission.objects.select_related("person")
            .order_by("person__name", "created_at")
        )
        for submission in queryset:
            if keyword and keyword not in (submission.person.name or "").upper():
                continue
            if options["skip_vacation"] and submission.project == VACATION_PROJECT:
                continue
            groups.setdefault(submission.person_id, []).append(submission)

        self.stdout.write("")
        self.stdout.write("=" * 78)
        self.stdout.write("工卡记录（按姓名库的人分组；删除会连同图片和工时记录一起删掉）")
        self.stdout.write("=" * 78)

        if not groups:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("没有符合条件的工卡记录，无需处理"))
            return

        total_cards = 0
        total_size = 0
        for index, person_id in enumerate(
            sorted(groups, key=lambda pk: groups[pk][0].person.name), 1
        ):
            items = groups[person_id]
            size = sum(_file_size(s) for s in items)
            total_cards += len(items)
            total_size += size
            self.stdout.write(
                f"  {index}. {items[0].person.name}  —— {len(items)} 条记录，"
                f"图片合计 {_human_size(size)}"
            )
            for submission in items:
                hours = submission.work_hours.count()
                self.stdout.write(
                    f"        id={submission.pk}  项目：{submission.project}"
                    f"  提交：{submission.created_at:%Y-%m-%d %H:%M}"
                    f"  图片：{_human_size(_file_size(submission))}"
                    f"  工时记录：{hours} 条"
                )

        target_ids = [s.pk for items in groups.values() for s in items]
        hours_count = WorkHour.objects.filter(submission_id__in=target_ids).count()

        self.stdout.write("")
        self.stdout.write("-" * 78)
        self.stdout.write(
            f"合计：{len(groups)} 人 / {total_cards} 条工卡记录 / "
            f"图片 {_human_size(total_size)} / 关联的工时记录 {hours_count} 条"
        )
        self.stdout.write(
            "删除会连同工卡图片文件、以及挂在这些工卡上的工时记录一起删掉，不可恢复。"
        )
        self.stdout.write("-" * 78)

        if not options["delete"]:
            self.stdout.write("")
            self.stdout.write(
                self.style.SUCCESS(
                    "以上是预览，未改动任何数据。确认无误后加 --delete 执行删除"
                )
            )
            return

        # ---------- 二次确认 ----------
        if not options["yes"]:
            self.stdout.write("")
            answer = input(
                f"确认删除这 {total_cards} 条记录？请输入 DELETE（直接回车取消）："
            ).strip()
            if answer != "DELETE":
                self.stdout.write(self.style.SUCCESS("已取消，未删除任何数据"))
                return

        # ---------- 真正删除 ----------
        deleted_cards = 0
        deleted_files = 0
        failed_files = 0
        submissions = list(Submission.objects.filter(pk__in=target_ids))
        with transaction.atomic():
            for submission in submissions:
                file_name = submission.card_image.name if submission.card_image else ""
                submission.delete()
                deleted_cards += 1
                if file_name:
                    try:
                        default_storage.delete(file_name)
                        deleted_files += 1
                    except Exception as exc:   # 文件本来就不在了也不影响数据库清理
                        failed_files += 1
                        self.stdout.write(f"  图片文件删除失败 {file_name}：{exc}")

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"已删除 {deleted_cards} 条工卡记录、{hours_count} 条工时记录，"
                f"清理图片文件 {deleted_files} 个"
                + (f"（{failed_files} 个文件删除失败）" if failed_files else "")
            )
        )
        self.stdout.write(
            f"剩余：提交记录 {Submission.objects.count()} 条 / "
            f"姓名库 {Name.objects.count()} 人 / 工时记录 {WorkHour.objects.count()} 条"
        )
