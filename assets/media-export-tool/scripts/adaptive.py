# -*- coding: utf-8 -*-
"""自适应容错公共模块（阶段一：抖音试点）

提供四类能力，均保持"配置只放数据、逻辑收在函数里"：
1. resolve_first   —— 多策略定位，按候选顺序试，返回命中的 locator 与策略名
2. check_fingerprint —— 页面指纹（关键结构文本）比对，缺了即报漂移
3. validate_xlsx_contract —— xlsx 产出契约校验（必备列名 + 最低行数）
4. write_manifest  —— 生成每次导出的数据清单
"""
import json
import os
import csv
import re
from datetime import datetime
import uuid

DEFAULT_STRATEGY = "?"


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


async def resolve_first(page, spec, scope=None, params=None):
    """按 spec['strategies'] 顺序尝试，返回 (locator, strategy_name)。
    全部失败返回 (None, DEFAULT_STRATEGY)。"""
    params = params or {}
    for st in spec.get("strategies", []):
        try:
            loc = None
            kind = st.get("type")
            if kind == "css":
                sel = st.get("selector")
                ht = st.get("has_text")
                if ht and params:
                    ht = ht.format(**params)
                base = scope if scope is not None else page
                loc = base.locator(sel, has_text=ht) if ht else base.locator(sel)
            elif kind == "text":
                text = st.get("text", "")
                if params:
                    text = text.format(**params)
                base = scope if scope is not None else page
                loc = base.get_by_text(text, exact=bool(st.get("exact", True)))
            if loc is None:
                continue
            if await loc.count() > 0 and await loc.first.is_visible():
                return loc.first, st.get("name", DEFAULT_STRATEGY)
        except Exception:
            continue
    return None, DEFAULT_STRATEGY


def first_strategy_name(spec):
    strategies = spec.get("strategies", [])
    return strategies[0].get("name", DEFAULT_STRATEGY) if strategies else DEFAULT_STRATEGY


def is_fallback(spec, strategy):
    return strategy != first_strategy_name(spec)


async def check_fingerprint(page, required):
    """返回 (found, missing)。required 为结构文本列表，不比对数据数值。"""
    missing = []
    for text in required:
        try:
            if await page.get_by_text(text, exact=True).count() == 0:
                missing.append(text)
        except Exception:
            missing.append(text)
    found = [t for t in required if t not in missing]
    return found, missing


def read_fingerprint(path):
    return load_json(path, default={"required": []}).get("required", [])


def write_fingerprint(path, required):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"required": required}, f, ensure_ascii=False, indent=2)
    return path


# 产出契约（阶段一：抖音 xlsx 最小校验——必备列名 + 最低行数）
CONTRACTS = {
    "douyin_play_data": {
        "required_columns": [
            "日期", "投稿量", "总播放量", "总点赞量", "总分享量", "总评论量",
            "5秒完播率", "2秒跳出率", "封面点击率", "平均播放时长",
        ],
        "min_rows": 2,
    },
    "douyin_video_list": {
        "required_columns": ["作品名称", "发布时间", "体裁", "审核状态", "播放量"],
        "min_rows": 2,
    },
    "xhs_overview_watch": {
        "required_columns": ["指标", "数值"],
        "first_column_contains": ["观看"],
        "min_rows": 2,
    },
    "xhs_overview_interact": {
        "required_columns": ["指标", "数值"],
        "first_column_contains": ["点赞"],
        "min_rows": 2,
    },
    "xhs_overview_fans_growth": {
        "required_columns": ["指标", "数值"],
        "first_column_contains": ["净涨粉"],
        "min_rows": 2,
    },
    "xhs_overview_publish": {
        "required_columns": ["指标", "数值"],
        "first_column_contains": ["总发布"],
        "min_rows": 2,
    },
    "xhs_content_analysis": {
        "required_columns": ["笔记标题", "观看量"],
        "header_row": 1,
        "min_rows": 2,
    },
    "wechat_video_all": {
        "required_columns": ["时间", "播放", "推荐", "喜欢", "评论", "分享", "关注"],
        "header_row": 2,
        "min_rows": 2,
    },
    "wechat_follower": {
        "required_columns": ["时间", "净增关注", "新增关注", "取消关注", "关注者总数"],
        "header_row": 2,
        "min_rows": 2,
    },
    "wechat_video_single": {
        "required_columns": ["视频描述", "视频ID", "发布时间", "播放量"],
        "header_row": 0,
        "min_rows": 2,
    },
    "wechat_source_dist": {
        "required_contains": ["来源分布", "合计"],
    },
    "wechat_fans_portrait": {
        "required_contains": ["【视频号关注者画像】", "【年龄分布】", "【性别分布】", "【设备分布】"],
    },
}


