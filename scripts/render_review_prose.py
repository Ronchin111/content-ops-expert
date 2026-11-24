"""Flowing single-work report; retain the existing evidence/analysis contract."""
import io
import re
from datetime import date
from html import escape
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, KeepTogether, HRFlowable
from reportlab.graphics.shapes import Drawing, Rect, String, Line

def skeleton():
    return {'version':1,'summary':'','sections':[]}

def validate(view, detail, packet, video):
    errors=[]
    def fail(msg):errors.append('report_prose: '+msg)
    if not isinstance(view,dict):return ['report_prose: 须为对象']
    if view.get('version')!=1:fail('version须为1')
    if not isinstance(view.get('summary'),str) or not view['summary'].strip():fail('summary须为完整总评')
    for key in ('platform','subject_role','platform_confirmed','account_profile_verified','data_match_method','data_snapshot_date'):
        if video.get(key)!=packet.get(key):fail('video.'+key+'与事实包不符')
    if video.get('platform') not in ('douyin','xiaohongshu','wechat_channels') or video.get('platform_confirmed') is not True:fail('作品平台未确认')
    if video.get('subject_role') not in ('own','reference'):fail('作品角色未确认')
    if video.get('work_id')!=packet.get('identity',{}).get('work_id'):fail('作品ID不符')
    if video.get('data_source')!=packet.get('metrics_source'):fail('数据来源不符')
    if video.get('duration_seconds')!=packet.get('duration_s'):fail('原片时长不符')
    if not isinstance(video.get('title'),str) or not video['title'].strip():fail('作品标题缺失')
    if type(video.get('account_profile_verified')) is not bool:fail('主页核验状态须明确')
    for key in ('report_date','data_snapshot_date'):
        if key=='data_snapshot_date' and not packet.get('metrics') and not video.get(key):continue
        try:date.fromisoformat(video.get(key))
        except (ValueError,TypeError):fail(key+'须为有效日期')
    if video.get('snapshot_date')!=video.get('data_snapshot_date'):fail('旧快照日期与当前日期不符')
    evidence={x['id']:x for x in packet.get('evidence',[])}
    findings={x['id'] for x in detail.get('findings',[])}
    sections=view.get('sections')
    if not isinstance(sections,list) or not sections:return errors+['report_prose: 缺正文章节']
    covered=set()
    for section in sections:
        if not isinstance(section,dict):fail('章节须为对象');continue
        if not isinstance(section.get('title'),str) or not section['title'].strip():fail('缺章节标题')
        paras=section.get('paragraphs')
        if not isinstance(paras,list) or not paras or any(not isinstance(p,str) or not p.strip() for p in paras):fail('缺完整段落');paras=[]
        presentation=section.get('presentation',[])
        if not isinstance(presentation,list):fail('presentation须为数组');presentation=[]
        for item in presentation:
            if not isinstance(item,dict):fail('段落标识须为对象');continue
            if type(item.get('at')) is not int or not 0<=item['at']<len(paras):fail('段落标识位置越界')
            if item.get('kind') not in ('subhead','note','action'):fail('未知段落标识')
            if item.get('kind') in ('subhead','action') and (not isinstance(item.get('label'),str) or not item['label'].strip()):fail('段落标识缺少标题')
        refs=section.get('finding_refs',[])
        if not isinstance(refs,list) or any(x not in findings for x in refs):fail('发现编号不存在')
        else:covered.update(refs)
        figures=section.get('figures',[])
        if not isinstance(figures,list):fail('figures须为数组');continue
        for fig in figures:
            if not isinstance(fig,dict):fail('插图须为对象');continue
            if type(fig.get('after')) is not int or not 0<=fig['after']<len(paras):fail('插图段落位置越界')
            if not isinstance(fig.get('caption'),str) or not fig['caption'].strip():fail('缺图注')
            kind=fig.get('kind')
            if kind=='frame':
                if evidence.get(fig.get('evidence_ref'),{}).get('kind')!='frame':fail('截图须引用原片帧')
            elif kind=='metrics':
                keys=fig.get('keys',[])
                if not isinstance(keys,list) or not keys or any(k not in packet.get('metrics',{}) for k in keys):fail('图表包含无来源指标')
                if any(not str(k).endswith('_pct') for k in keys):fail('百分比图只接受同量纲指标')
            elif kind!='timeline':fail('未知图形类型')
    for finding in detail.get('findings',[]):
        if finding.get('decision')=='improve' and finding['id'] not in covered:fail('正文未对应优先发现：'+finding['id'])
    return errors

def rich(text):
    safe=re.sub(r'\*\*(.+?)\*\*',r'<b>\1</b>',escape(text))
    return re.sub(r'__(.+?)__',r'<u>\1</u>',safe).replace('\n','<br/>')

