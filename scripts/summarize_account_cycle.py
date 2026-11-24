"""Read explicitly bound Douyin XLSX exports into cycle facts, never report prose.

No collection, network, model calls, spreadsheet writes, or inference of account
identity. A local binding ties the selected file hashes to an agent-verified
account. Blank cells remain unknown; duplicate dates and corrupt counts fail.
"""
import argparse
import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

COUNTS = {
    "posts": ("投稿量", "posts", "条"),
    "plays": ("总播放量", "total_plays", "次"),
    "likes": ("总点赞量", "total_likes", "次"),
    "shares": ("总分享量", "total_shares", "次"),
    "comments": ("总评论量", "total_comments", "次"),
}
RATES = {"completion_5s": "5秒完播率", "exit_2s": "2秒跳出率",
         "cover_click": "封面点击率", "avg_watch": "平均播放时长"}
EXTRA = {"favorites": "total_favorites", "profile_visits": "profile_visits",
         "new_follows": "new_follows"}
WORK_COUNTS = {"plays": "播放量", "likes": "点赞量", "shares": "分享量",
               "comments": "评论量", "favorites": "收藏量",
               "profile_visits": "主页访问量", "new_follows": "粉丝增量"}
WORK_RATES = {"completion_rate_pct": "完播率", "retention_5s_pct": "5s完播率",
              "exit_2s_pct": "2s跳出率", "cover_click_pct": "封面点击率"}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError("Duplicate JSON key: " + k)
            out[k] = v
        return out
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("Expected an ISO date, got " + repr(value))
    return date.fromisoformat(value.strip().replace("/", "-").split(" ")[0].split("T")[0])


def days(start, end):
    return [start + timedelta(days=i) for i in range((end-start).days+1)]


def period_dates(kind, as_of, start=None, end=None):
    if kind == "custom":
        if not start or not end:
            raise ValueError("Custom period needs --start-date and --end-date")
        a, b = parse_date(start), parse_date(end)
    elif start or end:
        raise ValueError("Explicit dates require --period custom")
    elif kind in ("last7", "last30"):
        b = as_of - timedelta(days=1)
        a = b - timedelta(days=6 if kind == "last7" else 29)
    elif kind == "previous_week":
        b = as_of - timedelta(days=as_of.weekday()+1)
        a = b - timedelta(days=6)
    elif kind == "previous_month":
        b = as_of.replace(day=1) - timedelta(days=1)
        a = b.replace(day=1)
    else:
        raise ValueError("Unknown period")
    if a > b or b > as_of or (b-a).days > 365:
        raise ValueError("Invalid/future period, or period exceeds 366 days")
    return a, b


def integer(value, signed=False):
    if value is None or (isinstance(value, str) and value.strip() in ("", "-", "--", "—", "N/A")):
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a metric count")
    try:
        n = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation as e:
        raise ValueError("Invalid metric count: " + repr(value)) from e
    if not n.is_finite() or (n < 0 and not signed) or n != n.to_integral_value():
        raise ValueError("Count must be a finite nonnegative integer: " + repr(value))
    return int(n)


def normalized(value, percentage=False):
    if value is None or (isinstance(value, str) and value.strip() in ("", "-", "--", "—", "N/A")):
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a metric")
    raw = str(value).strip()
    has_pct = raw.endswith("%")
    try:
        n = Decimal(raw.rstrip("%s").strip())
    except InvalidOperation as e:
        raise ValueError("Invalid rate/duration: " + raw) from e
    if not n.is_finite() or n < 0:
        raise ValueError("Invalid rate/duration: " + raw)
    if percentage:
        if not has_pct:
            if n > 1:
                raise ValueError("Ambiguous numeric percentage without %: " + raw)
            n *= 100
        if n > 100:
            raise ValueError("Percentage exceeds 100")
    elif has_pct:
        raise ValueError("Duration cannot be a percentage")
    return float(n)


def read_table(path, required, account_name):
    from openpyxl import load_workbook
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("XLSX exceeds 32 MiB input limit")
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        matches = []
        for sheet in book:
            iterator = sheet.iter_rows(values_only=True)
            headers = list(next(iterator, ()))
            if required not in headers:
                continue
            active = [h for h in headers if h is not None]
            if len(active) != len(set(active)):
                raise ValueError("Duplicate column names")
            records = []
            for row_number, values in enumerate(iterator, 2):
                if not any(v is not None for v in values):
                    continue
                row = dict(zip(headers, values))
                for key in ("账号", "账号名称", "账号昵称"):
                    if row.get(key) not in (None, "", account_name):
                        raise ValueError("Account conflict in selected workbook")
                records.append((row_number, row))
            matches.append((sheet.title, headers, records))
        if len(matches) != 1:
            raise ValueError("Expected exactly one recognized worksheet: " + str(path))
        return matches[0]
    finally:
        book.close()


