"""把考勤表《Latest list of attendance.xlsx》里的工种、护照号、单价
填充到系统「姓名库」（Name）的工号、护照号、工时系数。

用法（在 backend 目录执行）：
    python import_attendance.py                # 写入数据库
    python import_attendance.py --dry-run      # 只预览，不写库
    python import_attendance.py --file 路径    # 指定 Excel 文件

匹配规则：Excel 的「姓名/Name」与姓名库的 name 比对，
忽略大小写、首尾与连续空格差异；仍匹配不上时用「只保留字母数字」的宽松规则再试一次。
只更新 Excel 中有值的字段，已有值且相同则跳过。
"""
import argparse
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from openpyxl import load_workbook  # noqa: E402

from formapp.models import Name  # noqa: E402

DEFAULT_FILE = BASE_DIR / "Latest list of attendance.xlsx"
# 样表：第 1 行为标题，第 2 行为表头，第 3 行起为数据
HEADER_ROW = 2
FIRST_DATA_ROW = 3
# 找不到表头时的兜底列位置：B 姓名 / D 工种 / E 护照号 / F 单价
FALLBACK_COLUMNS = {"name": 2, "employee_no": 4, "passport_no": 5, "rate": 6}
COLUMN_KEYWORDS = {
    "name": ("姓名", "name"),
    "employee_no": ("工种", "employee"),
    "passport_no": ("护照", "passport"),
    "rate": ("单价",),
}


def normalize(value):
    """归一化：去首尾与连续空白、统一大写。"""
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


def loose_key(value):
    """宽松匹配键：只保留字母和数字。"""
    return re.sub(r"[^A-Z0-9]", "", normalize(value))


def find_columns(sheet):
    """按第 2 行表头文字定位列，定位不到则用兜底位置。"""
    headers = {}
    for column in range(1, sheet.max_column + 1):
        text = str(sheet.cell(row=HEADER_ROW, column=column).value or "").lower()
        if not text:
            continue
        for field, keywords in COLUMN_KEYWORDS.items():
            if field in headers:
                continue
            if any(keyword.lower() in text for keyword in keywords):
                headers[field] = column
    columns = dict(FALLBACK_COLUMNS)
    columns.update(headers)
    return columns


def clean_text(value):
    """工号 / 护照号：数字按整数展示，其余去空白。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def clean_rate(value):
    """单价：转成两位小数的 Decimal，无法解析返回 None。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def build_index():
    """姓名库索引：精确键 -> 人，宽松键 -> [人]。"""
    exact = {}
    loose = {}
    for person in Name.objects.all().only("id", "name", "employee_no", "passport_no", "hourly_rate"):
        exact[normalize(person.name)] = person
        loose.setdefault(loose_key(person.name), []).append(person)
    return exact, loose


def main():
    parser = argparse.ArgumentParser(description="导入考勤表的工种 / 护照号 / 单价到姓名库")
    parser.add_argument("--file", default=str(DEFAULT_FILE), help="Excel 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入数据库")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"找不到文件：{path}")
        return 1

    workbook = load_workbook(path, data_only=True)
    sheet = workbook.worksheets[0]
    columns = find_columns(sheet)
    print(f"工作表：{sheet.title}")
    print(
        "列位置：姓名={name} 工号={employee_no} 护照号={passport_no} 单价={rate}".format(
            **columns
        )
    )

    exact, loose = build_index()
    print(f"姓名库共 {len(exact)} 人")

    updated = []
    unchanged = 0
    skipped = 0
    unmatched = []
    ambiguous = []
    duplicates = []
    seen_ids = set()

    for row in range(FIRST_DATA_ROW, sheet.max_row + 1):
        raw_name = sheet.cell(row=row, column=columns["name"]).value
        name = normalize(raw_name)
        if not name:
            skipped += 1
            continue

        person = exact.get(name)
        if person is None:
            candidates = loose.get(loose_key(name), [])
            if len(candidates) == 1:
                person = candidates[0]
            elif len(candidates) > 1:
                ambiguous.append(name)
                continue

        if person is None:
            unmatched.append(name)
            continue

        # 一人只取 Excel 里第一条，重复行单独报告
        if person.id in seen_ids:
            duplicates.append((row, name))
            continue
        seen_ids.add(person.id)

        employee_no = clean_text(sheet.cell(row=row, column=columns["employee_no"]).value)
        passport_no = clean_text(sheet.cell(row=row, column=columns["passport_no"]).value)
        rate = clean_rate(sheet.cell(row=row, column=columns["rate"]).value)

        changed = False
        if employee_no and person.employee_no != employee_no:
            person.employee_no = employee_no
            changed = True
        if passport_no and person.passport_no != passport_no:
            person.passport_no = passport_no
            changed = True
        if rate is not None and person.hourly_rate != rate:
            person.hourly_rate = rate
            changed = True

        if changed:
            updated.append(person)
        else:
            unchanged += 1

    print("-" * 60)
    print(f"匹配成功并更新：{len(updated)} 人")
    print(f"匹配成功但无变化：{unchanged} 人")
    print(f"空行跳过：{skipped} 行")
    print(f"姓名库里找不到：{len(unmatched)} 人")
    print(f"宽松匹配到多个人（已跳过）：{len(ambiguous)} 人")
    print(f"同名重复行（仅取 Excel 里第一条）：{len(duplicates)} 行")

    if unmatched:
        print("-" * 60)
        print("姓名库中不存在的姓名（前 30 个）：")
        for name in unmatched[:30]:
            print("  -", name)
        if len(unmatched) > 30:
            print(f"  ... 其余 {len(unmatched) - 30} 个")
    if ambiguous:
        print("-" * 60)
        print("匹配到多个同名人（已跳过）：")
        for name in ambiguous[:30]:
            print("  -", name)
    if duplicates:
        print("-" * 60)
        print("同名重复行（只取第一条，其余未导入）：")
        for row, name in duplicates:
            print(f"  - 第 {row} 行：{name}")

    if args.dry_run:
        print("-" * 60)
        print("预览模式（--dry-run），未写入数据库。示例（前 10 条）：")
        for person in updated[:10]:
            print(
                f"  {person.name} -> 工号 {person.employee_no} | "
                f"护照 {person.passport_no} | 单价 {person.hourly_rate}"
            )
        return 0

    if updated:
        Name.objects.bulk_update(
            updated, ["employee_no", "passport_no", "hourly_rate"]
        )
    print("-" * 60)
    print(f"已写入数据库：{len(updated)} 人")
    return 0


if __name__ == "__main__":
    sys.exit(main())
