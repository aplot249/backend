import io
import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.files.storage import default_storage
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from openpyxl import load_workbook
from openpyxl.styles import Border, Side
from PIL import Image, UnidentifiedImageError

from django.db.models import Sum

from .models import Name, Project, Submission, WorkHour

# 工卡图片限制
MAX_CARD_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
# 单个自然日内允许录入的最大小时数
MAX_WORK_HOURS_PER_DAY = Decimal("24")
ALLOWED_CARD_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 工时导出：样表模板文件名（后端根目录 / 上一级项目根目录）
EXPORT_TEMPLATE_FILE = "样表模板.xlsx"
# 样表列位置：第 1 行为标题（合并单元格），第 2 行为表头，第 3 行起为数据
EXPORT_FIRST_DATA_ROW = 3
EXPORT_COL_NO = 1            # A 序号
EXPORT_COL_NAME = 2          # B 姓名/Name
EXPORT_COL_SITE = 3          # C SITE LOCATION
EXPORT_COL_EMPLOYEE = 4      # D 工种/ Employee.No
EXPORT_COL_PASSPORT = 5      # E 备注（护照号码/Passport number）
EXPORT_COL_RATE = 6          # F 单价/H
EXPORT_COL_DAY_START = 7     # G~AK：1~31，第 1 列对应周期起始日（21 号）
EXPORT_COL_TOTAL = 38        # AL Total Hour
EXPORT_COL_AMOUNT = 39       # AM 金额/RM


def form_view(request):
    """渲染表单页面。"""
    return render(request, "form.html")


@require_GET
def search_name(request):
    """按关键字模糊搜索姓名，返回匹配结果。"""
    keyword = request.GET.get("q", "").strip()
    queryset = Name.objects.all()
    if keyword:
        queryset = queryset.filter(name__icontains=keyword)
    results = list(queryset.values_list("name", flat=True)[:20])
    return JsonResponse({"results": results})


@require_GET
def search_project(request):
    """按关键字模糊搜索项目，返回匹配结果。"""
    keyword = request.GET.get("q", "").strip()
    queryset = Project.objects.all()
    if keyword:
        queryset = queryset.filter(name__icontains=keyword)
    results = list(queryset.values_list("name", flat=True)[:20])
    return JsonResponse({"results": results})


@require_GET
def list_project(request):
    """返回全部项目，供表单页面直接选择。"""
    results = list(Project.objects.values_list("name", flat=True))
    return JsonResponse({"results": results})


def _parse_payload(request):
    """兼容 JSON 与 multipart/form-data，返回 (name, project, remark, card_image)。"""
    content_type = (request.content_type or "").lower()

    if content_type.startswith("multipart/form-data") or content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        name = (request.POST.get("name") or "").strip()
        project = (request.POST.get("project") or "").strip()
        remark = (request.POST.get("remark") or "").strip()
        card_image = request.FILES.get("card_image")
        return name, project, remark, card_image

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        raise ValueError("Invalid request data")
    if not isinstance(data, dict):
        raise ValueError("Invalid request data")

    name = (data.get("name") or "").strip()
    project = (data.get("project") or "").strip()
    remark = (data.get("remark") or "").strip()
    return name, project, remark, None


