import io
import json
from pathlib import Path

from django.contrib.admin.views.decorators import staff_member_required
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from PIL import Image, UnidentifiedImageError

from .models import Name, Project, Submission

# 工卡图片限制
MAX_CARD_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
ALLOWED_CARD_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


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
@ensure_csrf_cookie
def summary_view(request):
    """数据整理页：以姓名库为基准，展示每个人的提交情况（最新提交置顶）。"""
    grouped = {}
    for submission in Submission.objects.order_by("-created_at"):
        grouped.setdefault(submission.name, []).append(submission)

    rows = []
    for person in Name.objects.all().only("name"):
        submissions = grouped.get(person.name, [])
        rows.append(
            {
                "name": person.name,
                "count": len(submissions),
                "submissions": submissions,  # 已按提交时间倒序，第一条为最新
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
    }
    return render(request, "summary.html", context)
