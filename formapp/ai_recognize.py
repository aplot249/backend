"""工卡图片 AI 识别：把卡上的「日期 + 手写工时」读成结构化数据。

设计原则：
  - **只给建议，不写数据库** —— 识别结果返回给整理页，管理员核对 / 调整后，
    仍然走原有的「保存工天数据」接口入库
  - 任何 OpenAI 兼容的视觉模型接口都能用（阿里云百炼 qwen-vl、智谱 GLM-4V、
    OpenAI gpt-4o、Gemini …），在 settings 里用环境变量配置
  - 识别有不确定性，所以：
      * 只输出**考勤周期内**的日期（周期外的丢进 `ignored`，不悄悄采纳）
      * 工时夹在 0~24 之间，日期必须是真实存在的日历日
      * 解析失败 / 超时 / 未配置都给出明确错误，不会写坏数据
"""
import base64
import io
import json
import re
import urllib.error
import urllib.request
from datetime import date, timedelta

from django.conf import settings
from PIL import Image


class AIConfigError(Exception):
    """AI_VISION_API_KEY / BASE_URL 没配。"""


class AIResponseError(Exception):
    """调 AI 接口失败（网络 / 鉴权 / 返回的不是合法 JSON）。"""


# 发给 AI 前把长边压到这个尺寸：省流量省 token，手写数字仍足够清楚
MAX_SIDE = 1400
JPEG_QUALITY = 82
# 页面框选后传上来的图片大小上限（防误传整张原图把请求撑爆）
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

PROMPT_TEMPLATE = """你是工地考勤工卡识别助手。图里是一张考勤工卡（Kad Perakuan / work card），通常包含：
- 一个表格：「日期 / Tarikh」列是日期数字（1~31），「Bil Hari Kerja」列是当天工作小时数（手写）
- 小时数常见写法：10、10.5、10+3（表示 10 加 3 共 13 小时）、8½ 等；空白、横线、划掉的格子表示当天没上班
- 卡片底部或边角可能写有考勤起止日期（例如 21-9-2026、20-10-2026，格式通常是 日-月-年）
- 工人姓名、工号可能竖着写在表格旁边（本任务不需要识别姓名）

本次考勤周期：{start} ~ {end}（YYYY-MM-DD）。
周期内每个「日号」只会出现一次：21~31 号属于 {prev_month}，1~20 号属于 {cur_month}。

任务：把表格里**每一天的工作小时数**读出来，严格按下面的 JSON 格式输出：
{{"entries": [{{"date": "YYYY-MM-DD", "hours": 10}}, {{"date": "YYYY-MM-DD", "hours": 13}}], "note": "简短说明"}}

规则：
1. hours 必须是数字："10+3" 记 13，"10.5" 或 "10½" 记 10.5，"8+4+2" 记 14
2. 空白 / 横线 / 0 的日期**不要**输出（当天没工时）
3. 把「日号」换算成周期内的完整日期；卡片上如果写明了起止日期，以它为准
4. 日期或小时看不清的格子直接跳过，并在 note 里说明是哪几天、不确定的原因
5. 只输出 JSON，不要输出任何其他文字、不要用 markdown 代码块
"""


def encode_image(raw):
    """把图片字节压到 MAX_SIDE 以内并转成 JPEG 的 data URL（真正发给 AI 的就是它）。"""
    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
    except Exception as exc:
        raise AIResponseError(f"图片读取失败：{exc}") from exc

    width, height = img.size
    scale = MAX_SIDE / max(width, height)
    if scale < 1:
        img = img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)

    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=JPEG_QUALITY)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _downscale_to_data_url(image_file):
    """把工卡原图（Django FieldFile）转成准备发给 AI 的 data URL。"""
    try:
        image_file.open("rb")
        raw = image_file.read()
    finally:
        try:
            image_file.close()
        except Exception:
            pass
    return encode_image(raw)


