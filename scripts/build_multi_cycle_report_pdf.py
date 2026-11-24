# -*- coding: utf-8 -*-
"""Render the multi-platform account-cycle JSON model to PDF and Markdown.

Parallel renderer for the three-platform cycle review. Reuses the visual
language of the single-platform build_cycle_report_pdf.py (fonts, colors,
DailyChart, numbered canvas, prose replacement rules) without importing or
modifying that protected renderer. Story layout fuses the single-platform
four-step structure (summary first, daily charts, comparison, actions review)
with per-platform sections and a cross-platform comparison block.

Local, deterministic presentation only. Does not collect data, call models,
fill missing business values, or invent analysis. Validation never changes
the input.
"""
import argparse
from datetime import date, timedelta
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
                               Table, TableStyle, PageBreak, Flowable, KeepTogether)

PDF_NAME = "三平台周期复盘.pdf"
MD_NAME = "三平台周期复盘.md"
FONT, BOLD = "MultiCycleYaHei", "MultiCycleYaHeiBold"
EMOJI = "MultiCycleEmoji"
EMOJI_FONT_FILE = Path(r"C:\Windows\Fonts\seguiemj.ttf")
WIDTH, HEIGHT = 595.28, 841.89
MARGIN = 18*mm
CONTENT_W = WIDTH-2*MARGIN
ACCENT = colors.HexColor("#1A3A34")
INK = colors.HexColor("#202B2A")
MUTED = colors.HexColor("#667B75")
PALE = colors.HexColor("#EEF4F1")
LINE = colors.HexColor("#D5E0DA")
# Verbatim existing word list (same rules as the single-platform renderer).
REPLACEMENTS = {'本轮参考': '参考数据', '留住观看': '留住观众', '本作品': '这条视频',
                '同好': '朋友', '亲历': '真实经历', '本条': '这条视频', '本轮': '这次'}
PLATFORM_ORDER = ["抖音", "小红书", "视频号"]


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
    for key in ("meta", "account", "period", "summary", "cross_platform_comparison",
                "platform_sections", "causes", "actions",
                "data_boundaries", "attachments"):
        if not isinstance(data.get(key), (dict, list)):
            raise ValueError("Missing required object: " + key)
    meta = data["meta"]
    if meta.get("report_type") != "multi_platform_cycle_review" or not isinstance(meta.get("is_synthetic_sample"), bool):
        raise ValueError("Wrong report type or missing synthetic marker")
    for key in ("headline", "overview", "next_direction"):
        if not isinstance(data["summary"].get(key), str) or not data["summary"][key].strip():
            raise ValueError("Summary is required: " + key)
    p = data["period"]
    a, b = date.fromisoformat(p["start_date"]), date.fromisoformat(p["end_date"])
    n = (b-a).days+1
    if not 1 <= n <= 366 or p["days_requested"] != n:
        raise ValueError("Invalid requested dates/day count")
    # Platform sections: each carries aligned series and honest metrics rows.
    sections = data["platform_sections"]
    if not isinstance(sections, list) or not sections:
        raise ValueError("platform_sections must be a non-empty list")
    for section in sections:
        plat = section.get("platform")
        if plat not in PLATFORM_ORDER:
            raise ValueError("Unknown platform section: " + str(plat))
        series = section.get("series")
        if series:
            dates = series["dates"]
            # P1-04: full expected calendar, not just length/ends/sorted — a
            # duplicated day that drops a real day must be rejected.
            expected = [(a + timedelta(days=i)).isoformat() for i in range(n)]
            if dates != expected:
                raise ValueError("Platform series must align to period: " + plat)
            for key, values in series["values"].items():
                if not isinstance(values, list) or len(values) != n:
                    raise ValueError("Series length mismatch: " + plat + "/" + key)
        for row in section.get("metrics_rows") or []:
            if len(row) != 4:
                raise ValueError("metrics_rows entries must be [label, value, unit, note]")
            if row[1] is not None and not scalar_number(row[1]):
                raise ValueError("Metric value must be number or null: " + str(row[0]))
    plats = [s["platform"] for s in sections]
    if len(plats) != len(set(plats)):
        raise ValueError("Duplicate platform sections")
    # Cross-platform comparison: same-definition additive fields only.
    for field in data["cross_platform_comparison"].get("comparable_fields") or []:
        for plat, entry in field.get("per_platform", {}).items():
            v = entry.get("value")
            if v is not None and not scalar_number(v):
                raise ValueError("Comparison value must be number or null")
    # Cross-check: comparison values must match their platform section's row
    # BY METRIC IDENTITY, not merely equal some number in the table. The
    # re-review found that value-only matching passes a misfiled value (plays
    # set to 7 = likes value); each comparison field must map to the exact
    # metrics_rows row and its declared source header.
    plat_metrics = {s["platform"]: s.get("metrics_rows") for s in sections}
    # metric key -> the metrics_rows label it must match, per platform.
    IDENTITY = {
        "抖音": {"posts": "投稿量", "plays": "总播放量", "likes": "总点赞量",
                 "comments": "总评论量", "shares": "总分享量", "fans": None},
        "小红书": {"posts": "总发布", "plays": "观看", "likes": "点赞",
                   "comments": "评论", "shares": "分享", "fans": "净涨粉"},
        "视频号": {"posts": None, "plays": "播放", "likes": "喜欢",
                  "comments": "评论", "shares": "分享", "fans": "净增关注"},
    }
    cmp_fields = data["cross_platform_comparison"].get("comparable_fields") or []
    for field in cmp_fields:
        metric = field.get("metric")
        for plat, entry in field.get("per_platform", {}).items():
            v = entry.get("value")
            if v is None or plat not in plat_metrics:
                continue
            want_label = IDENTITY.get(plat, {}).get(metric)
            if want_label is None:
                continue  # no declared row for this metric on this platform
            label = field.get("label") or metric
            matched = next((row for row in plat_metrics[plat] if row[0] == want_label), None)
            if matched is None:
                raise ValueError(
                    f"Comparison field {label}/{plat}: platform row '{want_label}' missing")
            if matched[1] != v:
                raise ValueError(
                    f"Comparison value {v} ({label}/{plat}) != platform row "
                    f"'{want_label}' value {matched[1]}; refusing to render")
            # P2-1 (third review): the declared source header must match the
            # metric identity too — a mislabeled header (e.g. plays claiming
            # 总点赞量) is rejected even when the value is correct.
            declared = entry.get("header")
            if declared is not None and declared != want_label:
                raise ValueError(
                    f"Comparison header '{declared}' ({label}/{plat}) != identity "
                    f"row label '{want_label}'; refusing to render")
    if data["attachments"].get("base_dir_relative", "./") not in ("./", "."):
        raise ValueError("Attachment base must be the JSON directory")
    for item in data["attachments"].get("items") or []:
        safe_attachment(base, item["path"])


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
    """Line chart over daily values; nulls break the line (same rules as the
    single-platform renderer)."""

    def __init__(self, dates, values, title=None):
        super().__init__()
        self.width, self.height = CONTENT_W, 120
        self.dates, self.values, self.title = dates, values, title

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