def read_daily(path, account_name, snapshot):
    if path is None:
        return {}, None
    sheet, headers, records = read_table(path, "日期", account_name)
    if not any(v[0] in headers for v in COUNTS.values()):
        raise ValueError("Daily sheet has no recognized additive metric")
    by_date = {}
    for row_number, row in records:
        day = parse_date(row.get("日期"))
        if day > snapshot:
            raise ValueError("Daily row is later than declared snapshot")
        if day in by_date:
            raise ValueError("Duplicate daily date: " + day.isoformat())
        metrics, issues = {}, []
        for k, v in COUNTS.items():
            try:
                metrics[k] = integer(row.get(v[0]))
            except ValueError as e:
                # Do not guess whether a negative count is a sentinel or net change.
                # Preserve raw cell and row, mark only this metric/date unknown.
                metrics[k] = None
                issues.append({"field": v[0], "reason": str(e)})
        by_date[day] = {"row": row_number, "values": metrics,
                        "rates": {k: row.get(h) for k, h in RATES.items()},
                        "raw": row, "issues": issues}
    if not by_date:
        raise ValueError("Header-only daily sheet is not verified zero data")
    return by_date, {"path": str(path), "sha256": sha256(path), "sheet": sheet,
                     "headers": headers, "records": len(records)}


def read_works(path, account_name, snapshot, top_count):
    if path is None:
        return None, None
    sheet, headers, records = read_table(path, "作品名称", account_name)
    if "播放量" not in headers:
        raise ValueError("Works sheet lacks plays column")
    works = []
    for row_number, row in records:
        if not row.get("作品名称"):
            raise ValueError("Works row lacks title")
        published = row.get("发布时间")
        if published not in (None, "") and parse_date(published) > snapshot:
            raise ValueError("Work publication is later than snapshot")
        item = {"title": row["作品名称"], "published_at": str(published) if published else None,
                "genre": row.get("体裁"), "status": row.get("审核状态"),
                "source_row": row_number}
        # 粉丝增量 is signed in real exports (net change), unlike engagement counts.
        item.update({k: integer(row.get(h), signed=(k == "new_follows")) for k, h in WORK_COUNTS.items()})
        item.update({k: normalized(row.get(h), percentage=True) for k, h in WORK_RATES.items()})
        item["avg_watch_seconds"] = normalized(row.get("平均播放时长"))
        item["note"] = "Accumulated through snapshot, NOT selected-period contribution."
        works.append(item)
    if not works:
        raise ValueError("Header-only works sheet is not verified empty data")
    ranked = sorted((w for w in works if w["plays"] is not None), key=lambda w: w["plays"], reverse=True)
    return {"snapshot_date": snapshot.isoformat(), "scope": "All source rows, including non-public statuses",
            "top_by_plays": ranked[:top_count],
            "period_filtered_note": "Cumulative candidate pool only; no work-level period increments available."}, {
                "path": str(path), "sha256": sha256(path), "sheet": sheet, "headers": headers,
                "records": len(records), "rows": [{"row": n, "raw": r} for n, r in records]}


