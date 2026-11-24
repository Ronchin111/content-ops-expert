# -*- coding: utf-8 -*-
"""Thin orchestration layer over the existing Douyin cycle summarizer.

Reads a multi-platform binding (platform -> source files + binding json),
runs the existing summarize_account_cycle.py as a subprocess for Douyin with
identical period parameters, and reads non-Douyin platforms via explicit
per-platform field mapping tables (no silent re-interpretation).

Hard rules, inherited from the single-platform design:
- No collection, network, model calls, spreadsheet writes or cross-platform
  metric merging. Platform sections stay in their own units and definitions.
- Blank cells stay unknown; account identity comes only from the binding files.
- Output is facts only; report prose is written by the host model.

Output multi_cycle_facts.json layout:
  platform_sections[]        each platform's complete facts (embedded as-is)
  cross_platform_comparison  only same-definition additive metrics, each row
                             stating the per-platform source header
  data_boundaries            per-platform coverage gaps recorded honestly
"""

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import subprocess
import sys

# ---------------------------------------------------------------------------
# Platform registry. Douyin goes through the existing engine as a subprocess;
# every other platform is read here via an explicit mapping table so the
# mapping is reviewable in code, not implicit.
# ---------------------------------------------------------------------------

DOUYIN = "抖音"
XHS = "小红书"
WECHAT = "视频号"
WECHAT_GAP = "视频号缺口"

# XHS export: one workbook per dashboard group; trends are one sheet per
# metric with (日期, 数值) rows. All mapping is explicit below.
XHS_FILE_KEYS = ["watch", "interact", "fans_growth", "publish"]

# abstract metric key -> (file key, exact sheet name). Additive daily counts
# only. Rates and durations live in XHS_RAW_MAP and are never aggregated.
XHS_COUNT_MAP = {
    "impressions": ("watch", "曝光趋势"),
    "views": ("watch", "观看趋势"),
    "likes": ("interact", "点赞趋势"),
    "comments": ("interact", "评论趋势"),
    "favorites": ("interact", "收藏趋势"),
    "shares": ("interact", "分享趋势"),
    "new_follows": ("fans_growth", "新增关注趋势"),
    "unfollows": ("fans_growth", "取消关注趋势"),
    "net_follows": ("fans_growth", "净涨粉趋势"),
    "profile_visits": ("fans_growth", "主页访客趋势"),
    "posts": ("publish", "总发布趋势"),
}

# Raw per-day values (strings like "12%" / "18秒"), kept as-is, never summed.
XHS_RAW_MAP = {
    "cover_ctr": ("watch", "封面点击率趋势"),
    "avg_watch_seconds": ("watch", "平均观看时长趋势"),
    "total_watch_seconds": ("watch", "观看总时长趋势"),
    "completion_rate": ("watch", "视频完播率趋势"),
    "profile_follow_rate": ("fans_growth", "主页转粉率趋势"),
}

# Identity sheets (指标, 数值) holding the export window's declared totals,
# used to cross-check trend additivity (full-trend sum vs declared total).
XHS_IDENTITY_MAP = {
    "watch": "账号总体观看数据",
    "interact": "账号总体互动数据",
    "fans_growth": "账号总体涨粉数据",
    "publish": "账号总体发布数据",
}

# Wechat: contract headers from MediaExportTool adaptive.py. video_all and
# follower are daily trend CSVs (header row 3 after two preamble lines);
# source_dist/fans_portrait are text summaries. This reader parses the two
# additive CSVs; text files stay raw evidence, never metric sources.
WECHAT_VIDEO_ALL_COLUMNS = ["时间", "播放", "推荐", "喜欢", "评论", "分享", "关注"]
WECHAT_FOLLOWER_COLUMNS = ["时间", "净增关注", "新增关注", "取消关注", "关注者总数"]

# Wechat abstract metric key -> (file key, exact column header). Additive
# daily counts only. 关注者总数 is a cumulative snapshot per day, recorded
# raw, never summed.
WECHAT_COUNT_MAP = {
    "plays": ("video_all", "播放"),
    "recommends": ("video_all", "推荐"),
    "likes": ("video_all", "喜欢"),
    "comments": ("video_all", "评论"),
    "shares": ("video_all", "分享"),
    "new_follows": ("video_all", "关注"),
    "net_follows": ("follower", "净增关注"),
    "follow_gained": ("follower", "新增关注"),
    "follow_lost": ("follower", "取消关注"),
}
WECHAT_RAW_MAP = {
    "follower_total_snapshot": ("follower", "关注者总数"),
}

# Cross-platform comparable fields: same abstract definition, additive counts.
# Values are listed side-by-side WITH each platform's own source header; no
# ratios, no summing across platforms.
COMPARABLE_FIELDS = {
    "posts": {"label": "投稿量", "douyin": "投稿量", "xhs": "总发布", "wechat": None},
    "plays": {"label": "播放/观看量", "douyin": "总播放量", "xhs": "观看", "wechat": "播放"},
    "likes": {"label": "点赞", "douyin": "总点赞量", "xhs": "点赞", "wechat": "喜欢"},
    "comments": {"label": "评论", "douyin": "总评论量", "xhs": "评论", "wechat": "评论"},
    "shares": {"label": "分享", "douyin": "总分享量", "xhs": "分享", "wechat": "分享"},
}

