"""从本地JSON渲染四步作品复盘；兼容旧四页与结构化详细模式。"""
import argparse
import io
import json
import os
import re
import sys
import subprocess
from html import escape
from pathlib import Path

# 独立Python未带排版库时，复用应用已有运行时，不安装或修改环境。
import reportlab

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Image, KeepInFrame, Paragraph, Spacer, Table, TableStyle

PAGE_W, PAGE_H = 595.28, 841.89
LEFT = RIGHT = 18 * mm
TOP = PAGE_H - 32 * mm
BOTTOM = 20 * mm
WIDTH = PAGE_W - LEFT - RIGHT
HEIGHT = TOP - BOTTOM
THEME = colors.HexColor('#1a3a34')
GREY = colors.HexColor('#8a9a94')
LIGHT_GREEN = colors.HexColor('#c0d0c8')
DARK_GREEN = colors.HexColor('#0d4740')
QUOTE_GREEN = colors.HexColor('#4a6a5a')
REPLACEMENTS = {'本轮参考': '参考数据', '留住观看': '留住观众', '本作品': '这条视频',
                '同好': '朋友', '亲历': '真实经历', '本条': '这条视频', '本轮': '这次'}


def local_svg(tree):
    from reportlab.graphics.shapes import Drawing, Group, Rect, Circle, Ellipse, Line, Polygon, PolyLine, String
    from reportlab.graphics.svgpath import SvgPath
    import math
    def mmult(a, b):
        return (a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
                a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
                a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5])
    def translate(x, y):
        return (1, 0, 0, 1, x, y)
    def scale(x, y):
        return (x, 0, 0, y, 0, 0)
    def rotate(angle):
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        return (c, s, -s, c, 0, 0)
    def number(value, default=0):
        match = re.match(r'[-+\d.eE]+', str(value))
        return float(match[0]) if match else default
    def paint(value):
        if value in ('none', '', None):
            return None
        return colors.toColor(value)
    box = [float(v) for v in re.split(r'[ ,]+', tree.get('viewBox', '').strip()) if v]
    width, height = number(tree.get('width'), box[2] if box else 1), number(tree.get('height'), box[3] if box else 1)
    drawing = Drawing(width, height)
    root = Group()
    root.transform = (1, 0, 0, -1, 0, height)
    if box:
        root.transform = mmult(root.transform, (width / box[2], 0, 0, height / box[3],
                                                -box[0] * width / box[2], -box[1] * height / box[3]))
    drawing.add(root)
    def parse(element, parent, inherited):
        tag = element.tag.split('}')[-1]
        if tag in ('defs', 'clipPath', 'title', 'desc', 'metadata'):
            return
        style = dict(inherited)
        style.update(element.attrib)
        style.update(dict(re.findall(r'([\w-]+)\s*:\s*([^;]+)', element.get('style', ''))))
        group = Group()
        for name, arguments in re.findall(r'(matrix|translate|scale|rotate)\(([^)]*)\)', element.get('transform', '')):
            values = [float(v) for v in re.split(r'[ ,]+', arguments.strip()) if v]
            if name == 'matrix':
                matrix = tuple(values)
            elif name == 'translate':
                matrix = translate(values[0], values[1] if len(values) > 1 else 0)
            elif name == 'scale':
                matrix = scale(values[0], values[1] if len(values) > 1 else values[0])
            else:
                matrix = rotate(values[0])
                if len(values) == 3:
                    matrix = mmult(mmult(translate(values[1], values[2]), matrix), translate(-values[1], -values[2]))
            group.transform = mmult(group.transform, matrix)
        parent.add(group)
        get = lambda key, default=0: number(element.get(key), default)
        shape = None
        if tag == 'rect':
            shape = Rect(get('x'), get('y'), get('width'), get('height'), rx=get('rx'), ry=get('ry'))
        elif tag == 'circle':
            shape = Circle(get('cx'), get('cy'), get('r'))
        elif tag == 'ellipse':
            shape = Ellipse(get('cx'), get('cy'), get('rx'), get('ry'))
        elif tag == 'line':
            shape = Line(get('x1'), get('y1'), get('x2'), get('y2'))
        elif tag in ('polygon', 'polyline'):
            points = [float(v) for v in re.split(r'[ ,]+', element.get('points', '').strip()) if v]
            shape = (Polygon if tag == 'polygon' else PolyLine)(points)
        elif tag == 'path':
            shape = SvgPath(element.get('d', ''))
        elif tag == 'text':
            shape = String(get('x'), get('y'), wording(''.join(element.itertext()), template_only=False),
                           fontName='YaHeiBold' if 'bold' in (style.get('font-family', '') + style.get('font-weight', '')).lower() else 'YaHei',
                           fontSize=number(style.get('font-size'), 9), textAnchor=style.get('text-anchor', 'start'))
            text_group = Group(shape)
            text_group.transform = (1, 0, 0, -1, 0, get('y') * 2)
            shape = text_group
        elif tag not in ('svg', 'g', 'a'):
            raise ValueError('不支持的SVG元素：' + tag)
        if shape is not None:
            target = shape.contents[0] if tag == 'text' else shape
            if hasattr(target, 'fillColor'):
                target.fillColor = paint(style.get('fill', 'black'))
            if hasattr(target, 'strokeColor'):
                target.strokeColor = paint(style.get('stroke', 'none'))
                target.strokeWidth = number(style.get('stroke-width'), 1)
            group.add(shape)
        if tag != 'text':
            for child in element:
                parse(child, group, style)
    parse(tree, root, {})
    return drawing


