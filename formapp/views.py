import io
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
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

logger = logging.getLogger(__name__)

from .ai_recognize import (
    AIConfigError,
    AIResponseError,
    recognize_hours,
    recognize_region,
)
from .models import (
    Name,
    Project,
    Submission,
    SystemSetting,
    WorkHour,
    normalize_name as _normalize_name,   # 姓名归一化唯一实现放在 models（提交匹配 / 项目分组都用）
)

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
        request,
        "check.html",
        {
            "form_enabled": SystemSetting.get_setting().form_enabled,
            # 工人可选周期：默认当前周期（按今天所在月份），只看这个周期的工卡
            "periods": _export_periods(),
            "default_period": _payroll_month(date.today()),
        },
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


def _card_image_url(submission):
    """工卡图片地址；没图或文件不在了就返回空字符串（前端会显示文字提示）。"""
    if not submission or not submission.card_image:
        return ""
    try:
        return submission.card_image.url
    except (ValueError, OSError):
        return ""


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
        # 姓名库里没有这个人；而每张工卡都属于姓名库里的某个人，所以就是查无此人
        return JsonResponse(
            {
                "ok": True,
                "found": False,
                "field": "name",
                "message": "No record found for this name. Please check the spelling "
                "(it should be the same as on your work card).",
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

    matched_by = "passport" if code_key == passport_key else "employee_no"

    # 只取**所选考勤周期**的最新工卡（周期按提交月份），历史周期的数据不混进来
    period_key = (request.GET.get("period") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", period_key):
        period_key = _payroll_month(date.today())
    period_start, period_end = _period_range(period_key)
    period_label = f"{period_key} ({period_start:%Y-%m-%d} ~ {period_end:%Y-%m-%d})"

    pairs = [
        (project, submission_id)
        for (person_id, project, _card_period), submission_id in _latest_submission_map(
            period_key
        ).items()
        if person_id == person.pk
    ]

    if not pairs:
        return JsonResponse(
            {
                "ok": True,
                "found": False,
                "field": "name",
                "period": period_key,
                "period_label": period_label,
                "message": "No work card has been submitted under this name in "
                + period_label
                + ". Please pick another period.",
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
                # 这次提交的那张工卡图片（查询页在项目工时下方直接显示给工人看）
                "card_image_url": _card_image_url(submission),
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
            "period": period_key,
            "period_label": period_label,
            "projects": projects,
        }
    )


@require_GET
def search_name(request):
    """按关键字模糊搜索姓名，返回**姓名库里的标准写法**（工人点一下就填进表单）。

    匹配从强到弱排序：归一化后完全相同 → 前缀 / 包含子串 → 单词都出现（忽略顺序）
    → 拼写相近（个别字母打错）。这样工人随手写的写法（大小写、`MD.` 这类标点、
    词序不同、少打一个字母）也能搜到自己并选中，提交时后台还会再校验一次。
    """
    keyword = request.GET.get("q", "").strip()
    names = list(Name.objects.values_list("name", flat=True))
    if not keyword:
        return JsonResponse({"results": names[:20]})

    key = _normalize_name(keyword)
    words = [w for w in re.split(r"[^0-9A-Za-z]+", key) if w]
    scored = []
    for name in names:
        norm = _normalize_name(name)
        if norm == key:
            score = 0
        elif norm.startswith(key) or key in norm:
            score = 1
        elif words and all(w in norm for w in words):
            score = 2                       # 单词都在，只是顺序不同
        elif words and any(w in norm for w in words):
            # 部分单词对上（如多写了 AL）：按「对上的字母比例」排，越像的越靠前
            hit = sum(len(w) for w in words if w in norm)
            score = 3 + (1 - hit / sum(len(w) for w in words))
        else:
            ratio = SequenceMatcher(None, key, norm).ratio()
            if ratio < 0.72:
                continue
            score = 4 + (1 - ratio)         # 拼写相近
        scored.append((score, name))

    scored.sort(key=lambda item: (item[0], item[1]))
    return JsonResponse({"results": [name for _score, name in scored[:20]]})


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

    - 后台「系统开关」关闭填报表单时，即使绕过页面直接调接口也会被拒绝；
      后台管理员登录着时同样不受限制（与表单页面保持一致）
    - **姓名必须与姓名库匹配**：匹配不到就拒绝（英文提示「请从列表里选你的姓名」），
      匹配到就把这条工卡**用外键挂到姓名库那个人身上**（不存姓名文本），
      所以不可能出现「姓名对不上库」的工卡，后台改名也会自动跟着变
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

    # 姓名必须能在姓名库里找到：找到就直接关联到那个人
    person = _match_person(name)
    if person is None:
        return JsonResponse(
            {
                "ok": False,
                "field": "name",
                "message": "We could not find your name in our records. "
                "Please select your name from the list.",
            },
            status=400,
        )

    error = _validate_card_image(card_image)
    if error:
        return JsonResponse({"ok": False, "message": error}, status=400)

    Submission.objects.create(
        person=person, project=project, remark=remark, card_image=card_image
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

    只允许在该工卡**所属考勤周期**（按提交月份）里的最新卡上保存；更早的那几张只读。
    每个周期各算各的最新卡，所以上个月的历史卡在自己的周期里仍然可以补录。
    """
    submission = get_object_or_404(Submission, pk=submission_id)

    if submission.pk not in _latest_submission_ids(_card_period_key(submission)):
        return JsonResponse(
            {
                "ok": False,
                "message": "这是同一周期里较早的一张工卡，只有该周期最新提交的工卡才能录入工天数据",
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
            person=submission.person,
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


@staff_member_required
@require_POST
def ai_recognize_hours(request, submission_id):
    """AI 识别工卡工时，**只返回建议，不写数据库**。

    两种模式（页面用前者，命令行测用后者）：
      - **选区模式**（请求带 `image`，页面框选后传来）：只按顺序读数字，
        返回逗号分隔的文本，管理员在页面上编辑后自己点「填入工时表」
      - **整卡模式**（不带 `image`）：读「日期 + 工时」，返回 {日期: 工时}
    """
    submission = get_object_or_404(Submission, pk=submission_id)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        data = {}

    try:
        # ---------- 选区模式：页面框选的那块图 ----------
        if data.get("image"):
            try:
                result = recognize_region(data["image"])
            except (AIConfigError, AIResponseError) as exc:
                status = 503 if isinstance(exc, AIConfigError) else 502
                return JsonResponse({"ok": False, "message": str(exc)}, status=status)
            return JsonResponse(
                {
                    "ok": True,
                    "mode": "region",
                    "text": result["text"],
                    "values": result["values"],
                    "count": result["count"],
                    "skipped": result["skipped"],
                    "raw": result["raw"],
                }
            )

        # ---------- 整卡模式：识别整张工卡上的日期 + 工时 ----------
        if not submission.card_image:
            return JsonResponse(
                {"ok": False, "message": "这张工卡没有图片，无法识别"}, status=400
            )

        # 识别哪个考勤周期由弹窗当前选中的周期决定（默认这张卡提交时标注的月份）
        period_month = (data.get("period_month") or "").strip() or _payroll_month(
            submission.created_at
        )
        if not re.fullmatch(r"\d{4}-\d{2}", period_month):
            return JsonResponse({"ok": False, "message": "考勤周期格式不对"}, status=400)

        start, end = _period_range(period_month)

        try:
            result = recognize_hours(submission.card_image, start, end)
        except AIConfigError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=503)
        except AIResponseError as exc:
            return JsonResponse({"ok": False, "message": str(exc)}, status=502)

        total = sum(result["entries"].values())
        return JsonResponse(
            {
                "ok": True,
                "mode": "card",
                "period_month": period_month,
                "period_range": f"{start.isoformat()} ~ {end.isoformat()}",
                "entries": result["entries"],
                "ignored": result["ignored"],
                "note": result["note"],
                "count": len(result["entries"]),
                "total_hours": round(total, 2),
            }
        )
    except Exception:  # 兜底：识别是辅助功能，不能因为报错把整理页搞挂
        logger.exception("AI 识别失败 submission_id=%s", submission_id)
        return JsonResponse(
            {"ok": False, "message": "AI 识别失败，请重试（详情见服务端日志）"}, status=500
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
        Submission.objects.filter(person=person, project=VACATION_PROJECT)
        .order_by("-created_at")
        .first()
    )
    if submission is None:
        submission = Submission.objects.create(
            person=person, project=VACATION_PROJECT, remark="系统生成：转为休假"
        )

    created = 0
    updated = 0
    day = start
    while day <= end:
        _, is_created = WorkHour.objects.update_or_create(
            person=person,
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
    """某张工卡所属工人**在该工卡所属周期**里的累计工时，口径与整理页当期一致。

    弹窗里保存工天后用它刷新页面上该工人的「总工时」，无需刷新整个页面。
    """
    submission = get_object_or_404(Submission, pk=submission_id)
    total = _person_hours_map(_card_period_key(submission)).get(
        submission.person_id, Decimal("0")
    )
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

    完全以**姓名库**（`Name`）为基准，和整理页同一套口径：
      - 工卡用外键指向姓名库，按人逐个列出（不存在「对不上库」的卡了）
      - 每个人每个「项目」只取**最新提交**的那张工卡；一个人干多个项目就都列出来
      - 项目名忽略大小写 / 空格 / 标点差异，避免同一个项目被算成两个
      - 休假（只有「在休假」记录）或没提交过工卡的人不列入

    周期 = **工卡提交时间所在月份**（每个「人 + 项目 + 周期」各留一张最新卡），
    period_key 为 None 时不按考勤周期筛选（导出所有周期各自的最新工卡）。
    """
    start, end = _period_range(period_key) if period_key else (None, None)

    profiles = {
        person.pk: person
        for person in Name.objects.all().only("name", "employee_no", "passport_no")
    }

    # 每个「人 + 项目 + 考勤周期」最新一张工卡（周期按提交月份，各周期互不相干）
    latest_ids = _latest_submission_map(period_key)

    submissions = {
        s.pk: s
        for s in Submission.objects.filter(pk__in=set(latest_ids.values())).only(
            "pk", "person_id", "project", "card_image", "created_at"
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

    # 每张工卡上已登记工时的日期，用于判断这张卡属于哪个考勤周期
    hours_dates = {}
    for submission_id, work_date in WorkHour.objects.filter(
        submission_id__in=set(submissions)
    ).values_list("submission_id", "date"):
        hours_dates.setdefault(submission_id, set()).add(work_date)

    def card_in_period(submission):
        if period_key is None:
            return True
        # ① 提交时间所在月份 = 该周期（主口径）
        if _payroll_month(submission.created_at) == period_key:
            return True
        # ② 兜底：卡上登记工时的日期落在该考勤区间内（老数据提交月份算错时仍能捞回来）
        return any(start <= d <= end for d in hours_dates.get(submission.pk, ()))

    cards_by_person = {}
    for (person_id, _project_key, _card_period), submission_id in latest_ids.items():
        submission = submissions.get(submission_id)
        if submission is None:
            continue
        if not card_in_period(submission):
            continue
        cards_by_person.setdefault(person_id, []).append(submission)

    # 休假人员：项目为「在休假」的工时记录
    vacation_queryset = WorkHour.objects.filter(project=VACATION_PROJECT)
    if period_key:
        vacation_queryset = vacation_queryset.filter(date__gte=start, date__lte=end)
    vacation_keys = set(
        vacation_queryset.values_list("person_id", flat=True)
    )

    rows = []
    for person_id, person in profiles.items():
        cards = sorted(
            cards_by_person.get(person_id, []),
            key=lambda item: item.created_at,
            reverse=True,
        )
        if not cards:
            continue
        rows.append(
            {
                "name": person.name,
                "employee_no": person.employee_no,
                "passport_no": person.passport_no,
                "cards": cards,
                "vacation": person_id in vacation_keys,
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
    - `?month=all`（默认）导出全部；`?month=YYYY-MM` 只导出该周期的工卡，判定取并集：
      工卡录入月份 = 该月，或卡上已登记工时落在该考勤区间（上月 21 日 ~ 本月 20 日）
    """
    month = (request.GET.get("month") or "all").strip()
    period_key = None if month in ("", "all") else month
    start, end = _period_range(period_key) if period_key else (None, None)
    rows, hours_by_card = _collect_card_pdf_rows(period_key)

    # _collect_card_pdf_rows 已按姓名库筛过，这里再去掉「只剩休假记录」的人
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
        else (
            f"{period_key}：考勤区间 {start:%Y-%m-%d} ~ {end:%Y-%m-%d}"
            f"（含 {period_key} 月录入的工卡）"
        )
    )
    card_count = sum(len(row["cards"]) for row in rows)

    story = [
        Paragraph("工人考勤工卡汇总", title_style),
        Paragraph(
            escape(
                f"考勤周期：{period_label}　生成时间：{datetime.now():%Y-%m-%d %H:%M}"
                f"　共 {len(rows)} 人 / {card_count} 张工卡"
                f"（按姓名库逐个列出，每人每个项目取最新提交的那张工卡；"
                f"休假 / 未提交的工人不列入）"
            ),
            head_style,
        ),
    ]
    story.append(Spacer(1, 4))

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

        if not cells:
            # 该周期一张工卡都没有：出个说明页，不要报 500
            story.append(
                Paragraph(
                    "该考勤周期没有可导出的工卡（没人提交工卡，或工卡不属于该周期）。",
                    note_style,
                )
            )
        else:
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


def _card_period_key(submission):
    """工卡所属考勤周期：**以提交时间所在月份为准**。

    例：2026 年 10 月提交的卡 → 2026-10 周期（2026-09-21 ~ 2026-10-20）
        2026 年 11 月提交的卡 → 2026-11 周期（2026-10-21 ~ 2026-11-20）

    每个月统计一次，各周期互不相干：做 11 月统计时看不到 10 月的卡，
    反过来补导 10 月报表也不受 11 月新卡影响。
    """
    return _payroll_month(submission.created_at)


def _latest_submission_map(period_key=None):
    """每个「人 + 项目 + 考勤周期」最新一条工卡：(人 id, 归一化项目, 周期) -> 工卡 id。

    周期是第三层维度 —— 每个周期各留各的最新卡，新周期的新卡**不会**把上一周期的卡
    挤成「历史卡」，所以任何时候回看历史周期的数据都在。
    period_key 给定时只看该周期。
    """
    latest = {}
    queryset = Submission.objects.order_by("created_at", "id").values_list(
        "id", "person_id", "project", "created_at"
    )
    for submission_id, person_id, project, created_at in queryset:
        card_period = _payroll_month(created_at)
        if period_key and card_period != period_key:
            continue
        latest[(person_id, _normalize_name(project), card_period)] = submission_id
    return latest


def _latest_submission_ids(period_key=None):
    """这些工卡允许录入 / 保存工天数据，导出与合计也只取这些工卡关联的工时。
    period_key 给定时只看该周期；不传则是所有周期各自的最新卡。"""
    return set(_latest_submission_map(period_key).values())


def _person_hours_map(period_key=None):
    """每个工人的累计工时：人 id -> 合计（跨项目相加，只取该周期最新工卡关联的数据）。"""
    return {
        item["person_id"]: item["total"] or Decimal("0")
        for item in WorkHour.objects.filter(
            submission_id__in=_latest_submission_ids(period_key)
        )
        .values("person_id")
        .annotate(total=Sum("hours"))
    }


def _card_period_keys():
    """所有提交过工卡的考勤周期（按提交月份）。"""
    return {
        _payroll_month(created_at)
        for created_at in Submission.objects.dates("created_at", "month")
    }


def _match_person(raw_name):
    """把填进来的姓名匹配到姓名库，返回**姓名库里的那个人**（`Name`）；匹配不到返回 None。

    ① 归一化后完全相同（忽略大小写 / 空格 / `. : , ; _ - /`，如 `MD.SUMON` = `MD SUMON`）
    ② 单词完全一样、只是顺序不同（如 `md rasel mia` = `MIA MD RASEL`），
       且库里只有这一种写法时才认 —— 否则可能把两个不同的人合成一个

    提交时用它的结果建外键，工卡就永远属于库里那个人，
    整理页 / 工时统计 / PDF 导出都不会再因为写法不同而漏人。
    """
    key = _normalize_name(raw_name)
    if not key:
        return None

    persons = list(Name.objects.all().only("pk", "name"))
    for person in persons:
        if _normalize_name(person.name) == key:
            return person

    wanted = tuple(sorted(key.split()))
    if len(wanted) < 2:
        return None
    matched = [
        person
        for person in persons
        if tuple(sorted(_normalize_name(person.name).split())) == wanted
    ]
    return matched[0] if len(matched) == 1 else None


def _period_key(day):
    """某一天所属的考勤周期键：21 号及以后归入下个月周期。"""
    if day.day >= 21:
        year, month = (day.year, day.month + 1) if day.month < 12 else (day.year + 1, 1)
        return f"{year:04d}-{month:02d}"
    return f"{day.year:04d}-{day.month:02d}"


def _export_periods():
    """考勤周期（倒序），供下拉选择：提交过工卡的周期 + 有工时记录的周期 + 当前周期。"""
    keys = set(_card_period_keys())
    keys.update(_period_key(day) for day in WorkHour.objects.dates("date", "day"))
    keys.add(_payroll_month(date.today()))
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
    """数据整理页：**只统计所选考勤周期**（默认最近提交过工卡的周期）。

    工卡属于哪个周期按**提交时间所在月份**算（`?period=YYYY-MM` 切换），
    页面上的姓名库人数 / 已提交 / 未提交 / 累计工时全部是**该周期**的口径。
    """
    periods = _export_periods()
    known_keys = {item["key"] for item in periods}
    period_key = (request.GET.get("period") or "").strip()
    if period_key not in known_keys:
        period_key = _default_period_key()
    period_year, period_month = int(period_key[:4]), int(period_key[5:7])
    period_start, period_end = _period_range(period_key)

    grouped = {}
    latest_ids = _latest_submission_ids(period_key)
    # 每张工卡上已录入的工时条数，供删除前提示
    hours_count_map = {
        item["submission_id"]: item["c"]
        for item in WorkHour.objects.values("submission_id").annotate(c=Count("id"))
    }
    for submission in (
        Submission.objects.filter(
            created_at__year=period_year, created_at__month=period_month
        )
        .order_by("-created_at")
        .select_related("person")
        .only("pk", "person_id", "person__name", "project", "card_image", "created_at")
    ):
        submission.period_month = _payroll_month(submission.created_at)
        # 只有本周期的最新工卡可录入工时，更早的那几张只读
        submission.is_latest_card = submission.pk in latest_ids
        submission.hours_count = hours_count_map.get(submission.pk, 0)
        # 工卡用外键指向姓名库，直接按人 id 分组
        grouped.setdefault(submission.person_id, []).append(submission)

    # 每个工人的累计工时（跨项目相加，只取本周期最新工卡关联的数据）
    hours_map = _person_hours_map(period_key)

    rows = []
    for person in Name.objects.all().only("name", "employee_no", "passport_no"):
        person_key = person.pk
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
        "periods": periods,
        "default_period": period_key,
        "period": period_key,
        "period_label": f"{period_key}（{period_start:%Y-%m-%d} ~ {period_end:%Y-%m-%d}）",
        "period_card_count": sum(len(items) for items in grouped.values()),
    }
    return render(request, "summary.html", context)


def _default_period_key():
    """默认周期：最近一个**提交过工卡**的周期；一张卡都没有时取当前月份。"""
    keys = sorted(_card_period_keys())
    return keys[-1] if keys else _payroll_month(date.today())


def _find_export_template():
    """样表模板位置：后端根目录，或上一级项目根目录。"""
    for base in (settings.BASE_DIR, settings.BASE_DIR.parent):
        candidate = base / EXPORT_TEMPLATE_FILE
        if candidate.exists():
            return candidate
    return None


def _collect_export_rows(start, end, period_key=None):
    """汇总周期内每个工人的数据：同一天的多个项目工时相加。

    period_key 对应的周期里每个「人 + 项目」取最新那张卡的工时；
    其它周期的卡就算是这个人更新的卡，也不参与本次导出。
    """
    workers = {}
    # 只统计该周期「人 + 项目」最新工卡上关联的工时
    queryset = (
        WorkHour.objects.filter(
            date__gte=start,
            date__lte=end,
            submission_id__in=_latest_submission_ids(period_key),
        )
        .values_list("person_id", "date", "hours", "project")
    )
    for person_id, work_date, hours, project in queryset:
        hours = hours or Decimal("0")
        item = workers.setdefault(
            person_id, {"hours": {}, "projects": [], "total": Decimal("0")}
        )
        item["hours"][work_date] = item["hours"].get(work_date, Decimal("0")) + hours
        item["total"] += hours
        if project and project not in item["projects"]:
            item["projects"].append(project)

    # 工时用外键指向姓名库，直接取工号 / 护照号 / 单价
    profiles = {
        person.pk: person
        for person in Name.objects.all().only(
            "name", "employee_no", "passport_no", "hourly_rate"
        )
    }

    def person_sort_key(person_id):
        person = profiles.get(person_id)
        return person.name if person else ""

    rows = []
    for person_id in sorted(workers, key=person_sort_key):
        item = workers[person_id]
        person = profiles.get(person_id)
        rate = person.hourly_rate if person else Decimal("0")
        rows.append(
            {
                "name": person.name if person else "",
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

    rows = _collect_export_rows(start, end, month_key)

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
