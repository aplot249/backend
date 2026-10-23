import io
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
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

from django.db.models import Count, Sum

from .models import Name, Project, Submission, SystemSetting, WorkHour

# 工卡图片限制
MAX_CARD_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
# 单个自然日内允许录入的最大小时数
MAX_WORK_HOURS_PER_DAY = Decimal("24")
ALLOWED_CARD_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# 工时导出：样表模板文件名（后端根目录 / 上一级项目根目录）
EXPORT_TEMPLATE_FILE = "样表模板.xlsx"
# 转为休假时使用的项目名
VACATION_PROJECT = "在休假"
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

# 考勤工卡 PDF 导出
PDF_FONT_NAME = "STSong-Light"   # reportlab 自带的中日韩字体，无需外挂字体文件
PDF_IMAGE_MAX_WIDTH = 1100       # 工卡图片像素宽度上限，控制 PDF 体积与生成耗时
PDF_IMAGE_JPEG_QUALITY = 62


def _is_backend_admin(user):
    """后台管理员（能进 /admin 的人）：staff 或超级用户，且已登录。"""
    return bool(
        user and user.is_authenticated and (user.is_staff or user.is_superuser)
    )


def form_view(request):
    """工卡填报表单页（`/`）。

    两个页面路由各自独立：`/` 是填报页、`/check/` 是查询页。
    后台「系统开关」关闭填报时，`/` 仍然留在填报页上，只是把表单换成
    「当前填报阶段已结束，进入查询系统」的友好提示 + 去查询页的入口。

    例外：**后台管理员登录着的时候不受开关限制**，照样看到完整填报表单
    （方便自己补录 / 测试），页面顶部会提示「工人端当前已关闭填报」。
    """
    setting_on = SystemSetting.get_setting().form_enabled
    form_enabled = setting_on or _is_backend_admin(request.user)
    return render(
        request,
        "form.html",
        {
            "form_enabled": form_enabled,
            # 开关关着、只是因为管理员身份才放行
            "admin_bypass": form_enabled and not setting_on,
        },
    )


def check_view(request):
    """工时查询页：工人输入「姓名 + 工号/护照号」核对身份后查询自己的工时（只读）。

    /check/ 始终可访问；首页 / 是否展示它由「系统开关」决定。
    """
    return render(
        request, "check.html", {"form_enabled": SystemSetting.get_setting().form_enabled}
    )


def _normalize_code(value):
    """工号 / 护照号匹配用：去掉所有空白并统一大写。"""
    return re.sub(r"\s+", "", str(value or "")).upper()


def _mask_code(value):
    """护照号脱敏展示：只保留前 2 位与后 2 位。"""
    text = (value or "").strip()
    if len(text) <= 4:
        return text
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def _person_codes(person):
    return (
        _normalize_code(person.employee_no),
        _normalize_code(person.passport_no),
    )


def _find_persons(keyword, limit=20, include_code=False):
    """按姓名找人，返回 (精确匹配, 模糊匹配)。

    `include_code=True` 时同时按工号 / 护照号匹配（大小写、空格都不敏感：
    `wx0339` / `WX 0339` / `WX0339 ` 都能匹配到工号 WX0339）。
    """
    name_key = _normalize_name(keyword)
    code_key = _normalize_code(keyword) if include_code else ""
    if not name_key and not code_key:
        return [], []

    exact, fuzzy = [], []
    for person in Name.objects.all().only("name", "employee_no", "passport_no"):
        codes = _person_codes(person)
        if _normalize_name(person.name) == name_key or (
            code_key and code_key in codes
        ):
            exact.append(person)
        elif (name_key and name_key in _normalize_name(person.name)) or (
            code_key and any(code_key in code for code in codes if code)
        ):
            fuzzy.append(person)
        if len(exact) + len(fuzzy) >= limit * 4:
            break
    return exact[:limit], fuzzy[:limit]


def _person_payload(person):
    return {
        "name": person.name,
        "employee_no": person.employee_no,
        "passport_no": _mask_code(person.passport_no),
    }


@require_GET
def check_search(request):
    """查询页的姓名联想（大小写、空格不敏感）。"""
    keyword = (request.GET.get("q") or "").strip()
    if not keyword:
        return JsonResponse({"results": []})
    exact, fuzzy = _find_persons(keyword)
    return JsonResponse(
        {"results": [_person_payload(p) for p in (exact + fuzzy)]}
    )


