"""Render the DeepSeek account-cycle JSON model to PDF and Markdown.

Local, deterministic presentation only. Does not collect data, call models, fill
missing business values, or invent analysis. Validation never changes the input.
"""
import argparse
from datetime import date
from decimal import Decimal
from html import escape
import io
import json
import math
import re
from pathlib import Path, PureWindowsPath
import subprocess
import sys

import reportlab

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, LongTable,
                               TableStyle, PageBreak, Flowable)

PDF_NAME = "账号周期复盘.pdf"
MD_NAME = "账号周期复盘.md"
FONT, BOLD = "CycleYaHei", "CycleYaHeiBold"
# System emoji fallback (existing font, no installation). Used only for glyphs
# missing from Microsoft YaHei, e.g. U+1F4B0 in verbatim work titles.
EMOJI = "CycleEmoji"
EMOJI_FONT_FILE = Path(r"C:\Windows\Fonts\seguiemj.ttf")
WIDTH, HEIGHT = 595.28, 841.89
MARGIN = 18*mm
CONTENT_W = WIDTH-2*MARGIN
ACCENT = colors.HexColor("#1A3A34")
INK = colors.HexColor("#202B2A")
MUTED = colors.HexColor("#667B75")
PALE = colors.HexColor("#EEF4F1")
LINE = colors.HexColor("#D5E0DA")
# Verbatim existing word list. The protected renderer is never imported/changed.
REPLACEMENTS = {'本轮参考': '参考数据', '留住观看': '留住观众', '本作品': '这条视频',
                '同好': '朋友', '亲历': '真实经历', '本条': '这条视频', '本轮': '这次'}
LABELS = {"posts": "投稿量", "total_plays": "播放量", "avg_daily_plays": "日均播放",
          "total_likes": "点赞", "total_shares": "分享", "total_comments": "评论",
          "total_favorites": "收藏", "profile_visits": "主页访问", "new_follows": "新增关注"}
WORK_LABELS = {"plays": "播放量", "likes": "点赞", "shares": "分享", "comments": "评论",
               "favorites": "收藏", "profile_visits": "主页访问", "new_follows": "新增关注",
               "completion_rate_pct": "完播率", "retention_5s_pct": "5秒完播率",
               "exit_2s_pct": "2秒跳出率", "cover_click_pct": "封面点击率",
               "avg_watch_seconds": "平均观看时长"}


def prose(value, original=False):
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool)):
        raise ValueError("Expected text/number scalar")
    value = str(value)
    if not original:
        for old, new in REPLACEMENTS.items():
            value = value.replace(old, new)
    return value


def scalar_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def safe_attachment(base, value):
    if not isinstance(value, str) or not value or PureWindowsPath(value).drive or "\\" in value:
        raise ValueError("Attachment must be a slash-separated relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or ":" in value:
        raise ValueError("Attachment path escapes report directory")
    resolved = (base/path).resolve(strict=True)
    if not resolved.is_relative_to(base.resolve()) or not resolved.is_file():
        raise ValueError("Attachment must be an existing file within report directory")
    return resolved


def load_data(path):
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError("Duplicate JSON key: " + k)
            out[k] = v
        return out
    def bad_constant(value):
        raise ValueError("Non-finite JSON number: " + value)
    data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=pairs, parse_constant=bad_constant)
    validate(data, path.parent)
    return data