def summarize(by_date, start, end, source_label, has_works):
    window = days(start, end)
    missing = [d.isoformat() for d in window if d not in by_date]
    covered = len(window)-len(missing)
    # Conservative union of the two design thresholds; never show a spurious trend.
    limited = len(missing) >= 4 or len(missing) > len(window)/2
    overall, coverage, subtotals = {}, [], {}
    series = {k: [by_date[d]["values"][k] if d in by_date else None for d in window] for k in COUNTS}
    for key, (_, field, unit) in COUNTS.items():
        values = [v for v in series[key] if v is not None]
        n = len(values)
        subtotal = sum(values) if values else None
        subtotals[key] = {"known_days": n, "known_days_subtotal": subtotal}
        complete = n == len(window)
        value = subtotal if n and not limited else None
        if key == "posts" and not complete:
            value = None  # Unknown full-period post count, even if known subtotal is 0.
        overall[field] = {"value": value, "unit": unit if value is not None else None,
                          "note": "Full-period total" if complete else f"Known-date subtotal only ({n}/{len(window)} days); not full-period total"}
        coverage.append({"metric_key": "posts_count" if key == "posts" else key,
                         "status": "available" if complete else "partial" if n else "unavailable",
                         "note": f"Known dates: {n}/{len(window)}"})
    plays = subtotals["plays"]
    value = plays["known_days_subtotal"] / plays["known_days"] if plays["known_days"] and not limited else None
    overall["avg_daily_plays"] = {"value": value, "unit": "次/天" if value is not None else None,
                                   "computed_from_days": plays["known_days"], "note": "Mean of observed dates only"}
    for key, field in EXTRA.items():
        overall[field] = {"value": None, "unit": None, "note": "Not present in this daily export"}
        coverage.append({"metric_key": key, "status": "unavailable", "note": "No period-level source"})
    for key in ("comment_text", "dm_count", "valid_consultations"):
        coverage.append({"metric_key": key, "status": "unavailable"})
    raw_rates = {key: [by_date[d]["rates"][key] if d in by_date else None for d in window] for key in RATES}
    return {
        "coverage": {"overall": "complete" if not missing else "partial" if covered else "degraded" if has_works else "unavailable",
                     "daily_coverage": {"days_covered": covered, "days_missing": len(missing), "missing_dates": missing,
                                        "source_type": "daily_report" if by_date else "work_accumulated_only" if has_works else "none",
                                        "source_label": source_label}, "metrics_coverage": coverage, "comparison_coverage": None},
        "overall": overall,
        "daily_series": {"dates": [d.isoformat() for d in window], "series": series, "rates_raw": raw_rates,
                         "rates_note": "Raw source values only; no aggregate rates or unweighted rate means"} if covered else None,
        "subtotals": subtotals, "chart_allowed": bool(covered and not limited),
    }


def verify_binding(binding, sources):
    if binding.get("platform") != "抖音" or not binding.get("account_name") or not binding.get("basis"):
        raise ValueError("Binding needs account_name, platform=抖音 and verified identity basis")
    declared = binding.get("sources", [])
    for path in sources:
        if not any(Path(s.get("path", "")).resolve() == path and s.get("sha256", "").lower() == sha256(path)
                   for s in declared):
            raise ValueError("Selected source is not bound to the confirmed account/hash")


