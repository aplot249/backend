"""后台首页用的「待处理申请」计数。

用法：`{% load resubmit_tags %}{% pending_resubmit_count as n %}`
"""
from django import template

from formapp.models import CardResubmitRequest

register = template.Library()


@register.simple_tag
def pending_resubmit_count():
    """还没有处理的「工人申请再次提交工卡」条数。"""
    return CardResubmitRequest.objects.filter(
        status=CardResubmitRequest.STATUS_PENDING
    ).count()