def validate(data, base):
    def finite_tree(value):
        if isinstance(value, dict):
            for v in value.values():
                finite_tree(v)
        elif isinstance(value, list):
            for v in value:
                finite_tree(v)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite numeric value")
    finite_tree(data)
    for key in ("meta", "account", "period", "coverage", "summary", "metrics", "content", "causes", "actions", "attachments"):
        if not isinstance(data.get(key), dict):
            raise ValueError("Missing required object: " + key)
    meta = data["meta"]
    if meta.get("report_type") != "account_cycle_review" or not isinstance(meta.get("is_synthetic_sample"), bool):
        raise ValueError("Wrong report type or missing synthetic marker")
    for key in ("headline", "overview", "next_direction"):
        if not isinstance(data["summary"].get(key), str) or not data["summary"][key].strip():
            raise ValueError("DeepSeek summary is required: " + key)
    p, cov = data["period"], data["coverage"]
    a, b = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
    n = (b-a).days+1
    if not 1 <= n <= 366 or p["days_requested"] != n:
        raise ValueError("Invalid requested dates/day count")
    dc = cov["daily_coverage"]
    if any(not isinstance(dc.get(k), int) or isinstance(dc[k], bool) or dc[k] < 0 for k in ("days_covered", "days_missing")):
        raise ValueError("Invalid coverage counts")
    missing = dc["missing_dates"]
    if dc["days_covered"]+dc["days_missing"] != n or len(set(missing)) != dc["days_missing"]:
        raise ValueError("Coverage counts and missing dates disagree")
    if any(not a <= date.fromisoformat(v) <= b for v in missing):
        raise ValueError("Missing date outside period")
    statuses = cov.get("metrics_coverage")
    if not isinstance(statuses, list) or any(v.get("status") not in ("available", "partial", "unavailable", "not_applicable") for v in statuses):
        raise ValueError("Invalid metric coverage list")
    if len({v['metric_key'] for v in statuses}) != len(statuses):
        raise ValueError("Duplicate metric coverage key")
    status_by_key = {v['metric_key']: v['status'] for v in statuses}
    if cov.get("overall") not in ("complete", "partial", "degraded", "unavailable"):
        raise ValueError("Unknown coverage state")
    if cov["overall"] == "complete" and missing:
        raise ValueError("Complete coverage cannot have missing dates")
    if cov["overall"] == "unavailable":
        raise ValueError("No usable source: stop at coverage explanation, do not render report")
    overall = data["metrics"].get("overall")
    if not isinstance(overall, dict):
        raise ValueError("Missing metric overall object")
    for key, metric in overall.items():
        if key not in LABELS:
            continue
        if not isinstance(metric, dict) or "value" not in metric:
            raise ValueError("Metric must contain value: " + key)
        v = metric["value"]
        if v is None:
            if metric.get("unit") is not None:
                raise ValueError("Unknown metric must have null unit: " + key)
        elif not scalar_number(v) or (v < 0 and key != "new_follows") or not isinstance(metric.get("unit"), str) or not metric["unit"]:
            raise ValueError("Invalid metric value/unit: " + key)
        elif key != "avg_daily_plays" and int(v) != v:
            raise ValueError("Count must be an integer: " + key)
    ds = data["metrics"].get("daily_series")
    if ds is not None:
        dates = ds["dates"]
        if len(dates) != n or len(set(dates)) != n or dates != sorted(dates) or dates[0] != p["start_date"] or dates[-1] != p["end_date"]:
            raise ValueError("Daily series must align to all requested dates")
        for key, values in {**ds["series"], **(ds.get("rates_raw") or {})}.items():
            if not isinstance(values, list) or len(values) != n:
                raise ValueError("Daily array length mismatch: " + key)
            if any(values[dates.index(day)] is not None for day in missing):
                raise ValueError("Missing-date cells must be null")
        for key, field in (("plays", "total_plays"), ("likes", "total_likes"), ("shares", "total_shares"), ("comments", "total_comments"), ("posts", "posts")):
            values = ds["series"].get(key)
            if values is None:
                continue
            if any(v is not None and (not scalar_number(v) or v < 0 or int(v) != v) for v in values):
                raise ValueError("Daily counts must be nonnegative integers or null")
            declared = overall.get(field, {}).get("value")
            known = [v for v in values if v is not None]
            expected_status = "available" if len(known) == n else "partial" if known else "unavailable"
            metric_key = "posts_count" if key == "posts" else key
            if metric_key in status_by_key and status_by_key[metric_key] != expected_status:
                raise ValueError("Metric coverage/series mismatch: " + key)
            if declared is not None and (not known or declared != sum(known)):
                raise ValueError("Summary/series mismatch: " + field)
    avg = overall.get("avg_daily_plays")
    if avg and avg.get("value") is not None:
        count = avg.get("computed_from_days")
        total = overall.get("total_plays", {}).get("value")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= n or total is None:
            raise ValueError("Average needs known total and valid observed-day denominator")
        digits = max(0, -Decimal(str(avg["value"])).as_tuple().exponent)
        if abs(avg["value"]-total/count) > .5*10**(-digits)+1e-10:
            raise ValueError("Average/denominator mismatch")
        if ds is not None and "plays" in ds["series"] and count != sum(v is not None for v in ds["series"]["plays"]):
            raise ValueError("Average denominator must be known play dates")
    if len(missing) >= 4 or len(missing) > n/2:
        if any(v.get("value") is not None for k,v in overall.items() if k in LABELS):
            raise ValueError("Insufficient daily coverage: omit period summary cards")
    posts = data["content"].get("posts_in_period")
    if posts is not None and (not isinstance(posts, int) or isinstance(posts, bool) or posts < 0):
        raise ValueError("posts_in_period must be integer or null")
    if posts != overall.get("posts", {}).get("value"):
        raise ValueError("Content/metric post counts disagree")
    if posts is not None and (missing or (ds is not None and any(v is None for v in ds["series"].get("posts", [])))):
        raise ValueError("Unknown post dates cannot confirm full-period posts")
    if posts is None and data["content"].get("groups"):
        raise ValueError("Unknown full-period posts requires empty groups")
    comparison = data["metrics"].get("comparison")
    cp = p.get("comparison")
    if not cp or not cp.get("comparable"):
        if comparison is not None or cov.get("comparison_coverage") is not None:
            raise ValueError("Incomparable period cannot have comparison results")
        if cp and not cp.get("incomparable_reason"):
            raise ValueError("Incomparable period needs reason")
    else:
        cc = cov.get("comparison_coverage")
        if not comparison or not comparison.get("comparable") or not cc or cc.get("days_missing") != 0 or missing:
            raise ValueError("Comparable period needs complete coverage/results")
        ca, cb = date.fromisoformat(cp['start_date']), date.fromisoformat(cp['end_date'])
        if (cb-ca).days+1 != n or cb >= a or cc.get('days_covered') != n:
            raise ValueError("Comparison period dates/coverage do not match")
        if not comparison.get("metrics"):
            raise ValueError("Empty comparison")
        for key, item in comparison["metrics"].items():
            if not all(scalar_number(item.get(k)) for k in ("current", "previous", "change")) or not item.get("unit"):
                raise ValueError("Invalid comparison values/unit")
            if item["change"] != item["current"]-item["previous"] or item["current"] != overall.get(key, {}).get("value"):
                raise ValueError("Comparison mismatch: " + key)
    if data["attachments"].get("base_dir_relative", "./") not in ("./", "."):
        raise ValueError("Attachment base must be the JSON directory")
    for item in data["attachments"].get("items") or []:
        safe_attachment(base, item["path"])
    for group in data["content"].get("groups") or []:
        for work in group.get("works") or []:
            if work.get("evidence_index"):
                safe_attachment(base, work["evidence_index"])
    for work in data["causes"].get("deep_dives") or []:
        if work.get("work_index"):
            safe_attachment(base, work["work_index"])