@require_GET
def check_hours(request):
    """查询整理后的工时数据：**姓名 +（工号 / 护照号）** 两项都对上才返回结果。

    口径与整理页一致：只取每个项目「最新提交」的工卡上已录入的工时，
    历史提交的工卡不展示；只返回工时，不含任何可编辑 / 删除的入口。
    """
    name_raw = (request.GET.get("name") or request.GET.get("q") or "").strip()
    code_raw = (request.GET.get("code") or "").strip()

    if not name_raw:
        return JsonResponse(
            {
                "ok": False,
                "field": "name",
                "message": "Please enter your name.",
            },
            status=400,
        )
    if not code_raw:
        return JsonResponse(
            {
                "ok": False,
                "field": "code",
                "message": "Please enter your employee No. or passport No.",
            },
            status=400,
        )

    # 1) 先按姓名找人
    exact, fuzzy = _find_persons(name_raw)
    candidates = exact or fuzzy

    if not candidates:
        # 姓名库里没有，看工卡提交里有没有这个人
        key = _normalize_name(name_raw)
        submitted = any(
            _normalize_name(person_name) == key
            for person_name, _ in _latest_submission_map()
        )
        if not submitted:
            return JsonResponse(
                {
                    "ok": True,
                    "found": False,
                    "field": "name",
                    "message": "No record found for this name. Please check the spelling "
                    "(it should be the same as on your work card).",
                }
            )
        return JsonResponse(
            {
                "ok": True,
                "found": False,
                "field": "code",
                "message": "We have no employee No. / passport No. on file for this name. "
                "Please contact the site office.",
            }
        )

    if len(candidates) > 1:
        return JsonResponse(
            {
                "ok": True,
                "found": False,
                "multiple": True,
                "field": "name",
                "candidates": [_person_payload(p) for p in candidates],
                "message": "Several people match this name. Please choose one below.",
            }
        )

    person = candidates[0]

    # 2) 再校验工号 / 护照号是否属于这个人（大小写、空格都不敏感）
    code_key = _normalize_code(code_raw)
    employee_key, passport_key = _person_codes(person)
    if not employee_key and not passport_key:
        return JsonResponse(
            {
                "ok": True,
                "found": False,
                "field": "code",
                "message": "We have no employee No. / passport No. on file for this name. "
                "Please contact the site office.",
            }
        )
    if code_key not in (employee_key, passport_key):
        return JsonResponse(
            {
                "ok": True,
                "found": False,
                "field": "code",
                "message": "This employee No. / passport No. does not match this name. "
                "Please check it and try again.",
            }
        )

    key = _normalize_name(person.name)
    matched_by = "passport" if code_key == passport_key else "employee_no"

    # 每个「人 + 项目」最新工卡，筛出这个人的
    pairs = [
        (project, submission_id)
        for (person_name, project), submission_id in _latest_submission_map().items()
        if _normalize_name(person_name) == key
    ]

    if not pairs:
        return JsonResponse(
            {
                "ok": True,
                "found": False,
                "field": "name",
                "message": "No work card has been submitted under this name yet.",
            }
        )

    submissions = {
        s.pk: s for s in Submission.objects.filter(pk__in=[sid for _, sid in pairs])
    }
    pairs = [pair for pair in pairs if pair[1] in submissions]
    # 按该项目最近一次提交时间倒序
    pairs.sort(key=lambda item: submissions[item[1]].created_at, reverse=True)

    hours_by_submission = {}
    for row in WorkHour.objects.filter(
        submission_id__in=[sid for _, sid in pairs]
    ).order_by("date"):
        if row.hours > 0:  # 只展示有工时的日期
            hours_by_submission.setdefault(row.submission_id, []).append(row)

    total = Decimal("0")
    projects = []
    for project, submission_id in pairs:
        submission = submissions.get(submission_id)
        rows = hours_by_submission.get(submission_id, [])
        sub_total = sum((row.hours for row in rows), Decimal("0"))
        total += sub_total
        projects.append(
            {
                "project": project,
                "is_vacation": project == VACATION_PROJECT,
                "submitted_at": submission.created_at.strftime("%Y-%m-%d %H:%M")
                if submission
                else "",
                "total_hours": float(sub_total),
                "total_hours_label": _format_hours(sub_total),
                "worked_days": len(rows),
                "days": [
                    {
                        "date": row.date.isoformat(),
                        "hours": float(row.hours),
                        "hours_label": _format_hours(row.hours),
                    }
                    for row in rows
                ],
            }
        )

    return JsonResponse(
        {
            "ok": True,
            "found": True,
            "name": person.name,
            "employee_no": person.employee_no,
            "passport_no": _mask_code(person.passport_no),
            # 说明这次是靠工号还是护照号核对通过的，方便工人确认查到的是自己
            "matched_by": matched_by,
            "project_count": len(projects),
            "total_hours": float(total),
            "total_hours_label": _format_hours(total),
            "projects": projects,
        }
    )


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
    """接收并保存表单提交（姓名 + 项目 + 工卡图片，工作内容选填）。

    后台「系统开关」关闭填报表单时，即使绕过页面直接调接口也会被拒绝；
    后台管理员登录着时同样不受限制（与表单页面保持一致）。
    """
    if not SystemSetting.get_setting().form_enabled and not _is_backend_admin(
        request.user
    ):
        return JsonResponse(
            {
                "ok": False,
                "message": "Work card submission is closed. "
                "Please use the work hours check page.",
            },
            status=403,
        )

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
    """保存工天数据：按「人 + 项目 + 日期」更新，已存在则覆盖，没有工作的天记为 0。

    仅允许在该「人 + 项目」最新提交的工卡上保存，历史工卡只读。
    """
    submission = get_object_or_404(Submission, pk=submission_id)

    if submission.pk not in _latest_submission_ids():
        return JsonResponse(
            {
                "ok": False,
                "message": "这是历史工卡，只有最新提交的工卡才能录入工天数据",
            },
            status=400,
        )

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
            name=submission.name,
            project=submission.project,
            date=work_date,
            defaults={"submission": submission, "hours": hours},
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