def wording(text, template_only=True):
    text = '' if text is None else str(text)
    if template_only:
        for old, new in REPLACEMENTS.items():
            text = text.replace(old, new)
    return text


def obj(value):
    return value if isinstance(value, dict) else {}


def items(value):
    return value if isinstance(value, list) else []


def register_fonts():
    regular = Path(r'C:\Windows\Fonts\msyh.ttc')
    bold = Path(r'C:\Windows\Fonts\msyhbd.ttc')
    fallback = Path(r'C:\Windows\Fonts\simsun.ttc')
    if regular.is_file() and bold.is_file():
        pdfmetrics.registerFont(TTFont('YaHei', str(regular), subfontIndex=0))
        pdfmetrics.registerFont(TTFont('YaHeiBold', str(bold), subfontIndex=0))
        adjustment = 0
    else:
        pdfmetrics.registerFont(TTFont('YaHei', str(fallback), subfontIndex=0))
        pdfmetrics.registerFont(TTFont('YaHeiBold', str(fallback), subfontIndex=0))
        adjustment = 1
    pdfmetrics.registerFontFamily('YaHei', normal='YaHei', bold='YaHeiBold',
                                italic='YaHei', boldItalic='YaHeiBold')
    return adjustment


class Report:
    def __init__(self, data, base):
        self.data = data
        self.base = base.resolve()
        offset = register_fonts()
        self.styles = {
            'body': ParagraphStyle('body', fontName='YaHei', fontSize=9.5 + offset,
                                   leading=15 + offset, spaceAfter=6, wordWrap='CJK',
                                   textColor=colors.HexColor('#2c3a34')),
        }
        for name, size, color, bold in [('title', 18, THEME, True), ('heading', 13, THEME, True),
                                        ('note', 7, GREY, False)]:
            self.styles[name] = ParagraphStyle(name, parent=self.styles['body'],
                                               fontName='YaHeiBold' if bold else 'YaHei',
                                               fontSize=size + offset, leading=size * 1.45 + offset,
                                               spaceBefore=5 if bold else 0)
        # 引用原片台词：缩进+左边框+灰绿色，一眼看出是引文
        self.styles['quote'] = ParagraphStyle('quote', parent=self.styles['body'],
                                              fontName='YaHei', fontSize=9 + offset,
                                              leading=14 + offset, textColor=QUOTE_GREEN,
                                              leftIndent=12, borderPadding=6,
                                              borderWidth=2, borderColor=LIGHT_GREEN)
        # 建议改法：加粗+深绿色，一眼看出是建议
        self.styles['suggested'] = ParagraphStyle('suggested', parent=self.styles['body'],
                                                  fontName='YaHeiBold', fontSize=9.5 + offset,
                                                  leading=15 + offset, textColor=DARK_GREEN,
                                                  leftIndent=8, spaceBefore=2, spaceAfter=2)
        # 行动清单
        self.styles['checklist'] = ParagraphStyle('checklist', parent=self.styles['body'],
                                                  fontName='YaHei', fontSize=9.5 + offset,
                                                  leading=16 + offset, leftIndent=8)
        # 收尾鼓励
        self.styles['closing'] = ParagraphStyle('closing', parent=self.styles['body'],
                                                fontName='YaHei', fontSize=9.5 + offset,
                                                leading=15 + offset, textColor=THEME, alignment=1)
        self.pages = [[], [], [], []]
        self.markdown = [[], [], [], []]

    def paragraph(self, text, style='body', template_only=True):
        safe = escape(wording(text, template_only=template_only))
        safe = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', safe)
        return Paragraph(safe.replace('\n', '<br/>'), self.styles[style])

    def add(self, page, text, style='body', template_only=True):
        if text is None or text == '':
            return
        self.pages[page].append(self.paragraph(text, style, template_only=template_only))
        prefix = '# ' if style == 'title' else '## ' if style == 'heading' else ''
        prefix = '> ' if style == 'quote' else prefix
        self.markdown[page].append(prefix + wording(text, template_only=template_only))

    def section(self, page, number, title, text=None):
        self.add(page, f'{page + 1}.{number} {title}', 'heading')
        self.add(page, text)

    def asset(self, relative):
        if not isinstance(relative, str) or not relative:
            return None
        path = (self.base / relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(self.base):
            raise ValueError('附件必须是JSON目录内的相对路径')
        if not path.is_file():
            print('跳过缺少的附件：' + relative, file=sys.stderr)
            return None
        return path

    def chart(self, page, relative):
        path = self.asset(relative)
        if not path:
            return
        if path.suffix.lower() == '.svg':
            import xml.etree.ElementTree as ET
            raw = path.read_text(encoding='utf-8-sig')
            if re.search(r'<!ENTITY', raw, re.I):
                raise ValueError('SVG不允许文档类型或实体声明')
            raw = re.sub(r'<!DOCTYPE[^>\[]*>', '', raw, flags=re.I)
            if re.search(r'<!DOCTYPE', raw, re.I):
                raise ValueError('SVG不允许内部文档类型声明')
            tree = ET.fromstring(raw)
            for element in tree.iter():
                for key, value in element.attrib.items():
                    if key.split('}')[-1] in ('href', 'src') and not value.startswith('#'):
                        raise ValueError('SVG不允许引用外部资源')
                    if any(not re.fullmatch(r'\s*#[\w.-]+\s*', link) for link in re.findall(r'url\((.*?)\)', value, re.I)):
                        raise ValueError('SVG不允许引用外部样式资源')
                if element.tag.split('}')[-1] == 'style' and re.search(r'@import|url\(', element.text or '', re.I):
                    raise ValueError('SVG不允许引用外部样式资源')
            drawing = local_svg(tree)
            if drawing is None or drawing.width <= 0 or drawing.height <= 0:
                raise ValueError('无法解析SVG附件')
            def visit(node):
                if hasattr(node, 'fontName'):
                    node.fontName = 'YaHeiBold' if 'bold' in node.fontName.lower() else 'YaHei'
                for child in getattr(node, 'contents', []):
                    visit(child)
            visit(drawing)
            scale = min(WIDTH / drawing.width, 85 / drawing.height)
            drawing.scale(scale, scale)
            drawing.width *= scale
            drawing.height *= scale
            self.pages[page].extend([drawing, Spacer(1, 4)])
        else:
            image = Image(str(path))
            scale = min(WIDTH / image.imageWidth, 85 / image.imageHeight)
            image.drawWidth = image.imageWidth * scale
            image.drawHeight = image.imageHeight * scale
            self.pages[page].append(image)

    def table(self, page, rows):
        if not rows:
            return
        count = max(len(row) for row in rows)
        cells = [[self.paragraph(value, 'note') for value in list(row) + [''] * (count - len(row))] for row in rows]
        table = Table(cells, colWidths=[WIDTH / count] * count, splitByRow=0, splitInRow=0)
        table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                                   ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e8f0ed')),
                                   ('GRID', (0, 0), (-1, -1), .3, colors.HexColor('#d0d8d4'))]))
        self.pages[page].append(table)
        self.markdown[page].extend('；'.join(wording(v, template_only=False) for v in row) for row in rows)

    def compose(self):
        d = self.data
        creator, video, metrics = (obj(d.get(k)) for k in ('creator', 'video', 'metrics'))
        classification, overview, chosen, suggestions = (obj(d.get(k)) for k in ('classification', 'overview', 'chosen_approach', 'suggestions'))
        reference = obj(d.get('reference'))
        attachments = obj(d.get('attachments'))
        charts = obj(attachments.get('charts'))
        # === 第1页 ===
        self.add(0, '作品复盘：' + wording(video.get('title', ''), template_only=False), 'title')
        labels = [('播放量', 'plays'), ('收藏', 'saves'), ('新增关注', 'new_follows')]
        cards = []
        for label, key in labels:
            value = metrics.get(key)
            number = f'{value:,}' if isinstance(value, (int, float)) else wording(value, template_only=False)
            cards.append([self.paragraph(label, 'note'), Paragraph(escape(number), ParagraphStyle(
                'card', fontName='YaHeiBold', fontSize=22, leading=28, textColor=colors.HexColor('#0d4740')))])
        card = Table([[Table([[v] for v in cell], colWidths=[WIDTH / 3 - 18]) for cell in cards]], colWidths=[WIDTH / 3] * 3)
        card.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f0f2f1')),
                                  ('ROUNDEDCORNERS', [7, 7, 7, 7])]))
        self.pages[0].append(card)
        self.markdown[0].append('；'.join(label + '：' + wording(metrics.get(key), template_only=False) for label, key in labels if key in metrics))
        self.add(0, ' '.join(wording(video.get(k), template_only=False) for k in ('snapshot_date', 'snapshot_type')), 'note')
        self.section(0, 1, '这条视频想做什么')
        parts = []
        primary = '——'.join(wording(classification[k], template_only=False) for k in ('primary_type_label', 'purpose_description') if classification.get(k))
        if primary: parts.append(primary)
        if classification.get('key_metrics'): parts.append('主要看' + wording(classification['key_metrics'], template_only=False))
        if classification.get('content_style'): parts.append(wording(classification['content_style'], template_only=False) + '是内容形式')
        if classification.get('format'): parts.append(wording(classification['format'], template_only=False) + '是表现形式')
        self.add(0, '。'.join(parts))
        self.section(0, 2, '总体表现')
        for key in ('overall_judgment', 'key_data_sentence', 'next_direction'):
            self.add(0, overview.get(key))
        self.section(0, 3, '做得好的地方')
        for strength in items(d.get('strengths')):
            strength = obj(strength)
            self.add(0, wording(strength.get('text'), template_only=False) + ' ' + wording(strength.get('timestamp'), template_only=False))
        self.section(0, 4, '可以试试的改进', chosen.get('direction'))
        self.add(0, reference.get('source_description'), 'note')

        # === 第2页 ===
        self.add(1, '数据评价', 'title')
        ref_metrics = obj(reference.get('metrics'))
        own_metrics = dict(metrics)
        plays = metrics.get('plays')
        if isinstance(plays, (int, float)) and not isinstance(plays, bool) and plays > 0:
            for rate_key, count_key in [('save_rate_pct', 'saves'), ('follow_rate_pct', 'new_follows'), ('share_rate_pct', 'shares')]:
                count = metrics.get(count_key)
                if own_metrics.get(rate_key) is None and isinstance(count, (int, float)) and not isinstance(count, bool):
                    own_metrics[rate_key] = count / plays * 100
        groups = [('起始观看', [('2秒跳出率', 'exit_2s_rate_pct', '%'), ('5秒完播率', 'retention_5s_rate_pct', '%')], 'starting_comparison'),
                  ('持续观看', [('完整看完比例', 'completion_rate_pct', '%'), ('平均观看时长', 'avg_watch_seconds', '秒')], 'sustained_comparison'),
                  ('收藏和关注', [('收藏率', 'save_rate_pct', '%'), ('吸粉率', 'follow_rate_pct', '%')], 'save_follow_comparison'),
                  ('分享', [('分享率', 'share_rate_pct', '%')], 'share_comparison')]
        for index, (title, fields, chart_key) in enumerate(groups, 1):
            self.section(1, index, title)
            for label, key, unit in fields:
                own_value, ref_value = own_metrics.get(key), ref_metrics.get(key)
                if own_value is None and ref_value is None:
                    continue
                own_text = f'{own_value:.2f}{unit}' if isinstance(own_value, (int, float)) else '—'
                ref_text = f'{ref_value:.2f}{unit}' if isinstance(ref_value, (int, float)) else '—'
                difference = '—'
                if isinstance(own_value, (int, float)) and isinstance(ref_value, (int, float)):
                    difference = f'{own_value - ref_value:+.2f}' + ('pp' if unit == '%' else '秒')
                direction = '越低越好' if key == 'exit_2s_rate_pct' else '越高越好'
                self.add(1, f'{label}：{own_text} / 参考 {ref_text} / 差距 {difference}（{direction}）')
            if index == 3:
                for key, label in [('saves', '收藏'), ('profile_visits', '主页访问'), ('new_follows', '新增关注')]:
                    if metrics.get(key) is not None:
                        self.add(1, f'{label}：{metrics[key]} 次')
            if index == 4 and metrics.get('shares') is not None:
                self.add(1, f'分享次数：{metrics["shares"]} 次')
            self.chart(1, charts.get(chart_key))
        self.add(1, reference.get('source_description'), 'note')
        self.add(1, reference.get('scope_note'), 'note')
        for label, value in [('本条视频时长', video.get('duration_seconds')), ('参考视频时长', reference.get('source_video_duration_seconds'))]:
            if value is not None:
                self.add(1, f'{label}：{value} 秒', 'note')

        # === 第3页 ===
        self.add(2, '原片诊断', 'title')
        timeline = obj(d.get('timeline'))
        self.section(2, 1, '这条视频是怎么讲的', timeline.get('description'))
        self.chart(2, charts.get('timeline'))
        self.section(2, 2, '这些地方说得不错')
        for strength in items(d.get('strengths')):
            strength = obj(strength)
            label = wording(strength.get('text'), template_only=False)
            quote = wording(strength.get('quote'), template_only=False)
            ts = wording(strength.get('timestamp'), template_only=False)
            if label:
                self.add(2, label)
            if quote:
                self.add(2, '「' + quote + '」', 'quote')
            if ts:
                self.add(2, ts, 'note')
        frame_cells = []
        for frame in items(attachments.get('frames')):
            info = obj(frame)
            path = self.asset(info.get('path') if info else frame)
            if path:
                image = Image(str(path))
                scale = min(60 * mm / image.imageWidth, 96 / image.imageHeight)
                image.drawWidth = image.imageWidth * scale
                image.drawHeight = image.imageHeight * scale
                frame_cells.append([image, self.paragraph(info.get('caption', path.stem), 'note')])
        for start in range(0, len(frame_cells), 2):
            row = frame_cells[start:start + 2]
            self.pages[2].append(Table([[cell for cell in row]], colWidths=[WIDTH / len(row)] * len(row)))
        self.section(2, 3, '可以再顺一顺的地方')
        for improvement in items(d.get('improvements')):
            improvement = obj(improvement)
            for key, label in [('label', ''), ('current_approach', '现在：'), ('suggested_change', '可以试试：'), ('timestamp', '')]:
                val = improvement.get(key)
                if val:
                    self.add(2, label + wording(val, template_only=False), 'body' if key == 'current_approach' else 'body')

        # 3.4：给出具体示例
        self.section(2, 4, '这次打算怎么改')
        self.add(2, chosen.get('direction'))
        self.add(2, chosen.get('reason'))
        self.add(2, chosen.get('what_stays_same'))
        example = obj(chosen.get('example'))
        if example:
            self.add(2, '举个例子：', 'heading')
            orig = example.get('original')
            sug = example.get('suggested')
            if orig:
                self.add(2, '现在是这样讲的：', 'body')
                self.add(2, '「' + wording(orig, template_only=False) + '」', 'quote')
            if sug:
                self.add(2, '改成这样会更清楚：', 'body')
                self.add(2, wording(sug, template_only=False), 'suggested')
            why = example.get('reason')
            if why:
                self.add(2, '为什么：' + wording(why, template_only=False))

        # === 第4页 ===
        self.add(3, '改进行动', 'title')
        self.section(3, 1, '只改这一件事')
        self.add(3, '题目：' + wording(suggestions.get('suggested_title'), template_only=False))
        self.add(3, suggestions.get('main_change'))
        self.section(3, 2, suggestions.get('structure_heading') or '顺序不用大动', suggestions.get('structure_description'))
        original, proposed = suggestions.get('original_word_count'), suggestions.get('word_count')
        if isinstance(original, (int, float)) and isinstance(proposed, (int, float)) and original > 0 and abs(proposed - original) / original > .3:
            self.add(3, f'建议稿约 {proposed} 字，预计 {wording(suggestions.get("estimated_duration"), template_only=False)}。原片约 {original} 字、{wording(suggestions.get("original_duration"), template_only=False)}。', 'note')

        # 4.3：区分原话 / 建议 / 说明
        self.section(3, 3, '替换示例')
        for replacement in items(suggestions.get('replacements')):
            replacement = obj(replacement)
            label = replacement.get('section_label')
            if label:
                self.add(3, label, 'heading')
            orig = replacement.get('original_quote')
            sug = replacement.get('suggested_text')
            visual = replacement.get('visual_instruction')
            reason = replacement.get('reason')
            if orig:
                self.add(3, '原来：', 'body')
                self.add(3, '「' + wording(orig, template_only=False) + '」', 'quote')
            if sug:
                self.add(3, '建议改成：', 'body')
                self.add(3, wording(sug, template_only=False), 'suggested')
            if visual:
                self.add(3, '画面：' + wording(visual, template_only=False))
            if reason:
                self.add(3, '理由：' + wording(reason, template_only=False))

        self.section(3, 4, '发完之后看什么')
        recovery = obj(suggestions.get('recovery'))
        if recovery.get('check_day') is not None:
            self.add(3, f'发布后第 {recovery["check_day"]} 天记录这些数据：')
        for key in ('metrics_to_watch', 'key_feedback_topic', 'success_indicator', 'risk_indicator', 'fallback_action'):
            if recovery.get(key):
                self.add(3, recovery.get(key))

        # 行动清单
        checklist = items(d.get('checklist'))
        if checklist:
            self.add(3, '', 'body')
            self.add(3, '行动清单', 'heading')
            for item_text in checklist:
                if item_text and isinstance(item_text, str):
                    self.add(3, '☐  ' + wording(item_text, template_only=False), 'checklist')

        # 收尾鼓励
        closing = d.get('closing_note')
        if closing:
            self.add(3, '', 'body')
            self.add(3, wording(closing, template_only=True), 'closing')

        note = wording(d.get('attachments_note'), template_only=True) or '完整口播稿、拍摄清单、数据核算见附件。'
        self.add(3, note, 'note')

    def decorate(self, canvas, page, physical_page=None):
        canvas.saveState()
        labels = ['分类定目的', '看数据达成', '拆原因', '给改进建议']
        for index, label in enumerate(labels):
            active = index == page
            canvas.setFont('YaHeiBold' if active else 'YaHei', 10 if active else 8.5)
            canvas.setFillColor(THEME if active else GREY)
            canvas.drawCentredString(LEFT + WIDTH * (index + .5) / 4, PAGE_H - 12 * mm, f'{index + 1} {label}')
            if index < len(labels) - 1:
                canvas.setFont('YaHei', 8.5)
                canvas.setFillColor(GREY)
                canvas.drawCentredString(LEFT + WIDTH * (index + 1) / 4, PAGE_H - 12 * mm, '→')
        canvas.setFillColor(THEME)
        canvas.setFont('YaHei', 8)
        canvas.drawString(LEFT, PAGE_H - 20 * mm, f'第 {page + 1} 步 / 共 4 步')
        canvas.setStrokeColor(colors.HexColor('#d0d8d4'))
        canvas.line(LEFT, 15 * mm, PAGE_W - RIGHT, 15 * mm)
        canvas.setFont('YaHei', 7)
        canvas.setFillColor(GREY)
        account = wording(obj(self.data.get('creator')).get('name'), template_only=False)
        footer = '多平台内容运营专家 · 单条作品复盘 · ' + account
        available = WIDTH - 55
        size = min(7, available / max(pdfmetrics.stringWidth(footer, 'YaHei', 7), 1) * 7)
        canvas.setFont('YaHei', size)
        canvas.drawString(LEFT, 9 * mm, footer)
        canvas.setFont('YaHei', 7)
        canvas.drawRightString(PAGE_W - RIGHT, 9 * mm, f'第 {physical_page if physical_page is not None else page + 1} 页')
        canvas.restoreState()

    def pdf_bytes(self):
        stream = io.BytesIO()
        canvas = Canvas(stream, pagesize=(PAGE_W, PAGE_H))
        for page, flowables in enumerate(self.pages):
            self.decorate(canvas, page)
            content = KeepInFrame(WIDTH, HEIGHT, flowables, mode='shrink', hAlign='LEFT', vAlign='TOP')
            content.wrapOn(canvas, WIDTH, HEIGHT)
            scale = getattr(content, '_scale', 1)
            if scale > 1.35:
                raise ValueError(f'第{page + 1}页内容过多（缩放{scale:.1f}x），请精简内容后重试')
            content.drawOn(canvas, LEFT, TOP - content.height)
            canvas.showPage()
        canvas.save()
        return stream.getvalue()