# Fields with different definitions across platforms: per-platform only.
NON_COMPARABLE_NOTES = {
    "plays_definition": "抖音总播放量与小红书观看(曝光后开始播放)口径不同；绝对数分列展示，不换算、不比率高下",
    "completion_rate": "抖音5秒完播率与小红书视频完播率口径不同，各平台单列，不直接对比",
    "watch_duration": "抖音平均播放时长与小红书人均观看时长口径不同，各平台单列，不直接对比",
    "fans_definition": "抖音粉丝净增与小红书净涨粉口径不同，各平台单列，不直接对比",
    "posts_definition": "投稿量口径：抖音按日投稿，小红书总发布含视频与图文合计，分列展示",
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_json(path):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError("Duplicate JSON key: " + k)
            out[k] = v
        return out
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("Expected a date, got " + repr(value))
    raw = value.strip().replace("/", "-")
    # Support "2026年11月11日" used by XHS sheets.
    if "年" in raw:
        parts = raw.replace("日", "").split("年")
        y, rest = parts[0], parts[1]
        m, d = rest.split("月")
        return date(int(y), int(m), int(d))
    return date.fromisoformat(raw.split(" ")[0].split("T")[0])


def period_window(args, as_of):
    if args.period == "custom":
        if not args.start_date or not args.end_date:
            raise ValueError("Custom period needs --start-date and --end-date")
        a, b = parse_date(args.start_date), parse_date(args.end_date)
    elif args.start_date or args.end_date:
        raise ValueError("Explicit dates require --period custom")
    elif args.period in ("last7", "last30"):
        b = as_of - timedelta(days=1)
        a = b - timedelta(days=6 if args.period == "last7" else 29)
    elif args.period == "previous_week":
        b = as_of - timedelta(days=as_of.weekday()+1)
        a = b - timedelta(days=6)
    elif args.period == "previous_month":
        b = as_of.replace(day=1) - timedelta(days=1)
        a = b.replace(day=1)
    else:
        raise ValueError("Unknown period")
    if a > b or b > as_of or (b-a).days > 365:
        raise ValueError("Invalid or future period, or period exceeds 366 days")
    return a, b


def integer(value, signed=False):
    if value is None or (isinstance(value, str) and value.strip() in ("", "-", "--", "—", "N/A")):
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a metric count")
    from decimal import Decimal, InvalidOperation
    try:
        n = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as e:
        raise ValueError("Invalid metric count: " + repr(value)) from e
    if not n.is_finite() or (n < 0 and not signed) or n != n.to_integral_value():
        raise ValueError("Count must be a finite nonnegative integer: " + repr(value))
    return int(n)


# ---------------------------------------------------------------------------
# XHS reading: per-group workbooks, one trend sheet per metric.
# ---------------------------------------------------------------------------

def read_xhs_trend_sheet(book, sheet_name, workbook_path):
    if sheet_name not in book.sheetnames:
        raise ValueError(f"XHS sheet not found: {sheet_name} in {workbook_path.name}")
    sheet = book[sheet_name]
    rows = [r for r in sheet.iter_rows(values_only=True) if any(v is not None for v in r)]
    if len(rows) < 2:
        raise ValueError(f"XHS sheet has no data rows: {sheet_name}")
    if list(rows[0][:2]) != ["日期", "数值"]:
        raise ValueError(f"XHS trend sheet header mismatch: {sheet_name}: {rows[0][:2]}")
    by_date = {}
    for r in rows[1:]:
        day = parse_date(r[0])
        if day in by_date:
            raise ValueError(f"Duplicate XHS daily date: {day.isoformat()} in {sheet_name}")
        by_date[day] = r[1]
    if not by_date:
        raise ValueError(f"Header-only trend sheet is not verified zero data: {sheet_name}")
    return by_date


def read_xhs_identity_sheet(book, sheet_name, workbook_path):
    if sheet_name not in book.sheetnames:
        raise ValueError(f"XHS sheet not found: {sheet_name} in {workbook_path.name}")
    sheet = book[sheet_name]
    rows = [r for r in sheet.iter_rows(values_only=True) if any(v is not None for v in r)]
    if len(rows) < 2:
        raise ValueError(f"XHS identity sheet has no data: {sheet_name}")
    if list(rows[0][:2]) != ["指标", "数值"]:
        raise ValueError(f"XHS identity sheet header mismatch: {sheet_name}: {rows[0][:2]}")
    out = {}
    for r in rows[1:]:
        if r[0] is None:
            continue
        if r[0] in out:
            raise ValueError(f"Duplicate XHS identity metric: {r[0]}")
        out[str(r[0])] = r[1]
    return out


def verify_binding_sources(entry, paths):
    """P0-1 fix: enforce sources[{path,sha256}] verification BEFORE any
    business data is read. Mirrors the Douyin engine's verify_binding rules:
    every source file must be declared in the binding with a matching hash;
    a wrong or missing hash is a hard error, never silently bypassed."""
    binding = load_json(Path(entry["binding"]))
    declared = binding.get("sources", [])
    for path in paths:
        if not any(Path(s.get("path", "")).resolve() == path and
                   s.get("sha256", "").lower() == sha256(path)
                   for s in declared):
            raise ValueError(
                f"Selected source is not bound to the confirmed account/hash: {path}")


def read_xhs_platform(entry, start, end, snapshot):
    """Read one XHS binding entry into a platform facts section."""
    from openpyxl import load_workbook
    files = entry.get("files")
    if not files or any(k not in files for k in XHS_FILE_KEYS):
        raise ValueError("XHS entry needs 'files' with keys: " + ", ".join(XHS_FILE_KEYS))
    file_paths = {}
    books = {}
    try:
        for key in XHS_FILE_KEYS:
            path = Path(files[key]).resolve(strict=True)
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("XLSX exceeds 32 MiB input limit")
            file_paths[key] = path
        # Verify every bound source BEFORE opening business data (P0-1).
        verify_binding_sources(entry, [file_paths[k] for k in XHS_FILE_KEYS])
        for key in XHS_FILE_KEYS:
            books[key] = load_workbook(file_paths[key], read_only=True, data_only=True)

        counts = {}
        for key, (file_key, sheet_name) in XHS_COUNT_MAP.items():
            counts[key] = read_xhs_trend_sheet(books[file_key], sheet_name, file_paths[file_key])
        raw_series = {}
        for key, (file_key, sheet_name) in XHS_RAW_MAP.items():
            raw_series[key] = read_xhs_trend_sheet(books[file_key], sheet_name, file_paths[file_key])
        identity = {}
        for file_key, sheet_name in XHS_IDENTITY_MAP.items():
            identity[file_key] = read_xhs_identity_sheet(books[file_key], sheet_name, file_paths[file_key])

        # Cross-check: full-trend sum vs identity-sheet declared total. The
        # identity sheet covers the export's own window; a mismatch means the
        # trend rows are not additive or the export is inconsistent.
        cross_checks = []
        identity_pairs = [
            ("impressions", "watch", "曝光"),
            ("views", "watch", "观看"),
            ("likes", "interact", "点赞"),
            ("comments", "interact", "评论"),
            ("favorites", "interact", "收藏"),
            ("shares", "interact", "分享"),
            ("new_follows", "fans_growth", "新增关注"),
            ("unfollows", "fans_growth", "取消关注"),
            ("net_follows", "fans_growth", "净涨粉"),
            ("profile_visits", "fans_growth", "主页访客"),
            ("posts", "publish", "总发布"),
        ]
        for key, file_key, label in identity_pairs:
            declared_raw = identity[file_key].get(label)
            if declared_raw is None:
                cross_checks.append({"key": key, "identity_value": None,
                                     "trend_sum": None, "match": None,
                                     "note": "identity sheet lacks this total"})
                continue
            signed = key in ("net_follows", "unfollows")
            try:
                declared = integer(declared_raw, signed=signed)
            except ValueError:
                cross_checks.append({"key": key, "identity_value": str(declared_raw),
                                     "trend_sum": None, "match": None,
                                     "note": "identity value not an integer"})
                continue
            # P1-01 (GPT re-check): a placeholder declared total ('-', 'N/A', ...)
            # parses to None -- it is unknown, not a comparable total.
            if declared is None:
                cross_checks.append({"key": key, "identity_value": str(declared_raw),
                                     "trend_sum": None, "match": None,
                                     "note": "identity value is a placeholder (unknown)"})
                continue
            total, bad = 0, 0
            for v in counts[key].values():
                if v is None or (isinstance(v, str) and v.strip() == ""):
                    bad += 1  # blank cells stay unknown, never summed
                    continue
                try:
                    # P1-01 (GPT re-check): placeholder cells parse to None --
                    # count as unknown (bad), never into the total.
                    parsed = integer(v, signed=signed)
                    if parsed is None:
                        bad += 1
                    else:
                        total += parsed
                except ValueError:
                    bad += 1
            if bad:
                cross_checks.append({"key": key, "identity_value": declared,
                                     "trend_sum": None, "match": None,
                                     "note": f"{bad} non-integer trend cells"})
            elif total != declared:
                raise ValueError(
                    f"XHS additivity check failed for {key}: trend sum {total} != "
                    f"identity-sheet total {declared}")
            else:
                cross_checks.append({"key": key, "identity_value": declared,
                                     "trend_sum": total, "match": True})
    finally:
        for b in books.values():
            b.close()

    window = [start + timedelta(days=i) for i in range((end-start).days+1)]
    overall, subtotals, coverage, xhs_issues = {}, {}, [], []
    for key in XHS_COUNT_MAP:
        by_date = counts[key]
        missing = [d.isoformat() for d in window if d not in by_date]
        values, issues = [], []
        for d in window:
            if d not in by_date:
                continue
            raw = by_date[d]
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                issues.append({"date": d.isoformat(), "reason": "blank cell kept unknown"})
                continue
            # P1-01: placeholder parses to None -> unknown day, not a sum crash.
            parsed = integer(raw, signed=(key in ("net_follows", "unfollows")))
            if parsed is None:
                issues.append({"date": d.isoformat(), "reason": "placeholder cell kept unknown"})
                continue
            try:
                values.append(parsed)
            except ValueError as e:
                issues.append({"date": d.isoformat(), "reason": str(e)})
        xhs_issues += [{"metric_key": key, "date": i["date"], "reason": i["reason"]} for i in issues]
        n = len(values)
        subtotal = sum(values) if values else None
        subtotals[key] = {"known_days": n, "known_days_subtotal": subtotal}
        complete = n == len(window)
        value = subtotal if complete or (n and missing) else None
        if not n:
            value = None
        overall[key] = {"value": value, "unit": "次" if value is not None else None,
                        "note": "Full-period total" if complete
                                else f"Known-date subtotal only ({n}/{len(window)} days); "
                                     "not full-period total"}
        coverage.append({"metric_key": key,
                         "status": "available" if complete else "partial" if n else "unavailable",
                         "note": f"Known dates: {n}/{len(window)}"
                                 + (f"; missing {missing}" if missing else "")})
    trend_span = {}
    for key, by_date in counts.items():
        if by_date:
            trend_span[key] = [min(by_date).isoformat(), max(by_date).isoformat()]

    section = {
        "platform": XHS,
        "account_identity": _binding_identity(entry),
        "period": {"label": "custom", "start_date": start.isoformat(),
                   "end_date": end.isoformat(), "days_requested": len(window)},
        "coverage": {
            "overall": "complete" if all(c["status"] == "available" for c in coverage) else "partial",
            "metrics_coverage": coverage,
            "trend_days_available": {k: len(v) for k, v in counts.items()},
            "trend_span": trend_span,
        },
        "metrics": {
            "source": {
                "snapshot_date": snapshot.isoformat(),
                "daily_source": [file_paths[k].name for k in XHS_FILE_KEYS],
                "works_source": None,
                "collected_at": load_json(Path(entry["binding"])).get("collected_at"),
            },
            "overall": overall,
            "daily_series": {
                "dates": [d.isoformat() for d in window],
                "series": {k: [counts[k].get(d) if d in counts[k] else None
                               for d in window] for k in XHS_COUNT_MAP},
                "raw_series": {k: [v.get(d) if d in v else None
                                   for d in window] for k, v in raw_series.items()},
                "raw_series_note": "Rates/durations kept as raw source values; never aggregated",
            },
            "work_accumulated": None,
            "comparison": None,
        },
        "content": {"posts_in_period": overall.get("posts", {}).get("value"),
                    "groups": []},
        "xhs_notes": {
            "views_definition": "小红书「观看」=曝光后开始播放次数，与抖音「总播放量」口径不同",
            "completion_rate": "小红书「视频完播率」口径与抖音「5秒完播率」不同，只保留原始日值，不求和",
            "identity_cross_checks": cross_checks,
            "works_table": "内容分析表(笔记级累计)本周期未接入作品级映射，如需接入须单独扩展",
        },
        "chart_allowed": True,
        "source_warnings": xhs_issues,
    }
    return section


def _binding_identity(entry):
    binding = load_json(Path(entry["binding"]))
    name = binding.get("account_name")
    platform = binding.get("platform")
    if platform != entry.get("platform"):
        raise ValueError(f"Binding platform {platform} != entry platform {entry.get('platform')}")
    if not name or not binding.get("basis"):
        raise ValueError("Binding needs account_name and basis")
    return {"name": name, "platform": platform, "verification": binding["basis"]}


# ---------------------------------------------------------------------------
# Wechat reading: daily trend CSVs with two preamble lines before the header.
# ---------------------------------------------------------------------------

def read_wechat_csv(path, expected_columns):
    """Parse one wechat trend CSV: skip preamble, find header row, index by date."""
    import csv as _csv
    text = Path(path).read_text(encoding="utf-8-sig")
    rows = [r for r in text.splitlines() if r.strip()]
    header_idx = None
    for i, line in enumerate(rows):
        cells = [c.strip().strip('"') for c in next(_csv.reader([line]))]
        if cells[:len(expected_columns)] == expected_columns:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"Wechat CSV header row not found in {Path(path).name}")
    header_cells = [c.strip().strip('"') for c in next(_csv.reader([rows[header_idx]]))]
    by_date = {}
    for line in rows[header_idx+1:]:
        cells = [c.strip().strip('"') for c in next(_csv.reader([line]))]
        if not cells or not cells[0]:
            continue
        day = parse_date(cells[0])
        if day in by_date:
            raise ValueError(f"Duplicate wechat daily date: {day.isoformat()}")
        by_date[day] = cells
    if not by_date:
        raise ValueError(f"Header-only wechat CSV is not verified zero data: {Path(path).name}")
    return by_date, header_cells