def _vacation_period():
    """休假记录所属周期：上月 21 日 ~ 本月 20 日。"""
    return _period_range(f"{date.today():%Y-%m}")


@staff_member_required
@require_POST
def set_vacation(request, name_id):
    """把某人转为休假：在该周期（上月 21 日 ~ 本月 20 日）内每天生成一条
    项目为「在休假」、工时为 0 的记录，供后台与导出核对。"""
    person = get_object_or_404(Name, pk=name_id)
    start, end = _vacation_period()

    submission = (
        Submission.objects.filter(name=person.name, project=VACATION_PROJECT)
        .order_by("-created_at")
        .first()
    )
    if submission is None:
        submission = Submission.objects.create(
            name=person.name, project=VACATION_PROJECT, remark="系统生成：转为休假"
        )

    created = 0
    updated = 0
    day = start
    while day <= end:
        _, is_created = WorkHour.objects.update_or_create(
            name=person.name,
            project=VACATION_PROJECT,
            date=day,
            defaults={"submission": submission, "hours": 0},
        )
        if is_created:
            created += 1
        else:
            updated += 1
        day += timedelta(days=1)

    return JsonResponse(
        {
            "ok": True,
            "message": f"已把 {person.name} 转为休假：{start:%Y-%m-%d} ~ {end:%Y-%m-%d} "
            f"共 {created + updated} 天记录（项目「{VACATION_PROJECT}」，工时 0）",
            "name": person.name,
            "project": VACATION_PROJECT,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "created": created,
            "updated": updated,
        }
    )


@staff_member_required
@require_GET
def person_total_hours(request, submission_id):
    """某张工卡所属工人的累计工时，口径与整理页一致（归一化姓名 + 只取最新工卡）。

    弹窗里保存工天后用它刷新页面上该工人的「总工时」，无需刷新整个页面。
    """
    submission = get_object_or_404(Submission, pk=submission_id)
    total = _person_hours_map().get(_normalize_name(submission.name), Decimal("0"))
    return JsonResponse(
        {
            "ok": True,
            "name": submission.name,
            "total_hours": float(total),
            "total_hours_label": _format_hours(total),
        }
    )