def calculate(args):
    as_of = parse_date(args.as_of) if args.as_of else datetime.now(ZoneInfo(args.timezone)).date()
    snapshot = parse_date(args.snapshot_date)
    if snapshot > as_of:
        raise ValueError("Snapshot is later than --as-of")
    start, end = period_dates(args.period, as_of, args.start_date, args.end_date)
    paths = [p.resolve(strict=True) for p in (args.daily, args.works) if p is not None]
    if not paths:
        raise ValueError("At least one explicit source is required")
    binding = load_json(args.source_binding.resolve(strict=True))
    verify_binding(binding, paths)
    account = binding["account_name"]
    daily, daily_source = read_daily(args.daily.resolve() if args.daily else None, account, snapshot)
    works, works_source = read_works(args.works.resolve() if args.works else None, account, snapshot, args.top_count)
    label = args.daily.name if args.daily else args.works.name
    current = summarize(daily, start, end, label, works is not None)
    period = {"label": args.period, "timezone": args.timezone, "start_date": start.isoformat(),
              "end_date": end.isoformat(), "days_requested": (end-start).days+1}
    comparison = None
    previous = None
    if not args.skip_comparison:
        if bool(args.compare_start) != bool(args.compare_end):
            raise ValueError("Comparison needs both endpoints")
        if args.compare_start:
            a, b = parse_date(args.compare_start), parse_date(args.compare_end)
            kind = "custom"
        elif args.period == "previous_month":
            b = start - timedelta(days=1); a = b.replace(day=1); kind = "previous_natural_month"
        else:
            b = start - timedelta(days=1); a = b - timedelta(days=(end-start).days)
            kind = "previous_natural_week" if args.period == "previous_week" else "previous_equal_length"
        if a > b or b >= start or (b-a).days > 365:
            raise ValueError("Invalid or overlapping comparison period")
        previous = summarize(daily, a, b, label, works is not None)
        current_complete = current["coverage"]["daily_coverage"]["days_missing"] == 0
        prev_complete = previous["coverage"]["daily_coverage"]["days_missing"] == 0
        eligible = {}
        if current_complete and prev_complete and (b-a).days == (end-start).days:
            for key, (_, field, unit) in COUNTS.items():
                if current["subtotals"][key]["known_days"] == len(days(start, end)) and previous["subtotals"][key]["known_days"] == len(days(a, b)):
                    now = current["overall"][field]["value"]; old = previous["overall"][field]["value"]
                    eligible[field] = {"current": now, "previous": old, "change": now-old, "unit": unit}
        comparable = bool(eligible)
        reason = None if comparable else "Incomplete date/metric coverage or unequal period lengths; no comparison calculated"
        period["comparison"] = {"type": kind, "start_date": a.isoformat(), "end_date": b.isoformat(),
                                 "comparable": comparable, "incomparable_reason": reason}
        if comparable:
            current["coverage"]["comparison_coverage"] = previous["coverage"]["daily_coverage"]
            comparison = {"comparable": True, "incomparable_reason": None, "metrics": eligible,
                          "note": "Absolute changes only; no growth rates, including zero baselines"}
    facts = {"account_identity": {"name": account, "platform": binding["platform"], "verification": binding["basis"]},
             "period": period, "coverage": current["coverage"],
             "metrics": {"source": {"snapshot_date": snapshot.isoformat(), "daily_source": args.daily.name if args.daily else None,
                                     "works_source": args.works.name if args.works else None,
                                     "collected_at": binding.get("collected_at")},
                         "overall": current["overall"], "daily_series": current["daily_series"],
                         "work_accumulated": works, "comparison": comparison},
             "content": {"posts_in_period": current["overall"]["posts"]["value"], "groups": []},
             "status": "facts_only_pending_deepseek_prose", "chart_allowed": current["chart_allowed"],
             "source_warnings": [{"date": d.isoformat(), "row": r["row"], "issues": r["issues"]}
                                 for d,r in daily.items() if r["issues"]]}
    evidence = {"binding_file": str(args.source_binding.resolve()), "binding_sha256": sha256(args.source_binding),
                "daily_source": daily_source, "works_source": works_source,
                "daily_rows": [{"date": d.isoformat(), **r} for d, r in sorted(daily.items())],
                "observed_subtotals": current["subtotals"], "comparison_subtotals": previous["subtotals"] if previous else None,
                "rate_rule": "daily rates retained raw; numeric work rates in [0,1] multiplied by 100; no precision rounding",
                "network_requests": 0, "spreadsheets_modified": 0}
    return facts, evidence


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--daily", type=Path); p.add_argument("--works", type=Path)
    p.add_argument("--source-binding", type=Path, required=True)
    p.add_argument("--snapshot-date", required=True)
    p.add_argument("--period", choices=["last7", "previous_week", "last30", "previous_month", "custom"], default="previous_week")
    p.add_argument("--start-date"); p.add_argument("--end-date"); p.add_argument("--as-of")
    p.add_argument("--timezone", default="Asia/Shanghai")
    p.add_argument("--compare-start"); p.add_argument("--compare-end"); p.add_argument("--skip-comparison", action="store_true")
    p.add_argument("--top-count", type=int, default=5); p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        ZoneInfo(args.timezone)
        if not 1 <= args.top_count <= 20:
            raise ValueError("top-count must be 1..20")
        if args.skip_comparison and (args.compare_start or args.compare_end):
            raise ValueError("Cannot skip and specify comparison")
        if args.output.exists():
            raise FileExistsError("Use a new output directory")
        facts, evidence = calculate(args)
        args.output.mkdir(parents=True, exist_ok=False)
        def write(name, value):
            (args.output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")
        write("cycle_facts.json", facts); write("source_evidence.json", evidence)
        lines = ["# 数据核算表", "", "Technical facts only; not an analysis or report.", "",
                 f"Period: {facts['period']['start_date']} .. {facts['period']['end_date']}", "",
                 "| Metric | Known days | Known-date subtotal |", "|---|---:|---:|"]
        for key, v in evidence["observed_subtotals"].items():
            lines.append(f"| {key} | {v['known_days']} | {v['known_days_subtotal']} |")
        lines += ["", "Unknown dates/cells are not zero. Work values are cumulative through snapshot.",
                  "Exact file hashes, sheets, rows and raw cells: source_evidence.json."]
        (args.output/"数据核算表.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
        print("FACTS_READY " + str(args.output.resolve()))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as e:
        print("CYCLE_FACTS_ERROR: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