def read_wechat_platform(entry, start, end, snapshot):
    """Read one wechat binding entry into a platform facts section."""
    files = entry.get("files")
    if not files or any(k not in files for k in ("video_all", "follower")):
        raise ValueError("Wechat entry needs 'files' with keys: video_all, follower")
    paths = {k: Path(files[k]).resolve(strict=True) for k in ("video_all", "follower")}
    for k in ("video_all", "follower"):
        if paths[k].stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Input exceeds 32 MiB limit: " + paths[k].name)
    # Verify every bound source BEFORE reading business data (P0-1).
    verify_binding_sources(entry, list(paths.values()))
    tables = {}
    headers = {}
    for key, columns in (("video_all", WECHAT_VIDEO_ALL_COLUMNS),
                         ("follower", WECHAT_FOLLOWER_COLUMNS)):
        tables[key], headers[key] = read_wechat_csv(paths[key], columns)

    def cell_value(file_key, column, day):
        cells = tables[file_key].get(day)
        if cells is None:
            return None
        try:
            idx = headers[file_key].index(column)
        except ValueError:
            return None
        return cells[idx] if idx < len(cells) else None

    window = [start + timedelta(days=i) for i in range((end-start).days+1)]
    overall, subtotals, coverage, all_issues = {}, {}, [], []
    for key, (file_key, column) in WECHAT_COUNT_MAP.items():
        values, issues, missing = [], [], []
        for d in window:
            raw = cell_value(file_key, column, d)
            if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                missing.append(d.isoformat())
                continue
            # P1-01: placeholder ('-', 'N/A', ...) parses to None; keep it an
            # unknown day (no known_days credit), never a crash in sum().
            parsed = integer(raw, signed=(key in ("net_follows", "follow_lost")))
            if parsed is None:
                missing.append(d.isoformat())
                continue
            try:
                # net_follows/follow_lost are legitimately signed in the CSV.
                values.append(parsed)
            except ValueError as e:
                issues.append({"date": d.isoformat(), "reason": str(e)})
        all_issues += issues
        n = len(values)
        subtotal = sum(values) if values else None
        subtotals[key] = {"known_days": n, "known_days_subtotal": subtotal}
        complete = n == len(window)
        value = subtotal if complete else None
        overall[key] = {"value": value, "unit": "次" if value is not None else None,
                        "note": "Full-period total" if complete
                                else f"Known-date subtotal only ({n}/{len(window)} days); "
                                     "not full-period total"}
        coverage.append({"metric_key": key,
                         "status": "available" if complete else "partial" if n else "unavailable",
                         "note": f"Known dates: {n}/{len(window)}"
                                 + (f"; missing {missing}" if missing else "")})
    raw_snapshots = {k: [cell_value(fk, col, d) for d in window]
                     for k, (fk, col) in WECHAT_RAW_MAP.items()}
    trend_span = {}
    for file_key, table in tables.items():
        if table:
            trend_span[file_key] = [min(table).isoformat(), max(table).isoformat()]

    section = {
        "platform": WECHAT,
        "account_identity": _binding_identity(entry),
        "period": {"label": "custom", "start_date": start.isoformat(),
                   "end_date": end.isoformat(), "days_requested": len(window)},
        "coverage": {
            "overall": "complete" if all(c["status"] == "available" for c in coverage) else "partial",
            "metrics_coverage": coverage,
            "trend_span": trend_span,
        },
        "metrics": {
            "source": {
                "snapshot_date": snapshot.isoformat(),
                "daily_source": [paths[k].name for k in ("video_all", "follower")],
                "works_source": None,
                "collected_at": load_json(Path(entry["binding"])).get("collected_at"),
            },
            "overall": overall,
            "daily_series": {
                "dates": [d.isoformat() for d in window],
                "series": {k: [cell_value(fk, col, d) for d in window]
                           for k, (fk, col) in WECHAT_COUNT_MAP.items()},
                "raw_series": raw_snapshots,
                "raw_series_note": "关注者总数为每日累计快照，保留原值，不求和",
            },
            "work_accumulated": None,
            "comparison": None,
        },
        "content": {"posts_in_period": None, "groups": [],
                    "note": "视频号本轮导出无单篇视频明细（近30天暂无视频）；投稿量无日度来源，记未知不填0"},
        "wechat_notes": {
            "plays_definition": "视频号「播放」口径与抖音「总播放量」、小红书「观看」均不同，绝对数分列，不换算",
            "source_dist": "来源分布txt(近7天窗口)与粉丝画像txt为文本快照，留原始证据，不混入周期发生量",
            "no_single_video": "单篇视频导出显示「近30天平台暂无视频」，作品级数据本轮缺失",
        },
        "chart_allowed": True,
        "source_warnings": [{"date": i["date"], "row": None, "issues": [i]}
                            for i in all_issues],
    }
    return section