def decode_data_url(data_url):
    """把页面上传来的 `data:image/...;base64,xxx` 还原成图片字节。"""
    text = str(data_url or "")
    if not text.startswith("data:image/"):
        raise AIResponseError("选区图片格式不对（应该是 data:image/...;base64,...）")
    _, _, payload = text.partition(",")
    if not payload:
        raise AIResponseError("选区图片是空的")
    try:
        raw = base64.b64decode(payload)
    except Exception as exc:
        raise AIResponseError(f"选区图片解码失败：{exc}") from exc
    if not raw:
        raise AIResponseError("选区图片是空的")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AIResponseError(
            f"选区太大（{len(raw) // 1024} KB），请框小一点再识别"
        )
    return raw


REGION_PROMPT = """图里是考勤工卡上「当天工作小时数」那一列（手写的数字），可能竖排也可能横排。

任务：**按顺序**把每个格子里的小时数读出来，用英文逗号分隔输出，例如：
10,10,10,0,8,13,10

规则：
1. 从上到下、从左到右依次读，顺序不能乱（后面会按这个顺序一天一天填进表格）
2. 只输出数字：整数或小数都行；"10+3" 这种加号算式记它们的和（13）；"10½" 记 10.5
3. 空白、横线、划掉、看不清的格子记 0 —— **不能省略**，否则后面全串位
4. 不要输出任何其他文字、不要换行、不要解释、不要用方括号或引号
"""

# 逗号分隔文本里允许的分隔符（中英文逗号、顿号、分号、空格、竖线）
SPLIT_RE = re.compile(r"[,，、;；|\s]+")


def split_items(text):
    """把「10,10+3,０,8」拆成单项，顺手把全角数字 / 中文句号换成正常写法。

    **读不出来的项按 0 占位**（记在 `skipped` 里）：因为后面要按「第几个数 = 第几天」
    依次填表，直接丢掉会让后面全部串位。
    """
    text = str(text or "").strip()
    text = text.replace("０", "0").replace("１", "1").replace("２", "2").replace(
        "３", "3").replace("４", "4").replace("５", "5").replace("６", "6").replace(
        "７", "7").replace("８", "8").replace("９", "9").replace("。", ".")
    if not text:
        return [], []
    items, skipped = [], []
    for token in SPLIT_RE.split(text):
        if not token:
            continue
        hours = _hours_of(token)
        if hours is None or hours < 0:
            skipped.append(token)
            items.append(0.0)      # 占位，保住后面各项的位置
            continue
        items.append(round(min(hours, 24.0), 2))
    return items, skipped


def recognize_region(data_url):
    """识别页面框选的区域：返回逗号分隔的工时文本 + 拆好的数值。

    页面会把它填进一个可编辑的输入框，管理员核对 / 改完再点「填入工时表」，
    程序按逗号拆分、从周期第一天开始一天一格地填。
    """
    raw = decode_data_url(data_url)
    text = call_vision_model(encode_image(raw), REGION_PROMPT)
    items, skipped = split_items(text)
    return {
        "text": ",".join(str(int(v)) if float(v).is_integer() else str(v) for v in items),
        "values": items,
        "count": len(items),
        "skipped": skipped,
        "raw": text.strip()[:300],
    }


def _build_prompt(start, end):
    prev_month, cur_month = (start.year, start.month), (end.year, end.month)
    return PROMPT_TEMPLATE.format(
        start=start.isoformat(), end=end.isoformat(),
        prev_month=f"{prev_month[0]}-{prev_month[1]:02d}",
        cur_month=f"{cur_month[0]}-{cur_month[1]:02d}",
    )


