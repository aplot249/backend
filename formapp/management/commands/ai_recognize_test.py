"""用一张真实工卡测试 AI 识别（配置好 AI_VISION_* 环境变量后先跑这个验证）。

用法：
    python manage.py ai_recognize_test 30                  # 识别工卡 id=30
    python manage.py ai_recognize_test 30 --month 2026-10  # 指定考勤周期
    python manage.py ai_recognize_test latest              # 识别最新一张有图的工卡

只输出识别结果，**不写数据库**。
"""
import os

from django.core.management.base import BaseCommand, CommandError

from formapp.ai_recognize import AIConfigError, AIResponseError, recognize_hours
from formapp.models import Submission
from formapp.views import _payroll_month, _period_range


class Command(BaseCommand):
    help = "用一张真实工卡测试 AI 识别（不写数据库）"

    def add_arguments(self, parser):
        parser.add_argument("card", help="工卡 id 或 latest（最新一张有图的工卡）")
        parser.add_argument("--month", default="", help="考勤周期 YYYY-MM，默认用这张卡的提交月份")

    def handle(self, *args, **options):
        card = options["card"].strip().lower()

        if card == "latest":
            submission = (
                Submission.objects.exclude(card_image="")
                .select_related("person")
                .order_by("-id")
                .first()
            )
            if submission is None:
                raise CommandError("库里没有带图片的工卡")
        else:
            if not card.isdigit():
                raise CommandError("card 要填工卡 id 或 latest")
            try:
                submission = Submission.objects.select_related("person").get(pk=int(card))
            except Submission.DoesNotExist:
                raise CommandError(f"工卡 id={card} 不存在")

        month = (options["month"] or "").strip() or _payroll_month(submission.created_at)
        start, end = _period_range(month)

        self.stdout.write("=" * 66)
        self.stdout.write(
            f"工卡 id={submission.pk}  {submission.person.name}  项目：{submission.project[:24]}"
        )
        self.stdout.write(f"图片：{submission.card_image.name}")
        self.stdout.write(f"考勤周期：{start} ~ {end}（--month {month}）")
        self.stdout.write(f"模型：{os.getenv('AI_VISION_MODEL', '（settings 默认）')}")
        self.stdout.write("=" * 66)
        self.stdout.write("识别中（要等几秒到几十秒）...")

        try:
            result = recognize_hours(submission.card_image, start, end)
        except AIConfigError as exc:
            raise CommandError(str(exc))
        except AIResponseError as exc:
            raise CommandError(str(exc))

        self.stdout.write("")
        if result["entries"]:
            self.stdout.write(self.style.SUCCESS(f"识别出 {len(result['entries'])} 天："))
            for work_date in sorted(result["entries"]):
                self.stdout.write(f"  {work_date}  {result['entries'][work_date]:>6} h")
            total = sum(result["entries"].values())
            self.stdout.write(f"  合计 {total:g} h / {len(result['entries'])} 天")
        else:
            self.stdout.write(self.style.WARNING("没有识别出有效数据"))

        if result["ignored"]:
            self.stdout.write("")
            self.stdout.write("已忽略：")
            for raw, reason in result["ignored"]:
                self.stdout.write(f"  {raw}  （{reason}）")

        if result["note"]:
            self.stdout.write("")
            self.stdout.write(f"AI 备注：{result['note']}")

        self.stdout.write("")
        self.stdout.write("结果未写入数据库；整理页弹窗里点「AI 识别选区」核对后保存")