# ---------------------------------------------------------------------------
# Douyin: subprocess over the existing engine, identical computed window.
# ---------------------------------------------------------------------------

def run_douyin(entry, start, end, comp_start, comp_end, snapshot, skip_comparison, output_dir):
    engine = Path(__file__).resolve().parent / "summarize_account_cycle.py"
    if not engine.exists():
        raise ValueError("Douyin engine not found: " + str(engine))
    out_dir = Path(output_dir) / "douyin_cycle"
    if out_dir.exists():
        raise FileExistsError("Douyin output directory already exists: " + str(out_dir))
    cmd = [sys.executable, str(engine),
           "--daily", str(Path(entry["daily"])),
           "--source-binding", str(Path(entry["binding"])),
           "--snapshot-date", snapshot.isoformat(),
           "--period", "custom",
           "--start-date", start.isoformat(),
           "--end-date", end.isoformat(),
           "--output", str(out_dir)]
    if entry.get("works"):
        cmd += ["--works", str(Path(entry["works"]))]
    if skip_comparison:
        cmd += ["--skip-comparison"]
    else:
        cmd += ["--compare-start", comp_start.isoformat(),
                "--compare-end", comp_end.isoformat()]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise ValueError("Douyin engine failed: " + (proc.stderr or proc.stdout).strip())
    facts = load_json(out_dir / "cycle_facts.json")
    evidence = load_json(out_dir / "source_evidence.json")
    return facts, evidence