def fonts():
    files = [Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\msyhbd.ttc"),
             EMOJI_FONT_FILE]
    if not all(p.is_file() for p in files):
        raise ValueError("Existing Microsoft YaHei fonts required; no installation attempted")
    for name, path in zip((FONT, BOLD, EMOJI), files):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=0))
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=BOLD, italic=FONT, boldItalic=BOLD)
    pdfmetrics.registerFontFamily(EMOJI, normal=EMOJI, bold=EMOJI, italic=EMOJI, boldItalic=EMOJI)


class DailyChart(Flowable):
    def __init__(self, dates, values):
        super().__init__()
        self.width, self.height = CONTENT_W, 120
        self.dates, self.values = dates, values

    def draw(self):
        c = self.canv
        x0, y0, w, h = 40, 28, self.width-50, 76
        maximum = max([v for v in self.values if v is not None]+[1])
        c.setStrokeColor(LINE); c.line(x0, y0, x0+w, y0); c.line(x0, y0, x0, y0+h)
        c.setFillColor(MUTED); c.setFont(FONT, 8)
        c.drawRightString(x0-5, y0-2, "0"); c.drawRightString(x0-5, y0+h-2, str(maximum))
        previous = None
        for i, v in enumerate(self.values):
            x = x0+w*i/max(1, len(self.values)-1)
            if v is None:
                previous = None
                continue
            y = y0+h*v/maximum
            c.setStrokeColor(ACCENT); c.setLineWidth(1.3)
            if previous is not None:
                c.line(*previous, x, y)
            c.setFillColor(ACCENT); c.circle(x, y, 2, fill=1, stroke=0)
            previous = (x, y)
        c.setFillColor(MUTED)
        for i in sorted(set([0, len(self.dates)//2, len(self.dates)-1])):
            x = x0+w*i/max(1, len(self.dates)-1)
            c.drawCentredString(x, y0-15, self.dates[i][5:])


class NumberedCanvas(Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.states = []

    def showPage(self):
        self.states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self.states)
        for state in self.states:
            self.__dict__.update(state)
            self.setFont(FONT, 8)
            self.setFillColor(MUTED)
            self.drawString(MARGIN, 12*mm, "多平台内容运营专家 · 账号周期复盘")
            self.drawRightString(WIDTH-MARGIN, 12*mm, f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


class CycleReport:
    def __init__(self, data):
        self.data, self.story, self.md = data, [], []
        fonts()
        self.body = ParagraphStyle("cycle_body", fontName=FONT, fontSize=10, leading=16,
                                   wordWrap="CJK", textColor=INK, spaceAfter=7)
        self.styles = {"body": self.body}
        for name, size, leading, color, bold in (("title", 21, 29, ACCENT, True),
                ("section", 14, 21, ACCENT, True), ("small", 8.5, 13, MUTED, False),
                ("quote", 10, 16, MUTED, False), ("suggestion", 10, 16, ACCENT, False)):
            self.styles[name] = ParagraphStyle("cycle_"+name, parent=self.body,
                fontName=BOLD if bold else FONT, fontSize=size, leading=leading,
                textColor=color, keepWithNext=(name == "section"),
                leftIndent=10 if name in ("quote", "suggestion") else 0,
                borderColor=LINE if name == "quote" else None,
                borderWidth=.5 if name == "quote" else 0, borderPadding=7 if name == "quote" else 0,
                backColor=PALE if name == "quote" else None)

    def paragraph(self, value, style="body"):
        text = escape(value).replace("\n", "<br/>")
        # PDF-only emoji fallback: wrap glyphs missing from the body font in an
        # inline font tag (existing system font). Applied after escaping so the
        # tags survive; Markdown keeps the original characters untouched.
        try:
            body_glyphs = pdfmetrics.getFont(self.styles[style].fontName).face.charToGlyph
        except KeyError:
            body_glyphs = {}
        if any(ord(ch) not in body_glyphs and not ch.isspace() for ch in value):
            text = re.sub(r"([^\x00-\x7F])", lambda m: m.group(1) if ord(m.group(1)) in body_glyphs
                          else '<font name="%s">%s</font>' % (EMOJI, m.group(1)), text)
        return Paragraph(text, self.styles[style])

    def add(self, value, style="body", original=False, prefix=""):
        if value is None or value == "":
            return
        value = prose(value, original)
        self.story.append(self.paragraph(value, style))
        self.md.append(prefix+value)

    def heading(self, value, major=False):
        self.add(value, "title" if major else "section", original=True, prefix="# " if major else "## ")

    def items(self, values, style="body"):
        for v in values or []:
            self.add(v, style, prefix="- ")

    def table(self, headers, records, widths=None):
        if not records:
            return
        rows = [headers]+records
        table = LongTable([[self.paragraph(str(v) if v is not None else "", "small") for v in row] for row in rows],
                          colWidths=widths or [CONTENT_W/len(headers)]*len(headers), repeatRows=1,
                          hAlign="LEFT", splitInRow=1)
        table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), PALE),
            ("LINEBELOW", (0,0), (-1,0), .6, LINE), ("LINEBELOW", (0,1), (-1,-1), .25, LINE),
            ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 7),
            ("RIGHTPADDING", (0,0), (-1,-1), 7), ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6)]))
        self.story += [table, Spacer(1, 10)]
        def mdcell(v):
            return str(v if v is not None else "").replace("|", "\\|").replace("\n", "<br>")
        self.md += ["| "+" | ".join(map(mdcell, headers))+" |", "|"+"---|"*len(headers)]
        self.md += ["| "+" | ".join(map(mdcell, row))+" |" for row in records]

    def work(self, w):
        self.add(w.get("title"), original=True)
        for k in ("published_at", "genre", "status"):
            self.add(w.get(k), "small", original=True)
        values = []
        for key, label in WORK_LABELS.items():
            if w.get(key) is not None:
                unit = "%" if key.endswith("_pct") else "秒" if key == "avg_watch_seconds" else "人" if key == "new_follows" else "次"
                values.append([label, str(w[key])+" "+unit])
        self.table(["指标", "截至快照日累计"], values)
        self.add(w.get("note"), "small")
        self.add(w.get("evidence_index"), "small", original=True)

    def build_story(self):
        d = self.data; summary = d["summary"]; period = d["period"]
        self.heading("账号周期复盘", True)
        self.add(d["account"].get("name"), "section", original=True)
        self.add(f"{period['start_date']} — {period['end_date']} · "+prose(d["account"].get("platform"), True), "small", original=True)
        if d["meta"]["is_synthetic_sample"]:
            self.add("合成测试样例", "section", original=True)
            self.add(d["meta"].get("sample_note"), "small")
        dc = d["coverage"]["daily_coverage"]
        self.add(f"数据覆盖 {dc['days_covered']}/{period['days_requested']} 天", "small", original=True)
        if dc["missing_dates"]:
            self.add("缺失日期："+"、".join(dc["missing_dates"]), "small", original=True)
        self.heading(summary["headline"])
        self.add(summary["overview"])
        metrics = d["metrics"]; overall = metrics["overall"]
        self.table(["本期表现", "数值"], [[LABELS[k], f"{v['value']} {v['unit']}"] for k, v in overall.items()
                   if k in ("posts", "total_plays", "avg_daily_plays") and v.get("value") is not None])
        for label, key in (("做得好的地方", "good_points"), ("最值得调整的地方", "concern_points")):
            if summary.get(key):
                self.heading(label); self.items(summary[key])
        self.heading("下一步方向"); self.add(summary["next_direction"], "suggestion")
        self.story.append(PageBreak())
        self.heading("数据达成")
        source = metrics.get("source") or {}
        for key in ("daily_source", "works_source"):
            if source.get(key):
                self.add(PureWindowsPath(str(source[key])).name, "small", original=True)
        self.add(source.get("snapshot_date"), "small", original=True)
        self.table(["指标", "数值", "口径说明"], [[LABELS[k], str(v["value"])+" "+str(v["unit"]), prose(v.get("note"))]
                   for k, v in overall.items() if k in LABELS and v.get("value") is not None],
                   [85, 100, CONTENT_W-185])
        unknown = [LABELS[k] for k, v in overall.items() if k in LABELS and v.get("value") is None]
        if unknown:
            self.add("未取得完整周期值："+"、".join(unknown), "small", original=True)
        ds = metrics.get("daily_series")
        if ds:
            if dc["days_missing"] < 4 and dc["days_missing"] <= period["days_requested"]/2:
                plays = ds["series"].get("plays")
                if plays is not None and any(v is not None for v in plays):
                    self.heading("逐日播放量（次）")
                    self.story.append(DailyChart(ds["dates"], plays))
            self.table(["日期", "播放", "投稿", "点赞", "分享", "评论"], [[day]+[
                "—" if ds["series"].get(k, [None]*len(ds["dates"]))[i] is None else ds["series"][k][i]
                for k in ("plays", "posts", "likes", "shares", "comments")] for i, day in enumerate(ds["dates"])],
                [100]+[(CONTENT_W-100)/5]*5)
            if ds.get("rates_raw"):
                rate_labels = {"completion_5s": "5秒完播率", "exit_2s": "2秒跳出率", "avg_watch": "平均观看时长", "cover_click": "封面点击率"}
                keys = list(ds["rates_raw"])
                self.table(["日期"]+[rate_labels.get(k,k) for k in keys], [[day]+[
                    "—" if ds["rates_raw"][k][i] is None else ds["rates_raw"][k][i] for k in keys] for i,day in enumerate(ds["dates"])])
                self.add(ds.get("rates_note"), "small")
        comparison = metrics.get("comparison")
        if comparison:
            self.heading("比较期对照")
            self.table(["指标", "当期", "上期", "变化", "单位"], [[LABELS.get(k,k), v["current"], v["previous"], v["change"], v["unit"]]
                       for k,v in comparison["metrics"].items()])
            self.add(comparison.get("note"), "small")
        elif period.get("comparison"):
            self.add(period["comparison"].get("incomparable_reason"), "small")
        distribution = overall.get("play_distribution")
        if distribution:
            self.heading("作品播放分布")
            self.add(distribution.get("scope"), "small")
            self.table(["区间", "数量"], [[v["label"], v["count"]] for v in distribution.get("bins") or []])
            self.add(distribution.get("note"), "small")
        accumulated = metrics.get("work_accumulated")
        if accumulated:
            self.heading("作品累计表现")
            self.add(accumulated.get("snapshot_date"), "small", original=True)
            self.add(accumulated.get("scope"), "small")
            self.add(accumulated.get("period_filtered_note"), "small")
            for w in accumulated.get("top_by_plays") or []:
                self.work(w)
        self.heading("内容产出与分组")
        self.add(d["content"].get("posts_note"))
        for group in d["content"].get("groups") or []:
            self.heading(group.get("group_label", "")); self.add(group.get("selection_reason"))
            for w in group.get("works") or []:
                self.work(w)
        self.heading("原因定位")
        self.add(d["causes"].get("distribution_factors"))
        for finding in d["causes"].get("screening_findings", []):
            self.add(finding.get("observation"))
            self.add(finding.get("data_basis"), "small")
            if finding.get("confidence") == "hypothesis":
                self.add("假设", "small", original=True)
        for w in d["causes"].get("deep_dives") or []:
            self.add(w.get("work_title"), "section", original=True)
            for key, label in (("hook","开头"), ("audience","受众"), ("structure","结构"), ("evidence_quality","证据"), ("ending","结尾")):
                if w.get("findings", {}).get(key):
                    self.add(label+"："+prose(w["findings"][key]), original=True)
            for q in w.get("original_quotes") or []:
                self.add(q.get("text"), "quote", original=True, prefix="> ")
                meta = [str(q[k]) for k in ("source", "time_seconds") if q.get(k) is not None]
                if meta:
                    self.add(" · ".join(meta), "small", original=True)
            self.add(w.get("notes"), "small"); self.add(w.get("work_index"), "small", original=True)
        self.heading("下期行动")
        actions = d["actions"]
        if actions.get("retain"):
            self.heading("保留清单"); self.items(actions["retain"], "suggestion")
        if actions.get("adjust"):
            self.heading("调整项")
            for item in actions["adjust"]:
                self.add(item.get("what"), "suggestion"); self.add(item.get("bound_to_finding"), "small")
        for label,key in (("选题建议","topic_suggestions"), ("所需材料","materials_needed")):
            if actions.get(key):
                self.heading(label); self.items(actions[key], "suggestion")
        review = actions.get("review")
        if review:
            self.heading("复查约定")
            self.add(review.get("check_date"), original=True)
            self.items(review.get("metrics"))
            self.add(review.get("retain_if"), "suggestion"); self.add(review.get("adjust_if"), "suggestion")
        attachments = d["attachments"].get("items", [])
        if attachments:
            self.heading("附件")
            for item in attachments:
                self.add(item["path"], "small", original=True)
                self.add(item.get("description"), "small")
        return self.story

    def render(self):
        stream = io.BytesIO()
        doc = SimpleDocTemplate(stream, pagesize=(WIDTH, HEIGHT), leftMargin=MARGIN, rightMargin=MARGIN,
                                topMargin=25*mm, bottomMargin=24*mm, title="账号周期复盘", author="多平台内容运营专家")
        def header(c, doc):
            c.saveState(); c.setFont(FONT, 8.5); c.setFillColor(ACCENT)
            c.drawString(MARGIN, HEIGHT-15*mm, "总评 / 数据达成 / 原因定位 / 下期行动")
            c.setStrokeColor(LINE); c.line(MARGIN, HEIGHT-18*mm, WIDTH-MARGIN, HEIGHT-18*mm); c.restoreState()
        doc.build(self.build_story(), onFirstPage=header, onLaterPages=header, canvasmaker=NumberedCanvas)
        return stream.getvalue(), "\n\n".join(self.md)+"\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", type=Path)
    args = parser.parse_args()
    try:
        path = args.json_path.resolve(strict=True)
        pdf, md = path.parent/PDF_NAME, path.parent/MD_NAME
        if pdf.exists() or md.exists():
            raise FileExistsError("Report output exists; use a new directory")
        data = load_data(path)
        pdf_bytes, markdown = CycleReport(data).render()
        # Both are fully prepared before writing. Exclusive creation prevents overwrite.
        with pdf.open("xb") as f:
            f.write(pdf_bytes)
        with md.open("x", encoding="utf-8") as f:
            f.write(markdown)
        print("CYCLE_REPORT_READY " + str(pdf))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError) as e:
        print("CYCLE_RENDER_ERROR: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