class BarPairChart(Flowable):
    """This-week vs last-week paired bars for one platform, with the delta
    annotated red/green between the bar tips. Values carry units in caption."""

    def __init__(self, label, current, previous, unit="", slot=None):
        super().__init__()
        self.width = slot or (CONTENT_W/2 - 8)
        self.height = 118
        self.label, self.current, self.previous, self.unit = label, current, previous, unit

    def draw(self):
        c = self.canv
        x0, y0, w, h = 40, 30, self.width-46, 62
        maximum = max(self.current, self.previous, 1)
        c.setFillColor(ACCENT); c.setFont(BOLD, 9)
        c.drawString(x0, self.height-14, f"{self.label}（本周vs上周）")
        c.setFont(FONT, 7); c.setFillColor(MUTED)
        c.drawRightString(x0-2, y0-2, "0")
        # Axis top label sits at the 0.80h bar cap: the axis value at h would
        # read maximum/0.80 which never matches a bar, so cap label = maximum.
        c.drawRightString(x0-2, y0+h*0.80, str(maximum))
        c.setStrokeColor(LINE); c.line(x0, y0, x0+w, y0)
        bw = w*0.26
        gap = w*0.18
        # previous (muted) then current (accent)
        for idx, (val, color) in enumerate(((self.previous, MUTED), (self.current, ACCENT))):
            bx = x0 + gap/2 + idx*(bw+gap)
            bh = h*0.80*val/maximum
            c.setFillColor(color)
            c.rect(bx, y0, bw, bh, fill=1, stroke=0)
            # Value inside the bar top (white) — the delta band above stays
            # empty so the two can never collide.
            c.setFillColor(colors.white); c.setFont(BOLD, 8)
            if bh > 11:
                c.drawCentredString(bx+bw/2, y0+bh-10, str(val))
            else:
                c.setFillColor(INK)
                c.drawCentredString(bx+bw/2, y0+bh+3, str(val))
        # delta annotation ABOVE the current bar's tip (own vertical band),
        # so it cannot collide with the bottom x-labels.
        diff = self.current - self.previous
        if diff > 0:
            c.setFillColor(colors.HexColor("#C0392B")); mark = f"▲ +{diff}"
        elif diff < 0:
            c.setFillColor(colors.HexColor("#1E8449")); mark = f"▼ {diff}"
        else:
            c.setFillColor(MUTED); mark = "— 持平"
        # Fixed delta band at 88% height: bars capped at 80% below it, the
        # title line sits above 100% — three separate vertical zones.
        c.setFont(BOLD, 8.5)
        c.drawCentredString(x0+gap/2+bw+gap+bw/2, y0+h*0.88, mark)
        c.setFont(FONT, 7); c.setFillColor(MUTED)
        c.drawCentredString(x0+gap/2+bw/2, y0-13, "上")
        c.drawCentredString(x0+gap/2+bw+gap+bw/2, y0-13, "本")


