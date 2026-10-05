import json

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Name, Project, Submission


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


@csrf_exempt
@require_POST
def submit_form(request):
    """接收并保存表单提交。"""
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"ok": False, "message": "Invalid request data"}, status=400)

    name = (data.get("name") or "").strip()
    project = (data.get("project") or "").strip()
    remark = (data.get("remark") or "").strip()

    if not name or not project:
        return JsonResponse({"ok": False, "message": "Name and project cannot be empty"}, status=400)

    Submission.objects.create(name=name, project=project, remark=remark)
    return JsonResponse({"ok": True, "message": "Submitted successfully"})