def _validate_card_image(card_image):
    """校验工卡图片，返回错误信息；校验通过返回 None。"""
    if card_image is None:
        return "Work card image is required"

    if card_image.size > MAX_CARD_IMAGE_SIZE:
        return "Work card image must be smaller than 10MB"

    suffix = Path(card_image.name or "").suffix.lower()
    if suffix not in ALLOWED_CARD_IMAGE_SUFFIXES:
        return "Only jpg / png / webp / bmp / gif images are allowed"

    if card_image.content_type and not card_image.content_type.startswith("image/"):
        return "Only image files are allowed"

    # 进一步确认文件内容确实是图片
    try:
        raw = card_image.read()
        with Image.open(io.BytesIO(raw)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        return "The uploaded file is not a valid image"
    finally:
        try:
            card_image.seek(0)
        except (AttributeError, ValueError):
            pass

    return None


@csrf_exempt
@require_POST
def submit_form(request):
    """接收并保存表单提交（姓名 + 项目 + 工卡图片，工作内容选填）。"""
    try:
        name, project, remark, card_image = _parse_payload(request)
    except ValueError as exc:
        return JsonResponse({"ok": False, "message": str(exc)}, status=400)

    if not name or not project:
        return JsonResponse(
            {"ok": False, "message": "Name and project cannot be empty"}, status=400
        )

    error = _validate_card_image(card_image)
    if error:
        return JsonResponse({"ok": False, "message": error}, status=400)

    Submission.objects.create(
        name=name, project=project, remark=remark, card_image=card_image
    )
    return JsonResponse({"ok": True, "message": "Submitted successfully"})


@staff_member_required
@require_POST
def update_card_image(request, submission_id):
    """保存裁剪 / 旋转后的工卡图片，替换原来的图片（需后台登录）。"""
    submission = get_object_or_404(Submission, pk=submission_id)
    card_image = request.FILES.get("card_image")

    error = _validate_card_image(card_image)
    if error:
        return JsonResponse({"ok": False, "message": error}, status=400)

    old_name = submission.card_image.name if submission.card_image else ""
    submission.card_image = card_image
    submission.save(update_fields=["card_image"])

    # 新文件使用新的随机文件名，旧文件已无引用，删除以释放空间
    if old_name and old_name != submission.card_image.name:
        try:
            default_storage.delete(old_name)
        except Exception:
            pass

    return JsonResponse(
        {
            "ok": True,
            "message": "工卡图片已更新",
            "url": submission.card_image.url,
        }
    )


@staff_member_required
@require_GET
def work_hours(request, submission_id):
    """获取某条提交记录（工卡）已录入的工天数据。"""
    submission = get_object_or_404(Submission, pk=submission_id)
    results = {
        row.date.isoformat(): float(row.hours)
        for row in submission.work_hours.all()
    }
    return JsonResponse({"ok": True, "results": results})


@staff_member_required
@require_POST
def save_work_hours(request, submission_id):
    """保存工天数据：按「提交记录 + 日期」更新，已存在则覆盖，没有工作的天记为 0。"""
    submission = get_object_or_404(Submission, pk=submission_id)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "message": "Invalid request data"}, status=400)

    entries = data.get("entries") or {}
    if not isinstance(entries, dict):
        return JsonResponse({"ok": False, "message": "Invalid entries data"}, status=400)

    saved = []
    total = Decimal("0")
    for raw_date, raw_hours in entries.items():
        try:
            work_date = date.fromisoformat(str(raw_date))
        except ValueError:
            continue

        try:
            hours = Decimal(str(raw_hours if raw_hours not in (None, "") else 0))
        except (InvalidOperation, ValueError):
            return JsonResponse({"ok": False, "message": "小时数格式不正确"}, status=400)

        if hours < 0:
            return JsonResponse({"ok": False, "message": "小时数不能为负数"}, status=400)
        if hours > MAX_WORK_HOURS_PER_DAY:
            return JsonResponse(
                {"ok": False, "message": "单日小时数不能超过 24"}, status=400
            )

        WorkHour.objects.update_or_create(
            submission=submission,
            date=work_date,
            defaults={
                "name": submission.name,
                "project": submission.project,
                "hours": hours,
            },
        )
        saved.append((work_date, hours))
        total += hours

    return JsonResponse(
        {
            "ok": True,
            "message": "工天数据已保存",
            "count": len(saved),
            "total_hours": float(total),
            "worked_days": sum(1 for _, hours in saved if hours > 0),
        }
    )


def _format_hours(value):
    """工时显示：最多两位小数，去掉多余的 0（21.50 -> 21.5，21.00 -> 21）。"""
    if value is None:
        return "0"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _payroll_month(value):
    """工卡考勤周期为「上月 21 日 ~ 本月 20 日」，默认取提交所在的月份（YYYY-MM）。"""
    return f"{value.year:04d}-{value.month:02d}"