def build_report(data, directory):
    if not isinstance(data, dict):
        raise ValueError('JSON顶层必须是对象')
    directory = Path(directory).resolve()
    targets = [directory / '作品复盘报告.pdf', directory / '作品复盘报告.md']
    if any(path.exists() for path in targets):
        raise FileExistsError('输出文件已存在，请使用新的JSON目录以保留旧文件')
    report = Report(data, directory)
    if 'report_prose' in data or 'analysis_detail' in data:
        import importlib.util
        renderer_name = 'render_review_prose.py' if 'report_prose' in data else 'render_review_detail.py'
        spec = importlib.util.spec_from_file_location('content_ops_render_review_detail', Path(__file__).resolve().with_name(renderer_name))
        detail_renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(detail_renderer)
        pdf, markdown = detail_renderer.render(data, directory, report)
        for path, content in zip(targets, [pdf, markdown]):
            with path.open('xb') as handle:
                handle.write(content)
        return targets
    report.compose()
    labels = ['分类定目的', '看数据达成', '拆原因', '给改进建议']
    for page in range(4):
        report.markdown[page].insert(0, ' → '.join(f'{i + 1} {label}' + ('（当前）' if i == page else '') for i, label in enumerate(labels)) + f'；第 {page + 1} 步 / 共 4 步')
    pdf = report.pdf_bytes()
    markdown = '\n\n<!-- page -->\n\n'.join('\n\n'.join(page) for page in report.markdown) + '\n'
    for path, content in zip(targets, [pdf, markdown.encode('utf-8')]):
        with path.open('xb') as handle:
            handle.write(content)
    return targets


def main():
    parser = argparse.ArgumentParser(description='渲染单条作品四页复盘PDF')
    parser.add_argument('json_path', type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.json_path.read_text(encoding='utf-8-sig'))
        build_report(data, args.json_path.resolve().parent)
    except (OSError, ValueError, TypeError, ImportError) as error:
        print('生成失败：' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