def call_vision_model(data_url, prompt):
    """调 OpenAI 兼容的视觉模型接口，返回模型输出的文本。"""
    if not settings.AI_VISION_BASE_URL or not settings.AI_VISION_API_KEY:
        raise AIConfigError(
            "AI 识别未配置：请在服务器环境变量里设置 AI_VISION_BASE_URL 和 "
            "AI_VISION_API_KEY（可选 AI_VISION_MODEL），然后重启服务"
        )

    url = f"{settings.AI_VISION_BASE_URL}/chat/completions"
    payload = {
        "model": settings.AI_VISION_MODEL,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    # 智谱 GLM-4.5V / 4.6V 这类「推理模型」默认要先想一大段再回答，读一张工卡能想 90 秒以上。
    # 数格子这种任务不需要推理，关掉能快几倍（其它服务商不认识这个字段也不影响）
    if getattr(settings, "AI_VISION_DISABLE_THINKING", False):
        payload["thinking"] = {"type": "disabled"}

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.AI_VISION_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.AI_VISION_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except AIConfigError:
        raise
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise AIResponseError(f"AI 接口返回 {exc.code}：{detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise AIResponseError(f"连不上 AI 接口：{exc}") from exc

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIResponseError(f"AI 返回格式不对：{str(body)[:300]}") from exc
    if not text:
        raise AIResponseError("AI 返回了空内容（可能被安全策略拦截），请换张清晰的卡或换个模型试试")
    return text


def _extract_json(text):
    """模型偶尔会用 ```json 包一层，把 JSON 部分剥出来。"""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.S)
        if brace:
            text = brace.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIResponseError(f"AI 没有返回合法 JSON：{text[:300]}") from exc


def _hours_of(value):
    """「10+3」=13、「10.5」=10.5、「10h」=10、8½ 之类统一转成数字；转不出来返回 None。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace("½", ".5").replace("１", "1").replace("２", "2")
    # 去掉单位后缀（AI 偶尔写成 10h / 10小时）
    text = re.sub(r"(?i)(小时|hours|hrs|h)$", "", text).strip()
    if not text:
        return None
    parts = [p for p in re.split(r"\+", text) if p.strip()]
    total = 0.0
    for part in parts:
        try:
            total += float(part.strip())
        except ValueError:
            return None
    return total


def parse_recognition(text, period_start, period_end):
    """把 AI 的输出解析并校验成 {日期: 工时}。

    返回 `(entries, ignored, note)`：
      entries  {“YYYY-MM-DD”: hours}，全部落在考勤周期内、工时 0<h<=24
      ignored  [(原始内容, 原因)]，例如周期外 / 看不懂 / 工时超范围的
      note     AI 自己给的说明
    """
    data = _extract_json(text)
    raw_entries = data.get("entries") if isinstance(data, dict) else None
    note = str(data.get("note") or "") if isinstance(data, dict) else ""

    if not isinstance(raw_entries, list):
        raise AIResponseError(f"AI 返回的 entries 不是列表：{text[:300]}")

    entries, ignored = {}, []
    for item in raw_entries:
        if not isinstance(item, dict):
            ignored.append((str(item)[:60], "格式不对"))
            continue
        raw_date = str(item.get("date") or item.get("day") or "").strip()
        hours = _hours_of(item.get("hours"))
        try:
            work_date = date.fromisoformat(raw_date)
        except ValueError:
            ignored.append((raw_date, "日期格式不对"))
            continue
        if not (period_start <= work_date <= period_end):
            ignored.append((work_date.isoformat(), "不在本考勤周期内"))
            continue
        if hours is None or hours <= 0:
            ignored.append((work_date.isoformat(), "没识别到工时"))
            continue
        hours = min(hours, 24.0)
        # 同一天出现多次时取较大值（宁可多填让人改小，也别漏）
        entries[work_date.isoformat()] = round(max(entries.get(work_date.isoformat(), 0), hours), 2)

    return entries, ignored, note


def recognize_hours(image_file, period_start, period_end):
    """识别一张工卡：返回 {"entries": {...}, "ignored": [...], "note": "…"}。"""
    data_url = _downscale_to_data_url(image_file)
    text = call_vision_model(data_url, _build_prompt(period_start, period_end))
    entries, ignored, note = parse_recognition(text, period_start, period_end)
    return {"entries": entries, "ignored": ignored, "note": note}
