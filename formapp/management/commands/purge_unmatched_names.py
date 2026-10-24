"""清理「姓名在姓名库里找不到」的工卡记录。

这些记录一直躺在数据库里，但整理页看不到（也就没法裁剪 / 录工时）、工时统计不到、
导出的 PDF 也会跳过它们，留着只会占空间。

**删除前先看清楚再决定**：默认只列出不删；加 `--delete` 会再要求手输 DELETE 才真删。
会一并把工卡图片文件和挂在它们上面的工时记录删掉（不可恢复）。

用法：
    python manage.py purge_unmatched_names                   # 只列出，不删（默认）
    python manage.py purge_unmatched_names --delete           # 列出后要手输 DELETE 才删
    python manage.py purge_unmatched_names --delete --yes     # 跳过确认（慎用）
    python manage.py purge_unmatched_names --name SHILON      # 只看姓名含该关键字的
    python manage.py purge_unmatched_names --skip-vacation    # 不处理「在休假」的记录
    python manage.py purge_unmatched_names --include-fixable  # 连「已能匹配到库」的也一起删
"""
import difflib

from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import transaction

from formapp.models import Name, Submission, WorkHour
from formapp.views import VACATION_PROJECT, _match_library_name, _normalize_name


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
    help = "列出 / 删除「姓名不在姓名库里」的工卡记录（默认只预览）"

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
        parser.add_argument(
            "--include-fixable",
            action="store_true",
            help="连「新规则下已能匹配到库（只是词序 / 标点不同）」的记录也一起删",
        )

    def handle(self, *args, **options):
        lib_names = list(Name.objects.values_list("name", flat=True))
        lib_keys = {_normalize_name(name): name for name in lib_names}
        keyword = (options["name"] or "").strip().upper()

        # ---------- 1. 找出姓名匹配不到库的工卡，按人分组 ----------
        groups = {}
        for submission in Submission.objects.all().order_by("name", "created_at"):
            key = _normalize_name(submission.name)
            if key in lib_keys:
                continue
            if options["skip_vacation"] and submission.project == VACATION_PROJECT:
                continue
            if keyword and keyword not in key:
                continue
            groups.setdefault(key, []).append(submission)

        if not groups:
            self.stdout.write(
                self.style.SUCCESS("没有「姓名不在姓名库里」的工卡记录，无需处理")
            )
            return

        # 分成两类：现在就能匹配到库的（词序 / 标点差异）与真匹配不上的
        fixable, unmatched = {}, {}
        for key, items in groups.items():
            target = _match_library_name(items[0].name)
            if target:
                fixable[key] = (items, target)
            else:
                unmatched[key] = items

        self.stdout.write("")
        self.stdout.write("=" * 78)
        self.stdout.write(
            "姓名不在姓名库的工卡：整理页 / 工时统计 / PDF 导出都看不到它们"
        )
        self.stdout.write("=" * 78)

        # ---------- 2. 先列出「建议改名而不是删除」的 ----------
        if fixable:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    f"【A 组】写法不同但能自动匹配到库：{len(fixable)} 人"
                    "（建议到后台「姓名管理」把库里的姓名改成这个写法，别删）"
                )
            )
            for index, (key, (items, target)) in enumerate(
                sorted(fixable.items(), key=lambda kv: kv[1][0][0].name), 1
            ):
                self.stdout.write(
                    f"  A{index}. {items[0].name}  ({len(items)} 条)"
                    f"  ->  库里标准写法：{target}"
                )
                self.stdout.write(f"       项目：{self._projects(items)}")
            if not options["include_fixable"]:
                self.stdout.write(
                    "      （默认不删这一组；确认要删请加 --include-fixable）"
                )

        # ---------- 3. 列出真正会被删的 ----------
        targets = dict(unmatched)
        if options["include_fixable"]:
            targets.update({key: items for key, (items, _t) in fixable.items()})

        self.stdout.write("")
        self.stdout.write(
            self.style.ERROR(f"【B 组】真匹配不上、可以删除：{len(targets)} 人")
        )
        total_cards = 0
        total_size = 0
        for index, key in enumerate(sorted(targets, key=lambda k: targets[k][0].name), 1):
            items = targets[key]
            size = sum(_file_size(s) for s in items)
            total_cards += len(items)
            total_size += size
            sample = items[0]
            near = difflib.get_close_matches(key, list(lib_keys), n=1, cutoff=0.7)
            suggest = (
                f"库里最接近：{lib_keys[near[0]]}（也可能是不同的人）"
                if near
                else "库里没有相近的姓名"
            )
            self.stdout.write(
                self.style.ERROR(
                    f"  B{index}. {sample.name}  —— {len(items)} 条记录，"
                    f"图片合计 {_human_size(size)}"
                )
            )
            for submission in items:
                hours = submission.work_hours.count()
                self.stdout.write(
                    f"        id={submission.pk}  项目：{submission.project}"
                    f"  提交：{submission.created_at:%Y-%m-%d %H:%M}"
                    f"  图片：{_human_size(_file_size(submission))}"
                    f"  工时记录：{hours} 条"
                )
            self.stdout.write(f"       {suggest}")

        target_ids = [s.pk for items in targets.values() for s in items]
        hours_count = WorkHour.objects.filter(submission_id__in=target_ids).count()

        self.stdout.write("")
        self.stdout.write("-" * 78)
        self.stdout.write(
            f"合计：{len(targets)} 人 / {total_cards} 条工卡记录 / "
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

        if not target_ids:
            self.stdout.write(self.style.SUCCESS("没有需要删除的记录"))
            return

        # ---------- 4. 二次确认 ----------
        if not options["yes"]:
            self.stdout.write("")
            answer = input(
                f"确认删除这 {total_cards} 条记录？请输入 DELETE（直接回车取消）："
            ).strip()
            if answer != "DELETE":
                self.stdout.write(self.style.SUCCESS("已取消，未删除任何数据"))
                return

        # ---------- 5. 真正删除 ----------
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

    def _projects(self, items):
        """把一个人名下的项目名拼成一行（太长就截断）。"""
        text = "、".join(
            dict.fromkeys(s.project or "（未填项目）" for s in items)
        )
        return text if len(text) <= 60 else text[:57] + "..."