def read_douyin_history(daily_path, weeks, as_of):
    """Read the Douyin daily xlsx directly for historical weekly sums.

    Bypasses the single-platform engine (which is period-parameterised per
    run); the daily table's additive columns are summed per natural week for
    the last `weeks` weeks ending Sunday. Only counts are touched — rates
    stay raw. Works read-only, same source file as the engine input.
    """
    from openpyxl import load_workbook
    path = Path(daily_path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("XLSX exceeds 32 MiB input limit: " + path.name)
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book["Sheet1"]
        rows = [r for r in sheet.iter_rows(values_only=True) if any(v is not None for v in r)]
        if len(rows) < 2:
            raise ValueError("Header-only daily sheet is not verified zero data")
        headers = list(rows[0])
        col = {h: i for i, h in enumerate(headers)}
        needed = {"日期": 0, "投稿量": None, "总播放量": None, "总点赞量": None,
                  "总分享量": None, "总评论量": None}
        for h in needed:
            if h in col:
                needed[h] = col[h]
        by_date = {}
        for r in rows[1:]:
            day = parse_date(r[0])
            if day in by_date:
                raise ValueError(f"Duplicate daily date: {day.isoformat()} in douyin history")
            by_date[day] = r
        # Natural weeks ending Sunday, most recent first.
        last_sunday = as_of - timedelta(days=as_of.weekday()+1)
        out = []
        for w in range(weeks):
            end_w = last_sunday - timedelta(days=7*w)
            start_w = end_w - timedelta(days=6)
            days_w = [start_w + timedelta(days=i) for i in range(7)]
            sums = {}
            for metric, idx in needed.items():
                if metric == "日期" or idx is None:
                    continue
                vals = []
                for d in days_w:
                    row = by_date.get(d)
                    if row is None:
                        continue
                    v = row[idx]
                    if v is None:
                        continue
                    # P1-01: placeholder parses to None -> unknown day, not a sum crash.
                    parsed = integer(v)
                    if parsed is None:
                        continue
                    vals.append(parsed)
                sums[metric] = {"known_days": len(vals),
                                "total": sum(vals) if vals and len(vals) == 7 else None,
                                "subtotal_known_days": sum(vals) if vals else None}
            out.append({"week_start": start_w.isoformat(), "week_end": end_w.isoformat(),
                        "metrics": sums})
        return out
    finally:
        book.close()


def read_xhs_history(entry, weeks, as_of):
    """Historical weekly sums from the already-bound XHS workbooks."""
    from openpyxl import load_workbook
    files = entry.get("files")
    if not files or any(k not in files for k in XHS_FILE_KEYS):
        raise ValueError("XHS entry needs 'files' for history")
    books = {}
    try:
        for key in XHS_FILE_KEYS:
            hp = Path(files[key]).resolve(strict=True)
            if hp.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("XLSX exceeds 32 MiB input limit: " + hp.name)
            books[key] = load_workbook(hp, read_only=True, data_only=True)
        counts = {}
        for key, (file_key, sheet_name) in XHS_COUNT_MAP.items():
            counts[key] = read_xhs_trend_sheet(books[file_key], sheet_name, Path(files[file_key]))
        last_sunday = as_of - timedelta(days=as_of.weekday()+1)
        out = []
        for w in range(weeks):
            end_w = last_sunday - timedelta(days=7*w)
            start_w = end_w - timedelta(days=6)
            days_w = [start_w + timedelta(days=i) for i in range(7)]
            sums = {}
            for key in XHS_COUNT_MAP:
                vals = []
                for d in days_w:
                    v = counts[key].get(d)
                    if v is None:
                        continue
                    # P1-01: placeholder parses to None -> unknown day, not a sum crash.
                    parsed = integer(v, signed=(key in ("net_follows", "unfollows")))
                    if parsed is None:
                        continue
                    vals.append(parsed)
                sums[key] = {"known_days": len(vals),
                             "total": sum(vals) if vals and len(vals) == 7 else None,
                             "subtotal_known_days": sum(vals) if vals else None}
            out.append({"week_start": start_w.isoformat(), "week_end": end_w.isoformat(),
                        "metrics": sums})
        return out
    finally:
        for b in books.values():
            b.close()


def read_wechat_history(entry, weeks, as_of):
    """Historical weekly sums from the bound wechat CSVs."""
    files = entry.get("files")
    if not files or any(k not in files for k in ("video_all", "follower")):
        raise ValueError("Wechat entry needs 'files' for history")
    tables, headers = {}, {}
    for key, columns in (("video_all", WECHAT_VIDEO_ALL_COLUMNS),
                         ("follower", WECHAT_FOLLOWER_COLUMNS)):
        hp = Path(files[key]).resolve(strict=True)
        if hp.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Input exceeds 32 MiB limit: " + hp.name)
        tables[key], headers[key] = read_wechat_csv(hp, columns)

    def cell(file_key, column, day):
        cells = tables[file_key].get(day)
        if cells is None:
            return None
        try:
            idx = headers[file_key].index(column)
        except ValueError:
            return None
        return cells[idx] if idx < len(cells) else None

    last_sunday = as_of - timedelta(days=as_of.weekday()+1)
    out = []
    for w in range(weeks):
        end_w = last_sunday - timedelta(days=7*w)
        start_w = end_w - timedelta(days=6)
        days_w = [start_w + timedelta(days=i) for i in range(7)]
        sums = {}
        for key, (fk, column) in WECHAT_COUNT_MAP.items():
            vals, bad = [], 0
            for d in days_w:
                raw = cell(fk, column, d)
                # blank/None stay unknown; signed for net_follows/follow_lost.
                if raw is None or (isinstance(raw, str) and raw.strip() == ""):
                    continue
                # P1-01: placeholder parses to None -> unknown day, not a sum crash.
                parsed = integer(raw, signed=(key in ("net_follows", "follow_lost")))
                if parsed is None:
                    continue
                vals.append(parsed)
            sums[key] = {"known_days": len(vals),
                         "total": sum(vals) if vals and len(vals) == 7 else None,
                         "subtotal_known_days": sum(vals) if vals else None,
                         "invalid_cells": bad}
        out.append({"week_start": start_w.isoformat(), "week_end": end_w.isoformat(),
                    "metrics": sums})
    return out


def comparison_window(args, start, end):
    if args.skip_comparison:
        return None, None
    if args.compare_start:
        if not args.compare_end:
            raise ValueError("Comparison needs both endpoints")
        a, b = parse_date(args.compare_start), parse_date(args.compare_end)
    else:
        b = start - timedelta(days=1)
        a = b - timedelta(days=(end-start).days)
    if a > b or b >= start or (b-a).days > 365:
        raise ValueError("Invalid or overlapping comparison period")
    return a, b


# ---------------------------------------------------------------------------
# Cross-platform comparison: only same-definition additive metrics.
# ---------------------------------------------------------------------------

def full_period(m):
    """P0-3: only full-period totals may enter the comparison layer; a
    known-date subtotal from partial coverage must NOT be presented as a
    period total. Readers mark full coverage with a fixed note prefix."""
    return bool(m) and m.get("value") is not None and (
        m.get("note") or "").startswith("Full-period total")


def build_comparison(sections):
    rows = []
    for key, spec in COMPARABLE_FIELDS.items():
        row = {"metric": key, "label": spec["label"],
               "per_platform": {}}
        for section in sections:
            platform = section["platform"]
            if platform == DOUYIN:
                field = {"posts": "posts", "plays": "total_plays", "likes": "total_likes",
                         "comments": "total_comments", "shares": "total_shares"}[key]
                m = section["metrics"]["overall"].get(field)
                if full_period(m):
                    row["per_platform"][platform] = {
                        "value": m["value"], "source_header": spec["douyin"],
                        "note": m.get("note", "")}
            elif platform == XHS:
                # XHS abstract keys differ: "plays" is stored as "views".
                field = {"posts": "posts", "plays": "views", "likes": "likes",
                         "comments": "comments", "shares": "shares"}[key]
                m = section["metrics"]["overall"].get(field)
                if full_period(m):
                    row["per_platform"][platform] = {
                        "value": m["value"], "source_header": spec["xhs"],
                        "note": m.get("note", "")}
            elif platform == WECHAT:
                # posts has no wechat source (kept None); other keys map 1:1.
                field = {"posts": "posts", "plays": "plays", "likes": "likes",
                         "comments": "comments", "shares": "shares"}[key]
                m = section["metrics"]["overall"].get(field)
                if full_period(m) and spec.get("wechat"):
                    row["per_platform"][platform] = {
                        "value": m["value"], "source_header": spec["wechat"],
                        "note": m.get("note", "")}
            # WECHAT: no data this run; recorded in data_boundaries, not faked.
        rows.append(row)
    return {
        "comparable_fields": rows,
        "method": "Side-by-side absolute counts per platform under each platform's own header; "
                  "no cross-platform sum, no ratios, no conversion",
        "non_comparable": NON_COMPARABLE_NOTES,
    }


def build_boundaries(sections, wechat_entry):
    boundaries = []
    for section in sections:
        notes = []
        cov = section.get("coverage", {})
        if cov.get("overall") != "complete":
            notes.append(f"metrics coverage: {cov.get('overall')}")
        if section["platform"] == XHS:
            span = section["coverage"].get("trend_span", {}).get("views")
            notes.append(f"小红书趋势数据仅覆盖自身导出窗口（观看趋势 {span}）；窗口外日期不可得，缺失≠零")
            notes.append("内容分析表为笔记级累计，未接入周期发生量映射")
        if section["platform"] == WECHAT:
            notes.append("视频号投稿量无日度来源（单篇视频导出显示近30天暂无视频），记未知不填0")
            notes.append("来源分布/粉丝画像txt为文本快照（近7天窗口/导出日），未混入周期发生量")
        if notes:
            boundaries.append({"platform": section["platform"],
                               "section_present": True, "issues": notes})
    if wechat_entry is not None:
        boundaries.append({
            "platform": WECHAT,
            "section_present": False,
            "reason": "本次未取得可用的视频号数据；"
                      "需按本次回执确认原因后重试",
            "expected_columns": WECHAT_VIDEO_ALL_COLUMNS,
            "expected_files": "wechat_video_all/follower/video_single/source_dist/fans_portrait 5表",
        })
    return boundaries


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--platform-bindings", type=Path, required=True)
    p.add_argument("--snapshot-date", required=True)
    p.add_argument("--period", choices=["last7", "previous_week", "last30",
                                        "previous_month", "custom"], default="previous_week")
    p.add_argument("--start-date"); p.add_argument("--end-date")
    p.add_argument("--as-of")
    p.add_argument("--timezone", default="Asia/Shanghai")
    p.add_argument("--compare-start"); p.add_argument("--compare-end")
    p.add_argument("--skip-comparison", action="store_true")
    p.add_argument("--history-weeks", type=int, default=0,
                   help="Also compute the last N natural weeks (ending Sunday) per platform "
                        "as self-reference baselines; 0 disables")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(args.timezone)
        snapshot = parse_date(args.snapshot_date)
        as_of = parse_date(args.as_of) if args.as_of else date.today()
        if snapshot > as_of:
            raise ValueError("Snapshot is later than --as-of")
        start, end = period_window(args, as_of)
        if args.skip_comparison and (args.compare_start or args.compare_end):
            raise ValueError("Cannot skip and specify comparison")

        bindings = load_json(args.platform_bindings.resolve(strict=True))
        if "platforms" not in bindings or not isinstance(bindings["platforms"], list):
            raise ValueError("platform-bindings needs a 'platforms' list")
        seen = set()
        for entry in bindings["platforms"]:
            plat = entry.get("platform")
            if plat in seen:
                raise ValueError("Duplicate platform entry: " + str(plat))
            seen.add(plat)

        if args.output.exists():
            raise FileExistsError("Output directory must be new: " + str(args.output))

        sections = []
        douyin_evidence = None
        wechat_entry = None
        comp_start, comp_end = comparison_window(args, start, end)
        for entry in bindings["platforms"]:
            plat = entry.get("platform")
            if plat == DOUYIN:
                facts, evidence = run_douyin(entry, start, end, comp_start, comp_end,
                                             snapshot, args.skip_comparison, args.output)
                # The single-platform engine stores platform under
                # account_identity; lift it to a top-level key for uniform
                # comparison/boundaries handling below.
                facts["platform"] = facts["account_identity"]["platform"]
                sections.append(facts)
                douyin_evidence = evidence
            elif plat == XHS:
                # XHS trend sheets carry their own export window; only an
                # explicit agent-chosen window keeps platform sections aligned.
                if args.period != "custom":
                    raise ValueError("XHS requires --period custom with explicit dates")
                sections.append(read_xhs_platform(entry, start, end, snapshot))
            elif plat == WECHAT:
                if args.period != "custom":
                    raise ValueError("Wechat requires --period custom with explicit dates")
                sections.append(read_wechat_platform(entry, start, end, snapshot))
                wechat_entry = None  # data present; gap record not needed
            elif plat == WECHAT_GAP:
                wechat_entry = entry
            else:
                raise ValueError("Unknown platform in bindings: " + str(plat))

        args.output.mkdir(parents=True, exist_ok=True)
        comparison = build_comparison(sections)
        boundaries = build_boundaries(sections, wechat_entry)

        history = None
        if args.history_weeks > 0:
            if not 1 <= args.history_weeks <= 12:
                raise ValueError("history-weeks must be 1..12")
            history = {"weeks_computed": args.history_weeks, "platforms": {}}
            for entry in bindings["platforms"]:
                plat = entry.get("platform")
                if plat == DOUYIN and entry.get("daily"):
                    history["platforms"][plat] = read_douyin_history(entry["daily"], args.history_weeks, as_of)
                elif plat == XHS:
                    history["platforms"][plat] = read_xhs_history(entry, args.history_weeks, as_of)
                elif plat == WECHAT and entry.get("files"):
                    history["platforms"][plat] = read_wechat_history(entry, args.history_weeks, as_of)

        multi = {
            "schema": "multi_cycle_facts_v1",
            "generated_note": "Facts only; report prose is written by the host model from these facts",
            "period": {"start_date": start.isoformat(), "end_date": end.isoformat(),
                       "days_requested": (end-start).days+1, "timezone": args.timezone},
            "platform_sections": sections,
            "cross_platform_comparison": comparison,
            "weekly_history": history,
            "data_boundaries": boundaries,
            "status": "facts_only_pending_prose",
        }
        (args.output / "multi_cycle_facts.json").write_text(
            json.dumps(multi, ensure_ascii=False, indent=2, default=str, allow_nan=False),
            encoding="utf-8")
        if douyin_evidence is not None:
            (args.output / "douyin_source_evidence.json").write_text(
                json.dumps(douyin_evidence, ensure_ascii=False, indent=2, default=str,
                           allow_nan=False), encoding="utf-8")

        print("MULTI_FACTS_READY " + str(args.output.resolve()))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as e:
        print("MULTI_FACTS_ERROR: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