def _xhs_content_records(header, rows):
    """One actual note is enough; blank/hint/summary rows are not note records."""
    columns = [str(c).strip() if c is not None else "" for c in header]
    title_index, views_index = columns.index("笔记标题"), columns.index("观看量")
    published_index = columns.index("首次发布时间") if "首次发布时间" in columns else None
    answer = []
    for row in rows:
        title = str(row[title_index]).strip() if len(row) > title_index and row[title_index] is not None else ""
        if re.fullmatch(r"(?:暂无(?:笔记)?数据|合计|总计|汇总)(?:\s+.*|\s*[：:（(].*)?", title):
            continue
        views = row[views_index] if len(row) > views_index else None
        numeric_views = (isinstance(views, (int, float)) and not isinstance(views, bool) and 0 <= views < float("inf")) or (
            isinstance(views, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", views.strip()) is not None)
        has_publication = False
        if published_index is not None and len(row) > published_index:
            published = row[published_index]
            has_publication = isinstance(published, datetime)
            if isinstance(published, str):
                for date_format in ("%Y年%m月%d日%H时%M分%S秒", "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日",
                                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d"):
                    try:
                        datetime.strptime(published.strip(), date_format)
                        has_publication = True
                        break
                    except ValueError:
                        pass
        if numeric_views and (title or has_publication):
            answer.append(row)
    return answer


def validate_xlsx_contract(path, contract_key):
    spec = CONTRACTS.get(contract_key)
    if not spec:
        return {"ok": False, "reason": f"未知契约 {contract_key}"}
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as exc:
        return {"ok": False, "reason": f"无法打开: {exc}"}
    header_row = spec.get("header_row", 0)
    if len(rows) <= header_row:
        return {"ok": False, "reason": f"缺表头行（header_row={header_row}）"}
    data_rows = rows[header_row + 1:]
    header = [str(c).strip() for c in (rows[header_row] or []) if c not in (None, "")]
    missing = [c for c in spec.get("required_columns", []) if c not in header]
    if missing:
        return {"ok": False, "reason": f"缺列: {missing}"}
    if contract_key == "xhs_content_analysis":
        data_rows = _xhs_content_records(rows[header_row], data_rows)
        if not data_rows:
            return {"ok": False, "reason": "未发现可核对的笔记记录（仅空行、提示或汇总）", "retryable": False}
    first_col = [str(r[0]).strip() for r in data_rows if r and r[0] not in (None, "")]
    missing_metric = [m for m in spec.get("first_column_contains", []) if m not in first_col]
    if missing_metric:
        return {"ok": False, "reason": f"首列缺指标: {missing_metric}"}
    minimum = 1 if contract_key == "xhs_content_analysis" else spec.get("min_rows", 2)
    if len(data_rows) < minimum:
        return {"ok": False, "reason": f"数据行 {len(data_rows)} < 最低 {minimum}"}
    return {"ok": True, "rows": len(rows), "required_columns": spec.get("required_columns", [])}


def csv_publication_date(value):
    """Accept slash/hyphen dates, one/two-digit fields and space/T times."""
    match = re.match(r'^(\d{4})([-/])(\d{1,2})\2(\d{1,2})(?=$|[\sT])', value.strip())
    if not match:
        return None
    year, separator, month, day = match.groups()
    try:
        date = datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return date.strftime(f'%Y{separator}%m{separator}%d')


def csv_data_rows(header, rows):
    """Count records, preserving notes without counting them as video data."""
    header = [cell.strip() for cell in header]
    nonempty = [row for row in rows if any(cell.strip() for cell in row)]
    if '发布时间' not in header:
        return nonempty
    column = header.index('发布时间')
    return [row for row in nonempty if len(row) > column
            and csv_publication_date(row[column]) is not None]


def validate_csv_contract(path, contract_key):
    """CSV 契约：找表头行（默认 0，可配 header_row），校验必备列 + 最低数据行数。"""
    spec = CONTRACTS.get(contract_key)
    if not spec:
        return {"ok": False, "reason": f"未知契约 {contract_key}"}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline='') as f:
            rows = [row for row in csv.reader(f) if any(cell.strip() for cell in row)]
    except Exception as exc:
        return {"ok": False, "reason": f"无法读取: {exc}"}
    header_row = spec.get("header_row", 0)
    if len(rows) <= header_row:
        return {"ok": False, "reason": f"缺表头行（header_row={header_row}）"}

    header = [cell.strip() for cell in rows[header_row]]
    missing = [c for c in spec.get("required_columns", []) if c not in header]
    if missing:
        return {"ok": False, "reason": f"缺列: {missing}"}
    data_rows = rows[header_row + 1:]
    # Publication-date record recognition belongs to the single-video report.
    if contract_key == 'wechat_video_single':
        data_rows = csv_data_rows(header, data_rows)
    if len(data_rows) < spec.get("min_rows", 2):
        return {"ok": False, "reason": f"数据行 {len(data_rows)} < 最低 {spec.get('min_rows', 2)}"}
    return {"ok": True, "rows": len(data_rows) + 1, "required_columns": spec.get("required_columns", [])}


def validate_txt_contract(path, contract_key):
    """TXT 契约：校验必备段落/关键词存在。"""
    spec = CONTRACTS.get(contract_key)
    if not spec:
        return {"ok": False, "reason": f"未知契约 {contract_key}"}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except Exception as exc:
        return {"ok": False, "reason": f"无法读取: {exc}"}
    missing = [k for k in spec.get("required_contains", []) if k not in text]
    if missing:
        return {"ok": False, "reason": f"缺关键段: {missing}"}
    return {"ok": True, "rows": None, "required_columns": []}


def validate_file_contract(path, contract_key):
    """按文件后缀自动分派契约校验。"""
    if contract_key not in CONTRACTS:
        return {"ok": False, "reason": f"未知契约 {contract_key}"}
    low = path.lower()
    if low.endswith(".xlsx"):
        return validate_xlsx_contract(path, contract_key)
    if low.endswith(".csv"):
        return validate_csv_contract(path, contract_key)
    if low.endswith(".txt"):
        return validate_txt_contract(path, contract_key)
    return {"ok": False, "reason": f"不支持的文件类型: {os.path.basename(path)}"}


def save_diag(out_dir, platform, label, screenshot_path=None, page_text=None, strategies=None, error=None):
    """L4 诊断包：截图 + 页面文本片段 + 策略与失败原因，仅存本地 output/diag/。

    截图由调用方（异步上下文）先保存，再把路径传入；本函数只做归档与留痕。
    """
    import shutil
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    diag_root = os.path.join(out_dir, "diag")
    entry = os.path.join(diag_root, f"{platform}_{label}_{ts}")
    os.makedirs(entry, exist_ok=True)
    if screenshot_path and os.path.exists(screenshot_path):
        shutil.copyfile(screenshot_path, os.path.join(entry, "screenshot.png"))
    if page_text:
        with open(os.path.join(entry, "page_text.txt"), "w", encoding="utf-8") as f:
            f.write(page_text)
    with open(os.path.join(entry, "context.json"), "w", encoding="utf-8") as f:
        json.dump({
            "platform": platform,
            "label": label,
            "at": datetime.now().isoformat(timespec="seconds"),
            "strategies": strategies,
            "error": error,
        }, f, ensure_ascii=False, indent=2)
    # 保留最近 10 份，更早的转 _archive/（归档不删）
    entries = sorted([e for e in os.listdir(diag_root)
                      if os.path.isdir(os.path.join(diag_root, e)) and not e.startswith("_")])
    if len(entries) > 10:
        archive = os.path.join(diag_root, "_archive")
        os.makedirs(archive, exist_ok=True)
        for old in entries[: len(entries) - 10]:
            src = os.path.join(diag_root, old)
            dst = os.path.join(archive, old)
            if not os.path.exists(dst):
                shutil.move(src, dst)
    return entry


def write_manifest(out_dir, platform, items, summary, error=None):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex
    path = os.path.join(out_dir, f"export_manifest_{platform}_{ts}.json")
    data = {
        "platform": platform,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "items": items,
        "summary": summary,
    }
    if error:
        data["error"] = error
    with open(path, "x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path
