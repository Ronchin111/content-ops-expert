"""Render the independent benchmark report from local JSON, without analysis or network.

Normal input produces three A4 pages, with script continuation and optional frame
evidence pages. Source titles and quotations stay verbatim; generated prose uses the existing
word list plus the two benchmark-specific replacements.

Optional presentation-only extensions: comments.summary and
script_analysis.duration_proportions_note. Neither is required, and proportions
are not inferred from prose time ranges when absent from the input contract.
"""
import argparse
import io
import json
import math
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import quote as url_quote

import reportlab

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Image, Paragraph, Table, TableStyle

PAGE_W, PAGE_H = 595.28, 841.89
MARGIN = 18 * mm
WIDTH = PAGE_W - MARGIN * 2
CONTENT_TOP = PAGE_H - 31 * mm
CONTENT_BOTTOM = 20 * mm
CONTENT_HEIGHT = CONTENT_TOP - CONTENT_BOTTOM
ACCENT = colors.HexColor("#234E83")
INK = colors.HexColor("#202B3B")
MUTED = colors.HexColor("#617083")
PALE = colors.HexColor("#EEF3FA")
LINE = colors.HexColor("#D5DFEC")
REPLACEMENTS = {
    "本轮参考": "参考数据", "留住观看": "留住观众", "本作品": "这条视频",
    "同好": "朋友", "亲历": "真实经历", "本条": "这条视频", "本轮": "这次",
}
BENCHMARK_REPLACEMENTS = {"爆款": "对标作品", "爆了": "对标作品"}
PDF_NAME = "对标爆款拆解.pdf"
MD_NAME = "对标爆款拆解.md"


def obj(value):
    return value if isinstance(value, dict) else {}


def rows(value):
    return [item for item in value if item is not None] if isinstance(value, list) else []


def present(value):
    return value is not None and value != ""


def text(value, original=False):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        raise ValueError("Expected a scalar text value, not an object or array")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite number")
        result = format(value, "f")
    else:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite number")
        result = str(value)
    if not original:
        for old, new in {**REPLACEMENTS, **BENCHMARK_REPLACEMENTS}.items():
            result = result.replace(old, new)
    return result


def html(value):
    return escape(str(value)).replace("\r\n", "\n").replace("\n", "<br/>")


def register_fonts():
    """Same Windows YaHei / SimSun fallback as the protected old renderer."""
    regular = Path(r"C:\Windows\Fonts\msyh.ttc")
    bold = Path(r"C:\Windows\Fonts\msyhbd.ttc")
    fallback = Path(r"C:\Windows\Fonts\simsun.ttc")
    if regular.is_file() and bold.is_file():
        files = (regular, bold)
    elif fallback.is_file():
        files = (fallback, fallback)
    else:
        raise ValueError("No existing Chinese font found (YaHei / SimSun); no installation attempted")
    for name, file in zip(("BenchmarkCJK", "BenchmarkCJKBold"), files):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(file), subfontIndex=0))
    pdfmetrics.registerFontFamily(
        "BenchmarkCJK", normal="BenchmarkCJK", bold="BenchmarkCJKBold",
        italic="BenchmarkCJK", boldItalic="BenchmarkCJKBold"
    )


@dataclass
class Block:
    flowable: object
    markdown: str
    gap: float = 4
    keep_next: bool = False