def _period_range(month_key):
    """考勤周期（上月 21 日 ~ 本月 20 日）的起止日期。"""
    year, month = int(month_key[:4]), int(month_key[5:7])
    end = date(year, month, 20)
    start_year, start_month = (year, month - 1) if month > 1 else (year - 1, 12)
    return date(start_year, start_month, 21), end


def _period_key(day):
    """某一天所属的考勤周期键：21 号及以后归入下个月周期。"""
    if day.day >= 21:
        year, month = (day.year, day.month + 1) if day.month < 12 else (day.year + 1, 1)
        return f"{year:04d}-{month:02d}"
    return f"{day.year:04d}-{day.month:02d}"


def _export_periods():
    """有工时记录的考勤周期（倒序）+ 当前周期，供导出下拉选择。"""
    keys = {_period_key(day) for day in WorkHour.objects.dates("date", "day")}
    keys.add(_period_key(date.today()))
    periods = []
    for key in sorted(keys, reverse=True):
        start, end = _period_range(key)
        periods.append(
            {"key": key, "label": f"{key}（{start:%Y-%m-%d} ~ {end:%Y-%m-%d}）"}
        )
    return periods


@staff_member_required
@ensure_csrf_cookie
def summary_view(request):
    """数据整理页：以姓名库为基准，按「人 + 项目」分组展示提交情况（最新提交置顶）。"""
    grouped = {}
    for submission in Submission.objects.order_by("-created_at"):
        submission.period_month = _payroll_month(submission.created_at)
        grouped.setdefault(submission.name, []).append(submission)

    # 每个工人的累计工时（跨项目相加）
    hours_map = {
        item["name"]: item["total"] or Decimal("0")
        for item in WorkHour.objects.values("name").annotate(total=Sum("hours"))
    }

    rows = []
    for person in Name.objects.all().only("name", "employee_no", "passport_no"):
        submissions = grouped.get(person.name, [])

        # 同一个人在不同项目下的提交分开成列，每个项目内按提交时间倒序
        projects = {}
        for submission in submissions:
            projects.setdefault(submission.project or "（未填项目）", []).append(
                submission
            )

        blocks = [
            {
                "project": project,
                "count": len(items),
                "submissions": items,  # 第一条为该项目下的最新提交
                "latest": items[0],
            }
            for project, items in projects.items()
        ]
        # 项目列按该项目最近一次提交时间倒序排列
        blocks.sort(key=lambda block: block["latest"].created_at, reverse=True)

        rows.append(
            {
                "name": person.name,
                "employee_no": person.employee_no,
                "passport_no": person.passport_no,
                "total_hours": hours_map.get(person.name, Decimal("0")),
                "total_hours_label": _format_hours(hours_map.get(person.name, Decimal("0"))),
                "count": len(submissions),
                "projects": blocks,
                "project_count": len(blocks),
                "latest": submissions[0] if submissions else None,
            }
        )

    total = len(rows)
    submitted_count = sum(1 for row in rows if row["count"])
    context = {
        "rows": rows,
        "total": total,
        "submitted_count": submitted_count,
        "missing_count": total - submitted_count,
        "duplicate_count": sum(1 for row in rows if row["count"] > 1),
        "multi_project_count": sum(1 for row in rows if row["project_count"] > 1),
        "total_hours": sum(row["total_hours"] for row in rows),
        "total_hours_label": _format_hours(sum(row["total_hours"] for row in rows)),
        "periods": _export_periods(),
        "default_period": _default_period_key(),
    }
    return render(request, "summary.html", context)


def _default_period_key():
    """默认导出周期：最近一个有工时记录的周期；没有记录时为当前周期。"""
    keys = sorted({_period_key(day) for day in WorkHour.objects.dates("date", "day")})
    return keys[-1] if keys else _period_key(date.today())