@staff_member_required
@require_POST
def delete_submission(request, submission_id):
    """删除某人在某项目的工卡记录（需后台登录）。

    同时删除：工卡图片文件、该工卡关联的所有工时记录（外键级联删除）。
    """
    submission = get_object_or_404(Submission, pk=submission_id)
    who = submission.name
    project = submission.project or "（未填项目）"
    hours_count = submission.work_hours.count()
    image_name = submission.card_image.name if submission.card_image else ""

    submission.delete()  # WorkHour 外键为 CASCADE，关联工时一并删除

    # Django 删除记录时不会删除文件，这里手动清理工卡图片
    deleted_image = False
    if image_name:
        try:
            default_storage.delete(image_name)
            deleted_image = True
        except Exception:
            pass

    return JsonResponse(
        {
            "ok": True,
            "message": f"已删除 {who} 在「{project}」的工卡记录"
            + (f"（含 {hours_count} 条工时）" if hours_count else ""),
            "deleted_hours": hours_count,
            "deleted_image": deleted_image,
        }
    )


def _pdf_register_font():
    """注册 PDF 用的中文字体（reportlab 自带 CID 字体，重复调用安全）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    if PDF_FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(PDF_FONT_NAME))
    return PDF_FONT_NAME


def _collect_card_pdf_rows(period_key=None):
    """按工人汇总考勤工卡：每人一条，含工号、项目、提交时间、工时与工卡图片。

    period_key 为 None 时不按考勤周期筛选（导出全部最新工卡）；
    否则只取「工卡提交时间」落在该周期（上月 21 日 ~ 本月 20 日）的工卡。
    """
    start, end = _period_range(period_key) if period_key else (None, None)

    latest = _latest_submission_map()
    submissions = {
        s.pk: s
        for s in Submission.objects.filter(pk__in=set(latest.values())).only(
            "pk", "name", "project", "card_image", "created_at"
        )
    }

    # 每张工卡上已登记的工时（只统计有工时的天数）
    hours_by_card = {}
    for item in (
        WorkHour.objects.filter(hours__gt=0)
        .values("submission_id")
        .annotate(total=Sum("hours"), days=Count("id"))
    ):
        hours_by_card[item["submission_id"]] = (
            item["total"] or Decimal("0"),
            item["days"],
        )

    cards_by_person = {}
    for (name, project), submission_id in latest.items():
        submission = submissions.get(submission_id)
        if submission is None:
            continue
        if period_key and _period_key(submission.created_at.date()) != period_key:
            continue
        cards_by_person.setdefault(_normalize_name(name), []).append(submission)

    # 休假人员：项目为「在休假」的工时记录
    vacation_queryset = WorkHour.objects.filter(project=VACATION_PROJECT)
    if period_key:
        vacation_queryset = vacation_queryset.filter(date__gte=start, date__lte=end)
    vacation_keys = {
        _normalize_name(name)
        for name in vacation_queryset.values_list("name", flat=True)
    }

    profiles = {
        _normalize_name(person.name): person
        for person in Name.objects.all().only("name", "employee_no", "passport_no")
    }

    rows = []
    for key in set(profiles) | set(cards_by_person) | vacation_keys:
        person = profiles.get(key)
        cards = sorted(
            cards_by_person.get(key, []),
            key=lambda item: item.created_at,
            reverse=True,
        )
        rows.append(
            {
                "name": person.name if person else (cards[0].name if cards else key),
                "employee_no": person.employee_no if person else "",
                "passport_no": person.passport_no if person else "",
                "cards": cards,
                "vacation": key in vacation_keys,
            }
        )
    rows.sort(key=lambda row: row["name"])
    return rows, hours_by_card


@staff_member_required
@require_GET
def export_cards_pdf(request):
    """把每个工人的考勤工卡导出成 PDF：按「姓名 → 工号 → 考勤图片」依次罗列。

    - 取数口径与整理页完全一致：每个「人 + 项目」取**最新提交**的那张工卡，
      一个人干多个项目就把多个项目的工卡**都列出来**（每张都出图）
    - **休假 / 未提交工卡的工人直接跳过**，不进 PDF（不再列「本周期无考勤工卡」的占位）
    - 每张工卡写明该记录的提交日期时间（含秒）：`最新工卡 2026-10-22 12:15:54 · 工时 … · 出勤 …`
    - 版面：左右两栏 × 上下两行，**一页 4 个人**；图片按格子高度自动等比缩放，
      一格最多占半页，放不进就自动压图，且保证一个人的内容不会被跨页拆开
    - 休假 / 未提交工卡的工人也会列出来，并写明「在休假，本周期无考勤工卡」
    - `?month=all`（默认）导出全部；`?month=YYYY-MM` 只导出该考勤周期提交的工卡
    """
    month = (request.GET.get("month") or "all").strip()
    period_key = None if month in ("", "all") else month
    start, end = _period_range(period_key) if period_key else (None, None)
    rows, hours_by_card = _collect_card_pdf_rows(period_key)

    # 与整理页口径一致：每个「人 + 项目」取最新提交的那张工卡，多个项目就都列出来；
    # 休假（只有「在休假」记录）或没提交过工卡的人直接跳过，不进 PDF
    rows = [
        row
        for row in rows
        if any(item.project != VACATION_PROJECT for item in row["cards"])
    ]

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image as ReportImage
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    font = _pdf_register_font()
    margin = 14 * mm
    content_width = A4[0] - margin * 2
    # 一页正文的可用高度（已扣掉上下边距和页脚留白）
    page_usable = A4[1] - margin - (margin + 6 * mm)
    # 版面：左右两栏 × 上下两行，一页放 4 个人（图片按格子高度自动缩放）
    column_gap = 8
    column_width = (content_width - column_gap) / 2
    cell_padding = 5
    inner_width = column_width - cell_padding * 2
    # 一格的高度上限 = 半页，两行正好一页；图片按这个高度等比缩放
    cell_max_height = page_usable / 2 - 6
    image_max_height = 300      # 单张工卡图片的高度上限（放不进半页时还会再压）

    title_style = ParagraphStyle(
        "title", fontName=font, fontSize=16, leading=21, spaceAfter=2
    )
    head_style = ParagraphStyle(
        "head", fontName=font, fontSize=9, leading=13,
        textColor=colors.HexColor("#666666"),
    )
    name_style = ParagraphStyle(
        "name", fontName=font, fontSize=11.5, leading=16,
        textColor=colors.HexColor("#222222"),
    )
    meta_style = ParagraphStyle(
        "meta", fontName=font, fontSize=8.5, leading=12,
        textColor=colors.HexColor("#666666"),
    )
    project_style = ParagraphStyle(
        "project", fontName=font, fontSize=10, leading=14, spaceBefore=3,
        textColor=colors.HexColor("#1f3d7a"),
    )
    note_style = ParagraphStyle(
        "note", fontName=font, fontSize=9, leading=13,
        textColor=colors.HexColor("#2f6fb2"),
    )

    period_label = (
        "全部（不按考勤周期筛选）"
        if period_key is None
        else f"{period_key}（{start:%Y-%m-%d} ~ {end:%Y-%m-%d}）"
    )
    card_count = sum(len(row["cards"]) for row in rows)

    story = [
        Paragraph("工人考勤工卡汇总", title_style),
        Paragraph(
            escape(
                f"考勤周期：{period_label}　生成时间：{datetime.now():%Y-%m-%d %H:%M}"
                f"　共 {len(rows)} 人 / {card_count} 张工卡"
                f"；口径与整理页一致（每人每个项目取最新提交的那张），"
                f"休假 / 未提交工卡的工人不列入"
            ),
            head_style,
        ),
        Spacer(1, 4),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:

        def measure_cell(cell):
            """估算一格内容的高度（用于保证一格不会被跨页拆开）。"""
            total = 0
            for flowable in cell:
                if isinstance(flowable, Spacer):
                    total += flowable.height
                    continue
                _w, h = flowable.wrap(inner_width, 10000)
                total += h + flowable.getSpaceBefore()
            return total + cell_padding * 2

        def build_worker_cell(index, row, card_items, image_cap):
            chips = []
            if row["employee_no"]:
                chips.append("工号 " + row["employee_no"])
            if row["passport_no"]:
                chips.append("护照 " + row["passport_no"])
            headline = f"{index}. {escape(row['name'])}"
            if chips:
                headline += (
                    f' <font color="#777777" size="8">{escape(" · ".join(chips))}</font>'
                )

            cell = [Paragraph(headline, name_style)]

            # 每个项目一张：都是该项目最新提交的那张工卡
            for submission, total, days, image in card_items:
                cell.append(
                    Paragraph(
                        escape(f"项目：{submission.project or '（未填项目）'}"),
                        project_style,
                    )
                )
                cell.append(
                    Paragraph(
                        escape(
                            f"最新工卡 {submission.created_at:%Y-%m-%d %H:%M:%S}"
                            f" · 工时 {_format_hours(total)} 小时 · 出勤 {days} 天"
                        ),
                        meta_style,
                    )
                )
                if image is None:
                    if submission.card_image:
                        cell.append(Paragraph("（工卡图片无法读取）", meta_style))
                    else:
                        cell.append(Paragraph("（该工卡未上传图片）", meta_style))
                    continue

                path, width, height = image
                scale = min(inner_width / width, image_cap / height, 1)
                cell.append(Spacer(1, 3))
                cell.append(
                    ReportImage(path, width=width * scale, height=height * scale)
                )
            return cell

        # 先给每个人做一格内容（姓名 → 工号 → 项目 → 最新一张考勤图片）
        cells = []
        for index, row in enumerate(rows, 1):
            # row["cards"] 已是「每个项目最新提交的那张」（与整理页同一份数据，按时间倒序）；
            # 「在休假」这类没有考勤图片的记录不排版
            card_items = []
            for submission in row["cards"]:
                if submission.project == VACATION_PROJECT:
                    continue
                total, days = hours_by_card.get(submission.pk, (Decimal("0"), 0))
                image = None
                if submission.project != VACATION_PROJECT and submission.card_image:
                    # 压缩后落盘再交给 reportlab，避免原图（几 MB）撑爆 PDF
                    path = os.path.join(tmpdir, f"card_{submission.pk}.jpg")
                    try:
                        with default_storage.open(submission.card_image.name, "rb") as fh:
                            raw = fh.read()
                        with Image.open(io.BytesIO(raw)) as img:
                            # JPEG 直接在解码阶段降采样，大图处理快很多
                            try:
                                img.draft(
                                    "RGB", (PDF_IMAGE_MAX_WIDTH, PDF_IMAGE_MAX_WIDTH)
                                )
                            except Exception:
                                pass
                            img = img.convert("RGB")
                            width, height = img.size
                            if width > PDF_IMAGE_MAX_WIDTH:
                                height = round(height * PDF_IMAGE_MAX_WIDTH / width)
                                width = PDF_IMAGE_MAX_WIDTH
                                img = img.resize((width, height), Image.LANCZOS)
                            img.save(
                                path,
                                "JPEG",
                                quality=PDF_IMAGE_JPEG_QUALITY,
                                optimize=True,
                            )
                        image = (path, width, height)
                    except Exception:
                        image = None
                card_items.append((submission, total, days, image))

            # 一格最多占半页（一页两行 = 4 个人）；放不进就把图片再等比压一压
            cell = build_worker_cell(index, row, card_items, image_max_height)
            used = measure_cell(cell)
            if used > cell_max_height:
                image_cap = max(80, image_max_height * cell_max_height / used * 0.97)
                cell = build_worker_cell(index, row, card_items, image_cap)

            cells.append(cell)

        # 两两并排成一行：一行就是两个人（格子矮的时候会自动多排几行）
        table_data = []
        for position in range(0, len(cells), 2):
            pair = cells[position : position + 2]
            if len(pair) == 1:
                pair = pair + [""]
            table_data.append(pair)

        table = Table(
            table_data,
            colWidths=[column_width, column_width],
            splitByRow=1,
            splitInRow=0,   # 一格已保证放得进一页，跨页时整行挪到下一页，不把人拆开
        )
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), cell_padding),
                    ("RIGHTPADDING", (0, 0), (-1, -1), cell_padding),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8dce6")),
                ]
            )
        )
        story.append(table)

        buffer = io.BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=margin,
            rightMargin=margin,
            topMargin=margin,
            bottomMargin=margin + 6 * mm,
            title="工人考勤工卡汇总",
        )

        def draw_footer(canvas, doc):
            canvas.saveState()
            canvas.setFont(font, 9)
            canvas.setFillColor(colors.HexColor("#999999"))
            canvas.drawString(margin, 10 * mm, "工人考勤工卡汇总")
            canvas.drawRightString(A4[0] - margin, 10 * mm, f"第 {doc.page} 页")
            canvas.restoreState()

        document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
        pdf = buffer.getvalue()

    filename = f"工人考勤工卡汇总_{date.today():%Y%m%d}.pdf"
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = (
        f"attachment; filename=cards.pdf; filename*=UTF-8''{quote(filename)}"
    )
    return response


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


def _latest_submission_map():
    """每个「人 + 项目」最新一条提交记录（工卡）：(姓名, 项目) -> 工卡 id。"""
    latest = {}
    queryset = Submission.objects.order_by("created_at", "id").values_list(
        "id", "name", "project"
    )
    for submission_id, name, project in queryset:
        latest[(name, project)] = submission_id
    return latest


def _latest_submission_ids():
    """只有这些工卡允许录入 / 保存工天数据，导出与合计也只取这些工卡关联的工时。"""
    return set(_latest_submission_map().values())


def _person_hours_map():
    """每个工人的累计工时：归一化姓名 -> 合计（跨项目相加，只取最新工卡关联的数据）。"""
    return {
        _normalize_name(item["name"]): item["total"] or Decimal("0")
        for item in WorkHour.objects.filter(submission_id__in=_latest_submission_ids())
        .values("name")
        .annotate(total=Sum("hours"))
    }


def _normalize_name(value):
    """姓名归一化：去首尾与连续空白、统一大写。

    姓名库与工卡里的姓名可能存在前后空格、大小写差异（例如 ' ABDUL MOJID'），
    汇总工时时按归一化后的姓名匹配，避免同一个人的工时统计不到。
    """
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


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
    latest_ids = _latest_submission_ids()
    # 每张工卡上已录入的工时条数，供删除前提示
    hours_count_map = {
        item["submission_id"]: item["c"]
        for item in WorkHour.objects.values("submission_id").annotate(c=Count("id"))
    }
    for submission in Submission.objects.order_by("-created_at"):
        submission.period_month = _payroll_month(submission.created_at)
        # 只有最新工卡可录入工时，历史工卡只读
        submission.is_latest_card = submission.pk in latest_ids
        submission.hours_count = hours_count_map.get(submission.pk, 0)
        # 按归一化姓名分组，避免姓名库与工卡姓名空格 / 大小写不一致导致分组不到
        grouped.setdefault(_normalize_name(submission.name), []).append(submission)

    # 每个工人的累计工时（跨项目相加，只取最新工卡关联的数据）
    hours_map = _person_hours_map()

    rows = []
    for person in Name.objects.all().only("name", "employee_no", "passport_no"):
        person_key = _normalize_name(person.name)
        submissions = grouped.get(person_key, [])

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

        total_hours = hours_map.get(person_key, Decimal("0"))
        rows.append(
            {
                "id": person.pk,
                "name": person.name,
                "employee_no": person.employee_no,
                "passport_no": person.passport_no,
                "total_hours": total_hours,
                "total_hours_label": _format_hours(total_hours),
                # 已提交工卡、但工时还没录（合计为 0）
                "is_zero": bool(submissions) and total_hours == 0,
                "count": len(submissions),
                "projects": blocks,
                "project_count": len(blocks),
                "latest": submissions[0] if submissions else None,
            }
        )

    total = len(rows)
    submitted_count = sum(1 for row in rows if row["count"])
    vacation_start, vacation_end = _vacation_period()
    context = {
        "rows": rows,
        "vacation_period_label": f"{vacation_start:%Y-%m-%d} ~ {vacation_end:%Y-%m-%d}",
        "total": total,
        "submitted_count": submitted_count,
        "missing_count": total - submitted_count,
        "duplicate_count": sum(1 for row in rows if row["count"] > 1),
        "multi_project_count": sum(1 for row in rows if row["project_count"] > 1),
        "zero_hours_count": sum(1 for row in rows if row["is_zero"]),
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
    # 只统计「人 + 项目」最新工卡上关联的工时，历史工卡的数据不导出
    queryset = (
        WorkHour.objects.filter(
            date__gte=start, date__lte=end, submission_id__in=_latest_submission_ids()
        )
        .values_list("name", "date", "hours", "project")
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

    # 按归一化姓名匹配姓名库，避免因空格 / 大小写差异取不到工号、护照号、单价
    profiles = {
        _normalize_name(person.name): person
        for person in Name.objects.all().only(
            "name", "employee_no", "passport_no", "hourly_rate"
        )
    }

    rows = []
    for name in sorted(workers):
        item = workers[name]
        person = profiles.get(_normalize_name(name))
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
