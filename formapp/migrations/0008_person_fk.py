"""工卡 / 工时直接关联姓名库（外键 `person`），不再各存一份姓名文本。

步骤：
  1. 给 `Submission` / `WorkHour` 加可空的 `person` 外键
  2. 按**原来存的姓名文本**归一化匹配姓名库，回填 `person`
     （回填不上的先留空，最后一步会报错拦下，不会静默丢数据）
  3. 确认没有空值后把 `person` 收成**非空**（从此姓名不可能对不上库）
  4. 唯一约束从「姓名+项目+日期」换成「人+项目+日期」
  5. 删掉 `name` 文本字段（姓名一律从姓名库实时取）

反向迁移（rollback）会把 `name` 填回空字符串，姓名文本会丢失，请先备份数据库。
"""
import re

from django.db import migrations, models
import django.db.models.deletion


def _norm(value):
    """与 `formapp.models.normalize_name` 同一套规则（迁移文件里自带一份，避免以后改坏）。"""
    text = re.sub(r"[.:,;_\-/]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().upper()


def backfill_person(apps, schema_editor):
    Name = apps.get_model("formapp", "Name")
    Submission = apps.get_model("formapp", "Submission")
    WorkHour = apps.get_model("formapp", "WorkHour")

    # 姓名库按归一化姓名建索引（库里若有归一化后同名的，就不回填，交给人工确认）
    by_key = {}
    for person in Name.objects.all():
        by_key.setdefault(_norm(person.name), []).append(person)

    def match(text):
        hits = by_key.get(_norm(text)) or []
        return hits[0] if len(hits) == 1 else None

    done = skipped = 0
    for submission in Submission.objects.all().only("pk", "name"):
        person = match(submission.name)
        if person is None:
            skipped += 1
            print(f"  ！工卡 id={submission.pk} 姓名 {submission.name!r} 匹配不到姓名库，留空")
            continue
        submission.person_id = person.pk
        submission.save(update_fields=["person"])
        done += 1
    print(f"  工卡回填：{done} 条成功，{skipped} 条留空")

    # 工时优先跟所属工卡同一个人（保证一致），工卡没人的再按自己的姓名文本匹配
    submission_person = dict(Submission.objects.values_list("pk", "person_id"))
    done = skipped = 0
    for row in WorkHour.objects.all().only("pk", "name", "submission_id"):
        person_id = submission_person.get(row.submission_id)
        if person_id is None:
            person = match(row.name)
            person_id = person.pk if person else None
        if person_id is None:
            skipped += 1
            print(f"  ！工时 id={row.pk} 姓名 {row.name!r} 匹配不到姓名库，留空")
            continue
        row.person_id = person_id
        row.save(update_fields=["person"])
        done += 1
    print(f"  工时回填：{done} 条成功，{skipped} 条留空")


def check_no_null(apps, schema_editor):
    """收成非空之前先确认：还有空的就把 id 打出来并中止迁移（不会静默丢数据）。"""
    for model_name in ("Submission", "WorkHour"):
        model = apps.get_model("formapp", model_name)
        blank = list(
            model.objects.filter(person__isnull=True).values_list("pk", flat=True)[:20]
        )
        if blank:
            raise RuntimeError(
                f"{model_name} 有 {len(blank)} 条记录没匹配到姓名库（id={blank}），"
                f"已中止。请先在后台「姓名管理」补齐这些姓名后重新 migrate。"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("formapp", "0007_systemsetting"),
    ]

    operations = [
        migrations.AddField(
            model_name="submission",
            name="person",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submissions",
                to="formapp.name",
                verbose_name="姓名库",
                help_text="这张工卡属于姓名库里的哪个人（改姓名库的姓名，这里自动跟着变）",
            ),
        ),
        migrations.AddField(
            model_name="workhour",
            name="person",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_hours",
                to="formapp.name",
                verbose_name="姓名库",
                help_text="这条工时属于姓名库里的哪个人（默认跟着所属工卡走）",
            ),
        ),
        migrations.RunPython(backfill_person, migrations.RunPython.noop),
        migrations.RunPython(check_no_null, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="submission",
            name="person",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="submissions",
                to="formapp.name",
                verbose_name="姓名库",
                help_text="这张工卡属于姓名库里的哪个人（改姓名库的姓名，这里自动跟着变）",
            ),
        ),
        migrations.AlterField(
            model_name="workhour",
            name="person",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="work_hours",
                to="formapp.name",
                verbose_name="姓名库",
                help_text="这条工时属于姓名库里的哪个人（默认跟着所属工卡走）",
            ),
        ),
        migrations.AlterUniqueTogether(
            name="workhour",
            unique_together={("person", "project", "date")},
        ),
        migrations.RemoveField(model_name="workhour", name="name"),
        migrations.RemoveField(model_name="submission", name="name"),
    ]