class Report:
    def __init__(self, data):
        self.data = data
        self.has_evaluation = "work_evaluation" in data
        register_fonts()
        body = ParagraphStyle(
            "benchmark_body", fontName="BenchmarkCJK", fontSize=10, leading=14,
            textColor=INK, wordWrap="CJK", splitLongWords=True,
        )
        self.styles = {"body": body}
        for name, size, leading, bold, color in (
            ("chapter", 14, 20, True, ACCENT),
            ("section", 12, 17, True, ACCENT),
            ("small", 8, 11, False, MUTED),
            ("quote", 9, 13, False, INK),
            ("title", 14, 20, True, INK),
        ):
            self.styles[name] = ParagraphStyle(
                "benchmark_" + name, parent=body, fontSize=size, leading=leading,
                fontName="BenchmarkCJKBold" if bold else "BenchmarkCJK",
                textColor=color,
            )
        self.chapters = [[], [], [], [], []]

    def paragraph(self, value, style="body"):
        return Paragraph(html(value), self.styles[style])

    def add(self, chapter, value, style="body", original=False, prefix="", keep=False):
        if not present(value):
            return
        value = text(value, original=original)
        self.chapters[chapter].append(Block(
            self.paragraph(value, style), prefix + value,
            6 if style in ("chapter", "section") else 4, keep
        ))

    def heading(self, chapter, label, major=False):
        self.add(chapter, label, "chapter" if major else "section",
                 original=True, prefix="# " if major else "## ", keep=True)

    def field(self, chapter, label, value, original=False, style="body"):
        if present(value):
            self.add(chapter, label + text(value, original=original), style, original=True)

    def quote(self, chapter, value, metadata="", limit=None):
        if not present(value):
            return
        value = text(value, original=True)
        if limit and len(value) > limit:
            value = value[:limit] + "……"
        self.add(chapter, "「" + value + "」" + metadata, "quote",
                 original=True, prefix="> ")

    def joined(self, values, original=False):
        return " · ".join(text(v, original=original) for v in values if present(v))

    def keep_group(self, blocks):
        table = Table([[[block.flowable for block in blocks]]], colWidths=[WIDTH])
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return Block(table, "\n\n".join(block.markdown for block in blocks), 7)

    def build(self):
        self.overview()
        if self.has_evaluation:
            self.account(obj(self.data.get("account")), 3)
        self.script()
        self.visual_and_action()
        if self.has_evaluation:
            self.conclusion()
        return self.chapters

    def frame_evidence(self, asset_root):
        frames = rows(obj(self.data.get("visual_analysis")).get("frame_evidence"))
        if not frames:
            return []
        if asset_root is None or len(frames) > 8:
            raise ValueError("Frame evidence requires a report directory and at most eight frames")
        root = asset_root.resolve()
        pages = []
        for index, value in enumerate(frames, 1):
            page_start = (index - 1) % 2 == 0
            if not self.has_evaluation and page_start:
                label = "04 画面证据与拍摄指导" if index == 1 else "04 画面证据与拍摄指导（续）"
                pages.append([Block(self.paragraph(label, "chapter"),
                                    "# " + label if index == 1 else "", 5)])
            frame = obj(value)
            fields = ("file", "time_seconds", "title", "observation", "filming_tip")
            if not all(present(frame.get(key)) for key in fields):
                raise ValueError(f"Frame {index} is missing required evidence or guidance")
            relative = Path(text(frame["file"], True))
            source = (root / relative).resolve()
            if (relative.is_absolute() or not source.is_relative_to(root)
                    or source.suffix.lower() not in {".jpg", ".jpeg", ".png"}
                    or not source.is_file()):
                raise ValueError(f"Frame {index} requires a local image inside the report directory")
            width, height = ImageReader(str(source)).getSize()
            scale = min((WIDTH if self.has_evaluation else 320) / width, 190 / height)
            shot = Image(str(source), width=width * scale, height=height * scale)
            shot.hAlign = "LEFT"
            heading = self.joined([seconds(frame["time_seconds"]), frame["title"]])
            caption = text(frame["observation"])
            tip = text(frame["filming_tip"])
            blocks = [
                Block(self.paragraph(heading, "section"),
                      ("### " if self.has_evaluation else "## ") + heading, 4),
                Block(shot, f"![{heading}]({url_quote(relative.as_posix())})", 5),
                Block(self.paragraph("画面证据：" + caption), "画面证据：" + caption, 3),
                Block(self.paragraph("怎么拍：" + tip), "怎么拍：" + tip, 10),
            ]
            if self.has_evaluation:
                # Keep each screenshot with its evidence and practical instruction.
                pages.append([self.keep_group(blocks)])
            else:
                pages[-1].extend(blocks)
        return pages

    def account(self, ac, chapter):
        if not ac and not self.has_evaluation:
            return
        label = "02 账号人设" if self.has_evaluation else "1.1 账号信息"
        self.heading(chapter, label, self.has_evaluation)
        self.field(chapter, "名称：", ac.get("name"), original=True)
        if "bio" in ac:
            self.field(chapter, "简介：", ac["bio"] if present(ac["bio"]) else "页面未能读取",
                       original=True)
        pins = rows(ac.get("pinned_videos"))[:3]
        if pins:
            self.add(chapter, "置顶作品", "small", original=True)
            for pin in pins:
                self.field(chapter, "• ", pin, original=True, style="small")
        if self.has_evaluation:
            self.field(chapter, "内容定位：", ac.get("positioning"))
            self.field(chapter, "这条作品怎样强化人设：", ac.get("work_fit"))
        self.add(chapter, ac.get("retrieval_note"), "small")

    def conclusion(self):
        conclusion = obj(self.data.get("conclusion"))
        self.heading(4, "05 成功要点与可借鉴打法", True)
        self.add(4, conclusion.get("success_assessment"))
        for label, key in (("账号定位与人设", "account"),
                           ("选题与脚本", "script"), ("画面与拍法", "visual")):
            if present(conclusion.get(key)):
                self.heading(4, label)
                self.add(4, conclusion[key])
        self.field(4, "适用前提：", conclusion.get("conditions"))
        self.field(4, "容易踩的坑：", conclusion.get("risks"))
        action = obj(self.data.get("next_action"))
        if action:
            self.heading(4, "下一步：仿拍自己的作品")
            self.add(4, action.get("prompt"))
            self.add(4, action.get("deliverable"))

    def overview(self):
        v, ac = obj(self.data.get("video")), obj(self.data.get("account"))
        pm, cm = obj(self.data.get("public_metrics")), obj(self.data.get("comments"))
        self.heading(0, "01 作品评价" if self.has_evaluation else "01 作品概览", True)
        if present(v.get("title")):
            title = text(v["title"], original=True)
            size = 14 if len(title) <= 80 else max(8, 14 * (80 / len(title)) ** .5)
            style = ParagraphStyle("benchmark_fitted_title", parent=self.styles["title"],
                                   fontSize=size, leading=size * 1.4)
            self.chapters[0].append(Block(Paragraph(html(title), style), "## " + title, 8))
        self.add(0, self.joined([v.get("platform"), v.get("author_name")], True),
                 "small", original=True)
        self.field(0, "时长：", seconds(v.get("duration_seconds")), original=True, style="small")
        if present(v.get("width")) and present(v.get("height")):
            self.field(0, "分辨率：", text(v["width"], True) + "×" + text(v["height"], True),
                       original=True, style="small")
        self.field(0, "链接：", v.get("url"), original=True, style="small")
        self.field(0, "数据采集日期：", v.get("snapshot_date"), original=True, style="small")
        if not self.has_evaluation:
            self.account(ac, 0)
        metrics = [(label, pm.get(key)) for label, key in (
            ("点赞", "likes_display"), ("评论", "comments_display"),
            ("收藏", "favorites_display"), ("分享", "shares_display"), ("播放", "views_display")
        ) if present(pm.get(key))]
        if metrics:
            self.heading(0, "公开数据" if self.has_evaluation else "1.2 公开数据")
            number_style = ParagraphStyle("metric", parent=self.styles["title"],
                                          fontSize=18, leading=24, alignment=2)
            label_style = ParagraphStyle("metric_label", parent=self.styles["small"], alignment=2)
            table = Table([
                [Paragraph(html(text(val, True)), number_style) for _, val in metrics],
                [Paragraph(html(label), label_style) for label, _ in metrics]
            ], colWidths=[WIDTH / len(metrics)] * len(metrics))
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), PALE),
                ("BOX", (0, 0), (-1, -1), .5, LINE),
                ("INNERGRID", (0, 0), (-1, -1), .4, LINE),
                ("TOPPADDING", (0, 0), (-1, 0), 9),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ]))
            md = "| " + " | ".join(k for k, _ in metrics) + " |\n"
            md += "| " + " | ".join("---:" for _ in metrics) + " |\n"
            md += "| " + " | ".join(text(v, True) for _, v in metrics) + " |"
            self.chapters[0].append(Block(table, md, 7))
        self.field(0, "采集时间：", pm.get("collected_at"), original=True, style="small")
        self.add(0, pm.get("source_note"), "small")
        evaluation = obj(self.data.get("work_evaluation"))
        self.field(0, "数据评价：", evaluation.get("metrics_assessment"))
        comments = [obj(item) for item in rows(cm.get("items")) if obj(item).get("text") is not None][:3]
        if comments or present(cm.get("scope_note")) or present(cm.get("summary")):
            self.heading(0, "评论样本与诉求" if self.has_evaluation else "1.3 评论快照")
            for comment in comments:
                meta = []
                if present(comment.get("likes_display")):
                    meta.append(text(comment["likes_display"], True) + "赞")
                if present(comment.get("date_display")):
                    meta.append(text(comment["date_display"], True))
                self.quote(0, comment.get("text"), "—— " + " · ".join(meta) if meta else "", limit=120)
                self.field(0, "作者回复：", comment.get("author_reply"), original=True, style="quote")
            # Optional presentation text, not a newly required input dependency.
            self.add(0, cm.get("summary"))
            self.add(0, cm.get("scope_note"), "small")
        self.field(0, "评论一致点：", evaluation.get("comment_pattern"))
        self.field(0, "评论的说法好在哪：", evaluation.get("comment_appeal"))
        self.field(0, "整体评价：", evaluation.get("overall"))

    def script(self):
        sa = obj(self.data.get("script_analysis"))
        self.heading(1, "03 脚本" if self.has_evaluation else "02 脚本结构", True)
        self.field(1, "选题击中的问题：", sa.get("topic_assessment"))
        self.field(1, "平台适配：", sa.get("platform_fit"))
        for source in rows(sa.get("platform_sources")):
            source = obj(source)
            if present(source.get("label")) and present(source.get("url")):
                self.field(1, "参考：", self.joined([source["label"], source["url"]], True),
                           original=True, style="small")
        self.heading(1, "结构总览" if self.has_evaluation else "2.1 结构总览")
        self.field(1, "推进线：", sa.get("overview_flow"))
        self.field(1, "总时长：", seconds(sa.get("total_duration_seconds")),
                   original=True, style="small")
        # Proportions are DeepSeek-supplied presentation data, never inferred from
        # free-form time_range or calculated as business metrics by this renderer.
        self.add(1, sa.get("duration_proportions_note"), "small")
        segments = rows(sa.get("segments"))
        if len(segments) > 15:
            raise ValueError("script_analysis.segments exceeds the supported maximum of 15")
        if segments:
            self.heading(1, "逐段拆解" if self.has_evaluation else "2.2 逐段拆解")
        for index, value in enumerate(segments, 1):
            segment_start = len(self.chapters[1])
            seg = obj(value)
            self.add(1, self.joined([f"{index:02}", seg.get("time_range")], True),
                     "section", original=True, keep=True)
            for line in rows(seg.get("key_lines")):
                kl = obj(line)
                source = kl.get("source")
                if source is not None and source not in ("subtitle", "machine_transcript"):
                    raise ValueError("key_lines.source must be subtitle or machine_transcript")
                label = {"subtitle": "画面字幕", "machine_transcript": "机器稿"}.get(source)
                meta = "，".join(text(v, True) for v in
                                 (label, seconds(kl.get("time_seconds"))) if present(v))
                self.quote(1, kl.get("text"), "（" + meta + "）" if meta else "")
            self.field(1, "功能：", seg.get("function"))
            transition = " → ".join(
                text(seg[k]) for k in ("transition_from", "transition_to") if present(seg.get(k))
            )
            self.field(1, "承接：", transition, original=True)
            if not self.has_evaluation:
                self.field(1, "画面：", seg.get("visual"))
            self.field(1, "引文校对：", seg.get("evidence_note"), style="small")
            if self.has_evaluation:
                segment = self.chapters[1][segment_start:]
                group = self.keep_group(segment)
                if height(group) < CONTENT_HEIGHT * .5:
                    self.chapters[1][segment_start:] = [group]
        highlights = rows(sa.get("highlights"))
        if highlights:
            self.heading(1, "结构为什么可能有效" if self.has_evaluation else "2.3 值得学的地方")
        for h in highlights:
            h = obj(h)
            self.add(1, h.get("position"), "section", keep=True)
            metadata = seconds(h.get("time_seconds"))
            self.quote(1, h.get("quote"), "（" + metadata + "）" if metadata else "")
            self.field(1, "为什么有效：", h.get("why_effective"))
            self.field(1, "可以怎样用：", h.get("adaptable_to"))

    def visual_and_action(self):
        va, fw, op = (obj(self.data.get(k)) for k in ("visual_analysis", "framework", "options"))
        self.heading(2, "04 画面" if self.has_evaluation else "03 画面、骨架与行动", True)
        records = []
        if va:
            self.heading(2, "构图、场景与视觉变化" if self.has_evaluation else "3.1 画面逐项记录")
            if present(va.get("frames_checked")):
                self.field(2, "已检查截图数：", va["frames_checked"], original=True, style="small")
            self.add(2, va.get("frames_note"), "small")
            if present(va.get("scene")):
                records.append(("人物场景", text(va["scene"])))
            for label, key in (("构图", "composition"), ("灯光", "lighting"),
                               ("背景与道具", "background_and_props")):
                if present(va.get(key)):
                    records.append((label, text(va[key])))
            sub = obj(va.get("subtitles"))
            if sub.get("has_subtitles") is False:
                records.append(("字幕", "已检查截图中未见字幕"))
            elif sub:
                detail = self.joined([sub.get(k) for k in ("position", "style", "consistency_in_samples")])
                if detail:
                    records.append(("字幕", detail))
            overlay = obj(va.get("title_overlay"))
            if overlay.get("has_title") is False:
                records.append(("抬头标题", "已检查截图中未见抬头标题"))
            elif overlay:
                detail = self.joined([
                    text(overlay["text"], True) if present(overlay.get("text")) else None,
                    text(overlay["position"]) if present(overlay.get("position")) else None,
                    text(overlay["style"]) if present(overlay.get("style")) else None,
                ], True)
                if detail:
                    records.append(("抬头标题", detail))
            graphics = []
            for graphic in rows(va.get("graphics")):
                g = obj(graphic)
                detail = self.joined([seconds(g.get("time_seconds")), g.get("type"),
                                      g.get("description"), g.get("note")])
                if present(g.get("persistent")):
                    detail += (" · " if detail else "") + "常驻标记：" + text(g["persistent"])
                if detail:
                    graphics.append(detail)
            if graphics:
                records.append(("图形", "\n".join(graphics)))
            motion_label = "字幕与画面动效" if self.has_evaluation else "字体动画"
            for label, key in (("画面变化", "frame_changes"), (motion_label, "font_animation_observed")):
                if present(va.get(key)):
                    records.append((label, text(va[key])))
        if records:
            cells = [[self.paragraph(k, "small"), self.paragraph(v)] for k, v in records]
            table = Table(cells, colWidths=[70, WIDTH - 70])
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), PALE),
                ("GRID", (0, 0), (-1, -1), .4, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            md = "| 观察项 | 记录 |\n|---|---|\n" + "\n".join(
                "| " + k + " | " + v.replace("|", r"\|").replace("\n", "<br/>") + " |"
                for k, v in records
            )
            self.chapters[2].append(Block(table, md, 8))
        if self.has_evaluation:
            if rows(va.get("frame_evidence")):
                self.heading(2, "截图与拍摄指导")
            return
        if fw:
            self.heading(2, "3.2 可借鉴骨架")
            self.field(2, "结构步骤数：", fw.get("step_count"), original=True, style="small")
            for i, step in enumerate(rows(fw.get("steps")), 1):
                step = obj(step)
                label = self.joined([step.get("order", i), step.get("label")])
                parts = [label] if label else []
                if present(step.get("original")):
                    parts.append("原作者「" + text(step["original"], True) + "」")
                if present(step.get("replaceable_with")):
                    parts.append("可替换变量：" + text(step["replaceable_with"]))
                if parts:
                    self.add(2, " · ".join(parts), original=True)
                    self.chapters[2][-1].gap = 3
            columns, markdown = [], []
            for label, key in (("适用条件", "conditions"), ("失败风险", "risks")):
                values = rows(fw.get(key))
                if values:
                    column = [self.paragraph(label, "section")]
                    markdown.append("### " + label)
                    for value in values:
                        value = "• " + text(value)
                        column.append(self.paragraph(value))
                        markdown.append(value)
                    columns.append(column)
            if columns:
                table = Table([columns], colWidths=[WIDTH / len(columns)] * len(columns))
                table.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND", (0, 0), (-1, -1), PALE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
                self.chapters[2].append(Block(table, "\n\n".join(markdown), 6))
        options = [obj(op[k]) for k in ("option_1", "option_2", "option_3") if obj(op.get(k))]
        if options:
            self.heading(2, "3.3 你的下一步")
            for option in options:
                self.add(2, option.get("label"))
                self.field(2, "需要提供：", option.get("user_provides"), style="small")


def seconds(value):
    return text(value, original=True) + "秒" if present(value) else None


def height(block, available=CONTENT_HEIGHT):
    return block.flowable.wrap(WIDTH, available)[1]


def paginate_blocks(blocks, keep_tables=False):
    """Split normal text as needed, keeping titles with the following content."""
    pending = list(blocks)
    pages, current, remaining = [], [], CONTENT_HEIGHT
    while pending:
        block = pending.pop(0)
        need = height(block, remaining)
        next_need = 0
        if block.keep_next and pending:
            following = height(pending[0])
            next_need = following if keep_tables and isinstance(pending[0].flowable, Table) else min(following, 26)
            if keep_tables:
                next_need += block.gap
        if need + next_need <= remaining + .01:
            current.append(block)
            remaining -= need + block.gap
            continue
        if current and block.keep_next:
            pages.append(current)
            current, remaining = [], CONTENT_HEIGHT
            pending.insert(0, block)
            continue
        parts = block.flowable.split(WIDTH, max(0, remaining))
        if parts:
            part = Block(parts[0], "", block.gap)
            part_height = height(part, remaining)
            if part_height <= remaining + .01:
                current.append(part)
                if len(parts) > 1:
                    pending = [Block(p, "", block.gap) for p in parts[1:]] + pending
                pages.append(current)
                current, remaining = [], CONTENT_HEIGHT
                continue
        if current:
            pages.append(current)
            current, remaining = [], CONTENT_HEIGHT
            pending.insert(0, block)
        else:
            raise ValueError("A report content block cannot fit or split on an A4 page")
    if current:
        pages.append(current)
    return pages or [[]]


def ensure_fixed_page(blocks, chapter):
    used = sum(height(block) + block.gap for block in blocks)
    if used > CONTENT_HEIGHT + .01:
        raise ValueError(
            f"Chapter {chapter} exceeds its page ({used:.1f}pt > {CONTENT_HEIGHT:.1f}pt); "
            "return the JSON to DeepSeek to shorten, without clipping or shrinking body text"
        )


def draw_page(canvas, report, blocks, number, total, continuation=""):
    v = obj(report.data.get("video"))
    canvas.saveState()
    details = [v.get("platform"), v.get("author_name"),
               seconds(v.get("duration_seconds")), v.get("snapshot_date")]
    header = "对标爆款拆解"
    extra = report.joined(details, True)
    if extra:
        header += " · " + extra
    header_style = ParagraphStyle("running_header", parent=report.styles["small"], fontSize=9, leading=11)
    header_p = Paragraph(html(header), header_style)
    _, hh = header_p.wrap(WIDTH, 36)
    if hh > 33:
        raise ValueError("Running header is too long for its reserved area")
    header_p.drawOn(canvas, MARGIN, PAGE_H - 14 * mm - hh)
    canvas.setStrokeColor(LINE)
    canvas.line(MARGIN, CONTENT_TOP + 12, PAGE_W - MARGIN, CONTENT_TOP + 12)
    canvas.setFont("BenchmarkCJK", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 12 * mm, "多平台内容运营专家 · 对标爆款拆解")
    canvas.drawRightString(PAGE_W - MARGIN, 12 * mm, f"第 {number} 页 / 共 {total} 页")
    if continuation:
        canvas.drawRightString(PAGE_W - MARGIN, CONTENT_TOP + 15, continuation)
    y = CONTENT_TOP
    for block in blocks:
        h = height(block, y - CONTENT_BOTTOM)
        if y - h < CONTENT_BOTTOM - .1:
            raise ValueError("Internal layout overflow; output aborted")
        block.flowable.drawOn(canvas, MARGIN, y - h)
        y -= h + block.gap
    canvas.restoreState()
    canvas.showPage()


def render(data, asset_root=None):
    report = Report(data)
    overview, script, visual, account, conclusion = report.build()
    evidence_pages = report.frame_evidence(asset_root)
    if report.has_evaluation:
        # The visuals and their evidence share one chapter and one page stream.
        visual = visual + [block for group in evidence_pages for block in group]
        chapters = [overview + account, script, visual, conclusion]
        pages = []
        for chapter, label in zip(chapters, ("作品评价与账号人设", "脚本", "画面", "成功要点与可借鉴打法")):
            for index, page in enumerate(paginate_blocks(chapter, keep_tables=True)):
                pages.append((page, label + "（续）" if index else ""))
    else:
        ensure_fixed_page(overview, 1)
        ensure_fixed_page(visual, 3)
        for number, page in enumerate(evidence_pages, 1):
            ensure_fixed_page(page, f"4-{number}")
        chapters = [overview, script, visual, *evidence_pages]
        pages = [(overview, "")]
        pages += [(page, f"脚本结构 · 第 2-{index} 页" if index else "")
                  for index, page in enumerate(paginate_blocks(script))]
        pages += [(page, "") for page in [visual, *evidence_pages]]
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=(PAGE_W, PAGE_H), pageCompression=1)
    canvas.setTitle("对标爆款拆解")
    canvas.setAuthor("多平台内容运营专家")
    for index, (blocks, continuation) in enumerate(pages, 1):
        draw_page(canvas, report, blocks, index, len(pages), continuation)
    canvas.save()
    markdown = "\n\n".join(
        block.markdown for chapter in chapters for block in chapter if block.markdown
    ) + "\n"
    return buffer.getvalue(), markdown, len(pages)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", type=Path)
    args = parser.parse_args(argv)
    try:
        path = args.json_path.resolve(strict=True)
        # Decimal retains the input time precision instead of formatting to 1dp.
        data = json.loads(path.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        if not isinstance(data, dict):
            raise ValueError("The JSON root must be an object")
        pdf_bytes, markdown, count = render(data, path.parent)
        pdf_path, md_path = path.parent / PDF_NAME, path.parent / MD_NAME
        # Build completely in memory first, so validation failures do not replace
        # earlier PDF/MD. Overwriting these two names is the explicit CLI contract.
        pdf_path.write_bytes(pdf_bytes)
        md_path.write_text(markdown, encoding="utf-8")
        print(f"OK: {count} pages; {pdf_path.name}; {md_path.name}")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