def render(data, directory, report):
    import importlib.util
    spec=importlib.util.spec_from_file_location('prose_analysis',Path(__file__).with_name('review_analysis.py'))
    analysis=importlib.util.module_from_spec(spec);spec.loader.exec_module(analysis)
    result=analysis.validate(data,directory)
    if result['errors']:raise ValueError('; '.join(result['errors']))
    view=data['report_prose'];video=data['video'];detail=data['analysis_detail']
    packet=analysis.read(Path(directory)/detail['fact_packet'])
    evidence={x['id']:x for x in packet['evidence']}
    width=489;styles={}
    for name,size,leading,color in [('body',11.5,20,'#202b36'),('small',8,12,'#586779'),('title',20,28,'#183d34'),('heading',13,21,'#183d34')]:
        styles[name]=ParagraphStyle(name,fontName='YaHeiBold' if name in ('title','heading') else 'YaHei',fontSize=size,leading=leading,textColor=colors.HexColor(color),wordWrap='CJK',spaceAfter=8 if name=='body' else 10,spaceBefore=8 if name=='heading' else 0,keepWithNext=name in ('heading','title'))
    styles['subhead']=ParagraphStyle('subhead',parent=styles['heading'],fontSize=11.5,leading=18,spaceBefore=8,spaceAfter=4)
    styles['note']=ParagraphStyle('note',parent=styles['body'],fontSize=9,leading=15,textColor=colors.HexColor('#586779'))
    styles['summary']=ParagraphStyle('summary',parent=styles['body'],fontSize=11,leading=18,borderWidth=.7,borderColor=colors.HexColor('#84948c'),borderPadding=9,backColor=colors.HexColor('#f6f8f7'),spaceBefore=5,spaceAfter=17)
    def p(text,style='body'):return Paragraph(rich(text),styles[style])
    platform={'douyin':'抖音','xiaohongshu':'小红书','wechat_channels':'视频号'}[video['platform']]
    meta=f"平台：{platform}｜{'自己的作品' if video['subject_role']=='own' else '对标作品'}｜ID：{video['work_id']}｜时长：{video['duration_seconds']:.2f}秒"
    title='作品复盘｜'+video['title']
    story=[p(title,'title'),p(meta,'small'),p('报告日期：'+video['report_date']+'｜数据快照：'+str(video.get('data_snapshot_date') or '未提供'),'small'),p(view['summary'],'summary')]
    md=['# '+title,meta,'报告日期：'+video['report_date'],view['summary']]
    def figure(fig):
        kind=fig['kind'];caption=fig['caption']
        if kind=='frame':
            item=evidence[fig['evidence_ref']];path=analysis.local_file(Path(directory),item['path'])
            image=Image(str(path));image.drawHeight=235;image.drawWidth=235*image.imageWidth/image.imageHeight
            md.append(f"![{caption}]({item['path']})")
            return KeepTogether([image,p(caption,'small')])
        chart=Drawing(width,95 if kind=='timeline' else 28*len(fig['keys'])+22)
        if kind=='timeline':
            x=0;duration=packet['duration_s'];palette=['#b8c7c1','#d4ded9','#7aa38e','#4a7764']
            for i,row in enumerate(detail['timeline']):
                w=(row['end_s']-row['start_s'])/duration*width
                chart.add(Rect(x,35,w,18,fillColor=colors.HexColor(palette[i%4]),strokeColor=colors.white))
                chart.add(String(x,21,str(round(row['start_s']))+'s',fontName='YaHei',fontSize=7))
                x+=w
            chart.add(String(width,21,f'{duration:.0f}s',textAnchor='end',fontName='YaHei',fontSize=7))
            chart.add(String(0,70,'原片功能段时长（仅表示内容安排，不表示流失）',fontName='YaHei',fontSize=9))
        else:
            labels={'exit_2s_rate_pct':'2秒跳出率','retention_5s_rate_pct':'5秒完播率','completion_rate_pct':'全片完播率'}
            for i,key in enumerate(fig['keys']):
                value=packet['metrics'][key];y=chart.height-26-i*28
                chart.add(String(0,y+3,labels.get(key,key),fontName='YaHei',fontSize=9))
                chart.add(Rect(115,y,290,12,fillColor=colors.HexColor('#edf1ef'),strokeColor=None))
                chart.add(Rect(115,y,290*value/100,12,fillColor=colors.HexColor('#3d7460'),strokeColor=None))
                chart.add(String(416,y+2,f'{value:.2f}%',fontName='YaHei',fontSize=9))
        md.append(caption)
        return KeepTogether([chart,p(caption,'small')])
    for section in view['sections']:
        story.append(p(section['title'],'heading'));md.append('## '+section['title'])
        rule=HRFlowable(width='100%',thickness=.4,color=colors.HexColor('#cbd4ce'),spaceAfter=8)
        rule.keepWithNext=True
        story.append(rule)
        for i,para in enumerate(section['paragraphs']):
            marks=[x for x in section.get('presentation',[]) if x['at']==i]
            style='note' if any(x['kind']=='note' for x in marks) else 'body'
            for mark in marks:
                if mark['kind'] in ('subhead','action'):
                    label=mark['label']
                    story.append(p(label,'subhead'));md.append('### '+label)
            paragraph=p(para,style)
            if any(x['kind']=='action' for x in marks) and i+1<len(section['paragraphs']):
                paragraph.keepWithNext=True
            story.append(paragraph);md.append(para)
            for fig in section.get('figures',[]):
                if fig['after']==i:story.append(figure(fig))
    stream=io.BytesIO()
    def decorate(canvas,doc):
        canvas.setFont('YaHei',7);canvas.setFillColor(colors.HexColor('#6e7c75'))
        canvas.drawString(53,28,'内容运营专家 · '+platform+'作品复盘 · '+video['report_date'])
        canvas.drawRightString(542,28,str(doc.page))
    doc=SimpleDocTemplate(stream,pagesize=(595.28,841.89),leftMargin=53,rightMargin=53,topMargin=42,bottomMargin=44,title=video['title'],author='内容运营专家')
    doc.build(story,onFirstPage=decorate,onLaterPages=decorate)
    import pymupdf
    pdf=stream.getvalue()
    with pymupdf.open(stream=pdf,filetype='pdf') as check:
        if not 4<=len(check)<=5:raise ValueError(f'正文为{len(check)}页，请调整重复内容或合理图文尺寸，禁止截断或缩字')
    return pdf,('\n\n'.join(md)+'\n').encode('utf-8')