class WeeklyTrendChart(Flowable):
    """Four-week mini bar chart per platform; last (current) week's bar in
    accent, prior weeks muted, values labeled on top."""

    def __init__(self, label, weeks_values, unit="", slot=None):
        super().__init__()
        self.width = slot or (CONTENT_W/2 - 8)
        self.height = 118
        self.label, self.values, self.unit = label, weeks_values, unit

    def draw(self):
        c = self.canv
        x0, y0, w, h = 34, 26, self.width-40, 58
        # P1-03: None weeks are unknown, not zero — excluded from the scale.
        known = [v for v in self.values if v is not None]
        maximum = max(known+[1])
        c.setFillColor(ACCENT); c.setFont(BOLD, 9)
        c.drawString(x0, self.height-12, f"{self.label}（近4周）")
        c.setFont(FONT, 7); c.setFillColor(MUTED)
        c.drawRightString(x0-2, y0-2, "0")
        c.drawRightString(x0-2, y0+h, str(maximum))
        c.setStrokeColor(LINE); c.line(x0, y0, x0+w, y0)
        # P1-03: labels follow the full slot list (4 slots even when a week is
        # null); null slots draw a small dash placeholder instead of a bar.
        n = len(self.values)
        bw = w*0.16
        slot = w/n
        for i, val in enumerate(self.values):
            bx = x0 + slot*i + (slot-bw)/2
            if val is None:
                c.setFillColor(MUTED); c.setFont(FONT, 7)
                c.drawCentredString(bx+bw/2, y0+h*0.5, "—")
                continue
            bh = h*val/maximum
            c.setFillColor(ACCENT if i == n-1 else LINE)
            c.rect(bx, y0, bw, bh, fill=1, stroke=0)
            c.setFillColor(INK); c.setFont(BOLD, 7)
            c.drawCentredString(bx+bw/2, y0+bh+2, str(val))
        c.setFont(FONT, 7); c.setFillColor(MUTED)
        labels = ["-3周", "-2周", "-1周", "本周"]
        for i, lab in enumerate(labels[-n:] if n < 4 else labels):
            c.drawCentredString(x0+slot*i+slot/2, y0-12, lab)


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
            self.drawString(MARGIN, 12*mm, "多平台内容运营专家 · 三平台周期复盘")
            self.drawRightString(WIDTH-MARGIN, 12*mm, f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


class MultiCycleReport:
    def __init__(self, data):
        self.data, self.story, self.md = data, [], []
        fonts()
        # Magazine-style hierarchy: distinct sizes/weights per level so the
        # reader can see structure at a glance (the old uniform look was the
        # top complaint). Levels: title > step(一、二…) > sub(2.1) > body.
        self.body = ParagraphStyle("multi_cycle_body", fontName=FONT, fontSize=10, leading=16,
                                   wordWrap="CJK", textColor=INK, spaceAfter=7)
        self.styles = {"body": self.body}
        for name, size, leading, color, bold, before in (
                ("title", 22, 30, ACCENT, True, 0),
                ("step", 17, 24, colors.white, True, 0),        # white on dark bar
                ("section", 13.5, 20, ACCENT, True, 10),
                ("small", 8.5, 13, MUTED, False, 0),
                ("quote", 10, 16, MUTED, False, 0),
                ("kpi_note", 8, 12, MUTED, False, 0),
                ("suggestion", 10, 16, ACCENT, False, 0)):
            self.styles[name] = ParagraphStyle("multi_cycle_"+name, parent=self.body,
                fontName=BOLD if bold else FONT, fontSize=size, leading=leading,
                textColor=color, keepWithNext=(name in ("section", "step")),
                spaceBefore=before,
                leftIndent=10 if name in ("quote", "suggestion") else 0,
                borderColor=LINE if name == "quote" else None,
                borderWidth=.5 if name == "quote" else 0, borderPadding=7 if name == "quote" else 0,
                backColor=PALE if name == "quote" else None)

    def paragraph(self, value, style="body"):
        text = escape(value).replace("\n", "<br/>")
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

    def step_heading(self, value):
        """Step banner (一、二、三、四): dark bar + white bold text — the main
        structural marker users scan for."""
        banner = Table([[self.paragraph(value, "step")]],
                       colWidths=[CONTENT_W], rowHeights=[26])
        banner.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), ACCENT),
                                    ("LEFTPADDING", (0,0), (-1,-1), 12),
                                    ("VALIGN", (0,0), (-1,-1), "MIDDLE")]))
        self.story += [Spacer(1, 6), banner, Spacer(1, 10)]
        self.md.append("# " + value)

    def kpi_cards(self, cards):
        """Big-number KPI row: value + delta triangle in red/green (China
        convention: red up, green down) + label + last-week reference."""
        if not cards:  # P1-02 guard: degrade, never divide by zero
            return
        UP = colors.HexColor("#C0392B")   # red = rise (China convention)
        DOWN = colors.HexColor("#1E8449")  # green = fall
        cell_tables = []
        for card in cards:
            label, value, prev, unit = card["label"], card["value"], card.get("prev"), card.get("unit", "")
            delta_html = ""
            delta_color = MUTED
            if prev is not None and value is not None:
                diff = value - prev
                if diff > 0:
                    delta_html = f'<font color="#C0392B">▲ +{diff}</font>'
                elif diff < 0:
                    delta_html = f'<font color="#1E8449">▼ {diff}</font>'
                else:
                    delta_html = f'<font color="#667B75">— 持平</font>'
            value_flow = [Paragraph(f'<font size="20"><b>{value}</b></font> <font size="9" color="#667B75">{unit}</font>',
                                     ParagraphStyle("kpi_v", fontName=BOLD, fontSize=20, leading=24,
                                                    textColor=INK, alignment=1))]
            if delta_html:
                value_flow.append(Paragraph(delta_html, ParagraphStyle("kpi_d", fontName=BOLD,
                                             fontSize=11, leading=15, alignment=1)))
            value_flow.append(Paragraph(f'上周 {prev}' if prev is not None else "无上周数据",
                                         ParagraphStyle("kpi_p", fontName=FONT, fontSize=8, leading=11,
                                                        textColor=MUTED, alignment=1)))
            # Pad to a fixed 3-row content height so cards without deltas
            # stay equal-height with the others (no staggered look).
            while len(value_flow) < 3:
                value_flow.append(Paragraph("", ParagraphStyle("kpi_pad", fontName=FONT,
                                                                fontSize=8, leading=15)))
            inner = Table([[value_flow]], colWidths=[(CONTENT_W-24)/len(cards)],
                          rowHeights=[74])
            inner.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), PALE),
                                       ("BOX", (0,0), (-1,-1), .7, LINE),
                                       ("TOPPADDING", (0,0), (-1,-1), 9),
                                       ("BOTTOMPADDING", (0,0), (-1,-1), 7),
                                       ("LEFTPADDING", (0,0), (-1,-1), 4),
                                       ("RIGHTPADDING", (0,0), (-1,-1), 4)]))
            header = Paragraph(label, ParagraphStyle("kpi_l", fontName=BOLD, fontSize=10,
                                                     leading=14, textColor=ACCENT, alignment=1))
            cell = Table([[header], [inner]], colWidths=[(CONTENT_W-24)/len(cards)])
            cell.setStyle(TableStyle([("LEFTPADDING", (0,0), (-1,-1), 4),
                                      ("RIGHTPADDING", (0,0), (-1,-1), 4),
                                      ("TOPPADDING", (0,0), (-1,-1), 2),
                                      ("BOTTOMPADDING", (0,0), (-1,-1), 2)]))
            cell_tables.append(cell)
        row = Table([cell_tables], colWidths=[CONTENT_W/len(cards)]*len(cards))
        row.setStyle(TableStyle([("LEFTPADDING", (0,0), (-1,-1), 0),
                                 ("RIGHTPADDING", (0,0), (-1,-1), 0),
                                 ("TOPPADDING", (0,0), (-1,-1), 0),
                                 ("BOTTOMPADDING", (0,0), (-1,-1), 0)]))
        self.story += [row, Spacer(1, 12)]
        md_parts = []
        for card in cards:
            prev = card.get("prev")
            arrow = ""
            if prev is not None and card["value"] is not None:
                diff = card["value"] - prev
                arrow = " ▲+" + str(diff) if diff > 0 else (" ▼" + str(diff) if diff < 0 else " —")
            md_parts.append(f"**{card['label']}：{card['value']}{arrow}**（上周 {prev if prev is not None else '—'}）")
        self.md += [" ".join(md_parts)]

    def delta_plain(self, current, previous):
        if previous is None or current is None:
            return ""
        diff = current - previous
        if diff > 0:
            return f"(+{diff})"
        if diff < 0:
            return f"({diff})"
        return "(持平)"

    def delta(self, current, previous):
        """Text delta with triangle + color, for tables."""
        if previous is None or current is None:
            return "—"
        diff = current - previous
        if diff > 0:
            return f'<font color="#C0392B">▲+{diff}</font>'
        if diff < 0:
            return f'<font color="#1E8449">▼{diff}</font>'
        return "—"

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

    def build_story(self):
        d = self.data; summary = d["summary"]; period = d["period"]
        account = d["account"]; cmp_ = d["cross_platform_comparison"]
        # ── Page 1 (magazine cover): title + headline + KPI cards + paired
        # bars. The reader gets the whole week in one glance here.
        self.heading("三平台周期复盘", True)
        platforms = " / ".join(account.get("platforms") or [])
        self.add(f"{account.get('name')} · {period['start_date']} — {period['end_date']} · {platforms}", "small", original=True)
        comp = period.get("comparison") or {}
        self.add(f"周环比 {comp.get('start_date','')} — {comp.get('end_date','')}", "small", original=True)
        if d["meta"]["is_synthetic_sample"]:
            self.add("合成测试样例", "section", original=True)
        self.heading(summary["headline"])
        # KPI row: the week's headline numbers with deltas.
        plays_field = next((f for f in cmp_.get("comparable_fields") or [] if f["metric"] == "plays"), {})
        kpi = []
        for plat in PLATFORM_ORDER:
            entry = plays_field.get("per_platform", {}).get(plat)
            if entry and entry.get("value") is not None:
                kpi.append({"label": f"{plat} {entry.get('header') or '播放'}",
                            "value": entry["value"], "prev": entry.get("prev"), "unit": "次"})
        comments_field = next((f for f in cmp_.get("comparable_fields") or [] if f["metric"] == "comments"), {})
        dy_c = comments_field.get("per_platform", {}).get("抖音") or {}
        if dy_c.get("value") is not None:
            kpi.append({"label": "抖音 评论", "value": dy_c["value"], "prev": dy_c.get("prev"), "unit": "条"})
        # P1-02: all-unknown comparison must degrade safely — skip the row
        # instead of dividing by len(cards)==0.
        if kpi:
            self.kpi_cards(kpi[:4])
        else:
            self.add("本周各平台合计值未知（无对比KPI卡）", "section")
        # Paired bars: this week vs last week, three platforms side by side.
        bars = []
        for plat in PLATFORM_ORDER:
            entry = plays_field.get("per_platform", {}).get(plat)
            if entry and entry.get("value") is not None and entry.get("prev") is not None:
                bars.append(BarPairChart(plat, entry["value"], entry["prev"], "次", slot=CONTENT_W/3 - 12))
        if bars:
            pair_row = Table([bars], colWidths=[CONTENT_W/len(bars)]*len(bars))
            pair_row.setStyle(TableStyle([("LEFTPADDING", (0,0), (-1,-1), 4),
                                          ("RIGHTPADDING", (0,0), (-1,-1), 4),
                                          ("TOPPADDING", (0,0), (-1,-1), 4),
                                          ("BOTTOMPADDING", (0,0), (-1,-1), 4)]))
            self.story += [pair_row, Spacer(1, 8)]
        self.add(summary["overview"])
        self.story.append(PageBreak())

        # ── Step 1: 回顾目标 ─────────────────────────────────────────────
        goal = d.get("goal_review") or {}
        self.step_heading("一、回顾目标")
        self.add(f"定位受众：{account.get('target_audience')}　·　内容价值：{account.get('content_value')}　·　业务目标：{account.get('business_goal')}", "small", original=True)
        if goal.get("positioning_check"):
            self.add(goal.get("positioning_check"), "small")
        if goal.get("has_previous_goal"):
            self.heading("上期目标与执行情况（台账）")
            rows = [[g.get("goal"), g.get("status"), g.get("evidence")] for g in goal.get("previous_goals") or []]
            self.table(["上期目标", "状态", "依据"], rows, [CONTENT_W-260, 80, 180])
        else:
            self.heading("上期目标")
            self.add("没有上一周期的对比数据，所以本次报告没有完成率的分析，但是有数据本身表现的分析（见第二步，以周环比与近4周自参照呈现）。", "small")
            if goal.get("no_previous_goal_note"):
                self.add(goal.get("no_previous_goal_note"), "small")

        # ── Step 2: 现状表现 ─────────────────────────────────────────────
        self.step_heading("二、现状表现")
        self.heading("三平台横向对比（含周环比）")
        rows = []
        for field in cmp_.get("comparable_fields") or []:
            row = [field["label"]]
            for plat in PLATFORM_ORDER:
                entry = field.get("per_platform", {}).get(plat)
                if entry is None:
                    row.append("—")
                elif entry.get("value") is None:
                    row.append("未知")
                else:
                    cell = f"{entry['value']}"
                    if entry.get("prev") is not None:
                        cell += " " + self.delta_plain(entry["value"], entry["prev"])
                    row.append(cell)
            rows.append(row)
        self.table(["指标（本期合计）"]+PLATFORM_ORDER, rows)
        # 近4周 trend charts (three mini bar charts side by side).
        hist = cmp_.get("weekly_history")
        if hist:
            self.heading("近4周趋势（本账号自参照）")
            trends = []
            for plat in PLATFORM_ORDER:
                # P1-03: keep the real time slots — a null week stays as a gap,
                # values are never re-labelled to a wrong week.
                weeks_values = [w.get(plat) for w in hist.get("weeks") or []]
                if any(v is not None for v in weeks_values):
                    trends.append(WeeklyTrendChart(plat, weeks_values, "次", slot=CONTENT_W/3 - 12))
            if trends:
                self.md.append("| 自然周 | 抖音 | 小红书 | 视频号 |")
                self.md.append("|---|---|---|---|")
                for w_ in hist.get("weeks") or []:
                    self.md.append(f"| {w_['week']} | {w_.get('抖音','—')} | {w_.get('小红书','—')} | {w_.get('视频号','—')} |")
                tr_row = Table([trends], colWidths=[CONTENT_W/len(trends)]*len(trends))
                tr_row.setStyle(TableStyle([("LEFTPADDING", (0,0), (-1,-1), 4),
                                            ("RIGHTPADDING", (0,0), (-1,-1), 4),
                                            ("TOPPADDING", (0,0), (-1,-1), 4),
                                            ("BOTTOMPADDING", (0,0), (-1,-1), 4)]))
                self.story += [tr_row, Spacer(1, 6)]
        self.heading("本期做得好的")
        self.items(summary.get("good_points"), "body")
        self.heading("最值得调整的")
        self.items(summary.get("concern_points"), "body")
        self.heading("下一步方向")
        self.add(summary["next_direction"], "suggestion")

        # ── Per-platform subsections under step 2: KPI cards open each
        # platform, then daily chart + tables.
        sub = {"抖音": "2.8", "小红书": "2.9", "视频号": "2.10"}
        for index, section in enumerate(d["platform_sections"]):
            plat = section["platform"]
            self.story.append(Spacer(1, 12))
            # Platform banner (distinct sub-step band, lighter than step bars).
            plat_banner = Table([[self.paragraph(f"{sub.get(plat, '')} {plat}", "section")]],
                                colWidths=[CONTENT_W], rowHeights=[20])
            plat_banner.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), PALE),
                                             ("LEFTPADDING", (0,0), (-1,-1), 10),
                                             ("VALIGN", (0,0), (-1,-1), "MIDDLE")]))
            self.story.append(plat_banner)
            head_flow = [Spacer(1, 4)]
            pf = section.get("platform_focus")
            if pf:
                focus_flow = [self.paragraph("这个平台看什么", "section"),
                              self.paragraph(prose(pf.get("watch") or ""), "body"),
                              self.paragraph(prose(pf.get("vs") or ""), "small")]
                self.story.append(KeepTogether(focus_flow))
                self.md.append("### 这个平台看什么")
                self.md.append(prose(pf.get("watch") or ""))
                self.md.append(prose(pf.get("vs") or ""))
            src = section.get("source_note")
            if src:
                head_flow.append(self.paragraph(prose(src, original=True), "small"))
            rows = [[r[0], "未知" if r[1] is None else str(r[1]), r[2] or "", prose(r[3] or "") if r[3] else ""]
                    for r in section.get("metrics_rows") or []]
            self.table(["指标", "数值", "单位", "口径说明"], rows, [90, 60, 55, CONTENT_W-205])
            head_flow.append(self.story.pop(-2))  # the LongTable
            self.story.append(KeepTogether(head_flow))
            self.md.append(f"## {sub.get(plat, '')} {plat}")
            if src:
                self.md.append(prose(src, original=True))
            # Platform KPI cards where prev is available.
            pk = []
            prev_map = {}
            if plat == "抖音" and section.get("comparison"):
                for r in section["comparison"].get("rows") or []:
                    prev_map[r[0]] = r[2]
            elif plat in ("小红书", "视频号"):
                for field in cmp_.get("comparable_fields") or []:
                    entry = field.get("per_platform", {}).get(plat)
                    if entry and entry.get("prev") is not None:
                        prev_map[field["label"]] = entry["prev"]
            for r in section.get("metrics_rows") or []:
                if r[1] is None or len(pk) >= 3:
                    continue
                label = r[0]
                if label in ("总播放量", "播放", "观看"):
                    continue  # plays already in the cover KPI
                pk.append({"label": label, "value": r[1], "prev": prev_map.get(label), "unit": r[2] or ""})
            if pk:
                self.kpi_cards(pk)
            # Works-focus block first (this period's key work).
            wf = section.get("works_focus")
            if wf:
                focus_flow = [self.paragraph("本期焦点作品", "section"),
                              self.paragraph(prose(wf.get("title_first_line") or ""), "body"),
                              self.paragraph(prose(wf.get("published_at") or "", original=True), "small"),
                              self.paragraph(prose(wf.get("snapshot_note") or ""), "small")]
                if wf.get("note"):
                    focus_flow.append(self.paragraph(prose(wf["note"]), "small"))
                self.story.append(KeepTogether(focus_flow))
                self.md.append(f"### {plat} · 本期焦点作品")
                self.md.append(prose(wf.get("title_first_line") or ""))
            series = section.get("series")
            if series:
                chart = section.get("chart") or {}
                key = chart.get("metric_key") or (series["columns"][0]["key"] if series.get("columns") else None)
                values = series["values"].get(key)
                if values and any(v is not None for v in values):
                    chart_flow = [self.paragraph(chart.get("title") or f"{plat} · 逐日", "section"),
                                  DailyChart(series["dates"], values)]
                    self.story.append(KeepTogether(chart_flow))
                cols = series.get("columns") or []
                self.table(["日期"]+[c["label"] for c in cols],
                           [[day]+[("—" if series["values"][c["key"]][i] is None else series["values"][c["key"]][i])
                             for c in cols] for i, day in enumerate(series["dates"])],
                           [70]+[(CONTENT_W-70)/len(cols)]*len(cols))
                # Daily table (8 rows) fits one page; pop it into the chart
                # block so a page break cannot strand a single row.
                daily_tbl = self.story.pop()  # Spacer after table
                daily_tbl2 = self.story.pop()  # the LongTable
                chart_plus = [daily_tbl2, Spacer(1, 6)]
                self.story.append(KeepTogether(chart_plus))
            rates = section.get("rates")
            if rates:
                keys = rates["keys"]
                rate_rows = [[day]+[("—" if rates["values"][k][i] is None else rates["values"][k][i])
                                   for k in keys] for i, day in enumerate(section["series"]["dates"])]
                rate_flow = [self.paragraph("逐日率值", "section")]
                self.table(rates["headers"], rate_rows)
                rate_flow.append(self.story.pop(-2))
                self.story.append(KeepTogether(rate_flow))
                self.add(rates.get("note"), "small")
            comparison = section.get("comparison")
            if comparison:
                self.heading("周环比对照")
                self.table(["指标", "当期", "上期", "变化", "单位"],
                           [[r[0], r[1], r[2], r[3], r[4]] for r in comparison.get("rows") or []])
            works = section.get("works_top")
            if works:
                works_flow = [self.paragraph("作品累计表现（背景参照）", "section"),
                              self.paragraph(prose(section.get("works_note") or ""), "small")]
                self.table(["作品（标题首行）", "发布时间", "累计播放"],
                           [[w["title_first_line"], w.get("published_at"), w.get("plays")] for w in works],
                           [CONTENT_W-220, 105, 115])
                works_flow.append(self.story.pop(-2))
                self.story.append(KeepTogether(works_flow))

        # ── Step 3: 原因分析 ─────────────────────────────────────────────
        causes = d["causes"]
        self.step_heading("三、原因分析")
        for finding in causes.get("screening_findings", []):
            obs = finding.get("observation") or ""
            basis = finding.get("data_basis") or ""
            if finding.get("confidence") == "hypothesis" and basis:
                basis = "〔假设〕" + basis
            finding_flow = [self.paragraph(obs, "section"),
                            self.paragraph(prose(basis), "small")]
            self.story.append(KeepTogether(finding_flow))
            self.md.append("### " + obs)
            self.md.append(prose(basis))
        self.add(causes.get("distribution_factors"), "small")

        # ── Step 4: 总结迭代 ─────────────────────────────────────────────
        self.step_heading("四、总结迭代")
        actions = d["actions"]
        if actions.get("retain"):
            self.heading("下周继续这么干（已验证有效）"); self.items(actions["retain"], "suggestion")
        if actions.get("adjust"):
            self.heading("下周要做的四件事（每件：做什么 + 为什么 + 做完看什么）")
            for item in actions["adjust"]:
                item_flow = [self.paragraph(prose(item.get("what") or ""), "suggestion"),
                             self.paragraph(prose(item.get("bound_to_finding") or ""), "small")]
                self.story.append(KeepTogether(item_flow))
                self.md.append("**" + prose(item.get("what") or "") + "**")
                self.md.append(prose(item.get("bound_to_finding") or ""))
        if actions.get("topic_suggestions"):
            self.heading("7条备选选题（挑1个就能开工）"); self.items(actions["topic_suggestions"], "body")
        review = actions.get("review")
        review_flow = []
        if review:
            review_flow.append(self.paragraph("下周复盘时自动核对（已登记入周目标台账，你不用平时盯）", "section"))
            review_flow.append(self.paragraph(prose(review.get("check_date") or "", original=True), "body"))
            for m in review.get("metrics") or []:
                review_flow.append(self.paragraph("- "+prose(m), "body"))
            for key in ("retain_if", "adjust_if"):
                if review.get(key):
                    review_flow.append(self.paragraph(prose(review[key]), "suggestion"))
        if review_flow:
            self.story.append(KeepTogether(review_flow))
            self.md.append("## 下周复盘时自动核对")
            self.md.append(prose(review.get("check_date") or ""))
            for m in review.get("metrics") or []:
                self.md.append("- " + prose(m))
            for key in ("retain_if", "adjust_if"):
                if review.get(key):
                    self.md.append(prose(review[key]))

        # ── 数据边界 moved to report end: data explanation, not narrative.
        boundary_flow = [self.paragraph("数据边界与口径说明（附录）", "section")]
        for section in d.get("platform_sections") or []:
            plat = section.get("platform")
            for v in section.get("notes") or []:
                boundary_flow.append(self.paragraph(f"- 〔{plat}〕"+prose(v), "small"))
        for b in d.get("data_boundaries") or []:
            boundary_flow.append(self.paragraph("- "+prose(b), "small"))
        for b in cmp_.get("non_comparable") or []:
            boundary_flow.append(self.paragraph("- "+prose(b), "small"))
        self.story.append(KeepTogether(boundary_flow))
        self.md.append("## 数据边界与口径说明（附录）")
        self.md += ["- "+prose(b) for b in (d.get("data_boundaries") or [])]
        self.md += ["- "+prose(b) for b in (cmp_.get("non_comparable") or [])]

        attachments = d["attachments"].get("items", [])
        if attachments:
            att_flow = [self.paragraph("附件", "section")]
            for item in attachments:
                att_flow.append(self.paragraph(prose(item["path"], original=True), "small"))
                att_flow.append(self.paragraph(prose(item.get("description") or ""), "small"))
            self.story.append(KeepTogether(att_flow))
            self.md.append("## 附件")
        return self.story

    def render(self):
        stream = io.BytesIO()
        doc = SimpleDocTemplate(stream, pagesize=(WIDTH, HEIGHT), leftMargin=MARGIN, rightMargin=MARGIN,
                                topMargin=25*mm, bottomMargin=24*mm, title="三平台周期复盘", author="多平台内容运营专家")
        def header(c, doc):
            c.saveState(); c.setFont(FONT, 8.5); c.setFillColor(ACCENT)
            c.drawString(MARGIN, HEIGHT-15*mm, "一、回顾目标 · 二、现状表现 · 三、原因分析 · 四、总结迭代")
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
        pdf_bytes, markdown = MultiCycleReport(data).render()
        with pdf.open("xb") as f:
            f.write(pdf_bytes)
        with md.open("x", encoding="utf-8") as f:
            f.write(markdown)
        print("MULTI_CYCLE_REPORT_READY " + str(pdf))
        return 0
    except (ValueError, OSError, KeyError, TypeError, IndexError) as e:
        print("MULTI_RENDER_ERROR: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