def _find_export_template():
    """样表模板位置：后端根目录，或上一级项目根目录。"""
    for base in (settings.BASE_DIR, settings.BASE_DIR.parent):
        candidate = base / EXPORT_TEMPLATE_FILE
        if candidate.exists():
            return candidate
    return None


def _collect_export_rows(start, end):
    """汇总周期内每个工人的数据：同一天的多个项目工时相加。"""
    workers = {}
    queryset = WorkHour.objects.filter(date__gte=start, date__lte=end).values_list(
        "name", "date", "hours", "project"
    )
    for name, work_date, hours, project in queryset:
        hours = hours or Decimal("0")
        item = workers.setdefault(
            name, {"hours": {}, "projects": [], "total": Decimal("0")}
        )
        item["hours"][work_date] = item["hours"].get(work_date, Decimal("0")) + hours
        item["total"] += hours
        if project and project not in item["projects"]:
            item["projects"].append(project)

    profiles = {
        person.name: person for person in Name.objects.filter(name__in=workers)
    }

    rows = []
    for name in sorted(workers):
        item = workers[name]
        person = profiles.get(name)
        rate = person.hourly_rate if person else Decimal("0")
        rows.append(
            {
                "name": name,
                "site": " / ".join(item["projects"]),
                "employee_no": person.employee_no if person else "",
                "passport_no": person.passport_no if person else "",
                "rate": rate,
                "hours": item["hours"],  # {日期: 当天各项目工时之和}
                "total": item["total"],
                "amount": (item["total"] * rate).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                ),
            }
        )
    return rows


@staff_member_required
@require_GET
def export_work_hours_excel(request):
    """按样表模板导出某考勤周期的工时统计 Excel。"""
    month_key = (request.GET.get("month") or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", month_key):
        month_key = _default_period_key()
    start, end = _period_range(month_key)

    template_path = _find_export_template()
    if not template_path:
        return HttpResponse(
            f"未找到样表模板 {EXPORT_TEMPLATE_FILE}（请放在后端根目录）", status=500
        )

    rows = _collect_export_rows(start, end)

    workbook = load_workbook(template_path)
    sheet = workbook.active

    # 标题行（模板 A1 为合并单元格）
    sheet.cell(row=1, column=1).value = (
        f"MONTH OF ATTENDANCE ({end:%b} {end:%Y})".upper()
    )

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for offset, row_data in enumerate(rows):
        row_index = EXPORT_FIRST_DATA_ROW + offset
        values = {
            EXPORT_COL_NO: offset + 1,
            EXPORT_COL_NAME: row_data["name"],
            EXPORT_COL_SITE: row_data["site"],
            EXPORT_COL_EMPLOYEE: row_data["employee_no"],
            EXPORT_COL_PASSPORT: row_data["passport_no"],
            EXPORT_COL_RATE: row_data["rate"],
            EXPORT_COL_TOTAL: row_data["total"],
            EXPORT_COL_AMOUNT: row_data["amount"],
        }
        for column, value in values.items():
            cell = sheet.cell(row=row_index, column=column)
            cell.value = value
            cell.border = border

        # 第 1~31 列依次对应周期起始日（21 号）、22 号……
        for column in range(EXPORT_COL_DAY_START, EXPORT_COL_DAY_START + 31):
            index = column - EXPORT_COL_DAY_START + 1
            work_date = start + timedelta(days=index - 1)
            if work_date > end:
                continue
            cell = sheet.cell(row=row_index, column=column)
            cell.value = row_data["hours"].get(work_date, 0)
            cell.border = border

        for column in (EXPORT_COL_RATE, EXPORT_COL_TOTAL, EXPORT_COL_AMOUNT):
            sheet.cell(row=row_index, column=column).number_format = "0.00"

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)

    filename = f"工时统计_{month_key}.xlsx"
    response = HttpResponse(
        output.getvalue(),
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    response["Content-Disposition"] = (
        f'attachment; filename="work_hours_{month_key}.xlsx"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return response
