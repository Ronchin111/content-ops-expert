"""Five-page reader report: vector charts, evidence images and action tables."""
import io
from html import escape
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, Table, TableStyle

W,H=595.28,841.89
M=36
CW=W-2*M
INK='#173D37'
TEAL='#317B70'
AMBER='#C78632'
MUTED='#65736E'
PALE='#F0F5F2'
SAND='#FBF2E4'
LINE='#DCE5DF'
STEPS=['定目的','看数据','拆原因','给改法']
LABELS={'plays':'播放','likes':'点赞','comments':'评论','saves':'收藏','shares':'分享','profile_visits':'主页访问','new_follows':'新增关注',
        'exit_2s_rate_pct':'2秒跳出率','retention_5s_rate_pct':'5秒完播率','completion_rate_pct':'完整看完比例'}


def stamp(value):
    value=int(value)
    return f'{value//60:02d}:{value%60:02d}'


def compact(value):
    if not isinstance(value,(int,float)):return str(value)
    if value>=100000000:return f'{value/100000000:.1f}亿'
    if value>=10000:return f'{value/10000:.1f}万'
    return f'{value:g}'


class Page:
    def __init__(self):
        self.buffer=io.BytesIO()
        self.c=Canvas(self.buffer,pagesize=(W,H))

    def box(self,x,top,w,h,fill=PALE,stroke=None,r=9):
        self.c.setFillColor(colors.HexColor(fill))
        self.c.setStrokeColor(colors.HexColor(stroke or fill))
        self.c.roundRect(x,H-top-h,w,h,r,fill=1,stroke=bool(stroke))

    def paragraph(self,text,w,size=10.5,bold=False,color=INK):
        style=ParagraphStyle('visual',fontName='YaHeiBold' if bold else 'YaHei',fontSize=size,leading=size*1.43,
                             wordWrap='CJK',textColor=colors.HexColor(color))
        return Paragraph(escape(str(text)).replace('\n','<br/>'),style)

    def text(self,text,x,top,w,size=10.5,bold=False,color=INK,maxh=None):
        p=self.paragraph(text,w,size,bold,color)
        _,height=p.wrap(w,1000)
        if (maxh is not None and height>maxh+.1) or top+height>792:
            raise ValueError(f'图解正文超框：{str(text)[:25]}；请精炼短句，不缩字截断')
        p.drawOn(self.c,x,H-top-height)
        return height

    def header(self,title,step,page,subtitle=None,metadata=None,first_lead=None):
        subtitle_shift=0
        for i,label in enumerate(STEPS):
            x=M+i*CW/4
            if i==step:self.box(x,23,CW/4-8,23,INK,r=6)
            self.text(f'{i+1}  {label}',x+10,27,CW/4-28,9.5,i==step,'#FFFFFF' if i==step else MUTED,maxh=15)
        self.text(f'{page}/5',W-M-26,59,26,9,color=MUTED)
        if metadata:
            self.text(metadata,M,57,CW-35,9.5,True,maxh=15)
            self.text(title,M,82,CW-35,22,True,maxh=32)
            if subtitle:
                height=self.text('作品：'+subtitle,M,121,CW,9.5,color=MUTED,maxh=28)
                subtitle_shift=max(0,height-9.5*1.43)
            if first_lead:self.text(first_lead,M,141+subtitle_shift,CW,9.5,color=MUTED,maxh=28)
        else:
            self.text(title,M,72,CW-35,22,True,maxh=64)
            if subtitle:self.text(subtitle,M,111,CW,9.5,color=MUTED,maxh=28)
        self.c.setStrokeColor(colors.HexColor(LINE))
        self.c.line(M,34,W-M,34)
        self.c.setFont('YaHei',8)
        self.c.setFillColor(colors.HexColor(MUTED))
        self.c.drawString(M,20,'内容运营专家 · '+getattr(self,'footer_label','自有作品复盘')+getattr(self,'footer_suffix',''))
        self.c.drawRightString(W-M,20,f'第 {page} 页')
        return subtitle_shift

    def table(self,top,widths,headers,rows,maxbottom=787):
        content=[[self.paragraph(x,w-16,10,True,'#FFFFFF') for x,w in zip(headers,widths)]]
        content.extend([[self.paragraph(x,w-16,10) for x,w in zip(row,widths)] for row in rows])
        table=Table(content,colWidths=widths,hAlign='LEFT')
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor(INK)),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor(PALE)]),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),
            ('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),
            ('LINEBELOW',(0,0),(-1,-1),.4,colors.HexColor(LINE))]))
        _,height=table.wrap(CW,1000)
        if top+height>maxbottom:raise ValueError('表格超出本页预算，请合并信息或精炼台词')
        table.drawOn(self.c,M,H-top-height)
        return height

    def flow(self,top,labels):
        gap=12
        width=(CW-gap*(len(labels)-1))/len(labels)
        for i,label in enumerate(labels):
            x=M+i*(width+gap)
            self.box(x,top,width,52,PALE)
            self.text(label,x+10,top+10,width-20,10.5,True,maxh=32)
            if i<len(labels)-1:self.text('→',x+width+1,top+17,12,10,color=TEAL)

    def end(self):self.c.showPage()

    def finish(self):
        self.c.save()
        return self.buffer.getvalue()


def render(data,directory,report):
    # Full evidence and compact-view validation runs in render_review_detail first.
    detail=data['analysis_detail']
    view=data['report_view']
    packet=__import__('json').loads((directory/detail['fact_packet']).read_text(encoding='utf-8'))
    metrics=packet['metrics']
    evidence={item['id']:item for item in packet['evidence']}
    p=Page()
    md=[]
    version2=view.get('version')==2
    flow=view.get('page_flow') if version2 else None
    def page_title(index,old):return flow[index]['title'] if flow else old
    def lead(index,old):return flow[index]['lead'] if flow else old
    video=data['video']
    metadata=None
    if version2:
        platform={'douyin':'抖音','xiaohongshu':'小红书','wechat_channels':'视频号'}[video['platform']]
        subject={'own':'自己的作品','reference':'对标作品'}[video['subject_role']]
        metadata=f"平台：{platform}（已确认）｜分析对象：{subject}｜作品ID：{video.get('work_id') or '未取得'}"
        p.footer_label='自有作品复盘' if video['subject_role']=='own' else '对标作品拆解'
        p.footer_suffix=' · '+platform+' · 报告日期 '+video['report_date']

    # 1: one decision, not the complete analysis workbook.
    display_title=video['title']
    if version2 and p.paragraph('作品：'+display_title,CW,9.5).wrap(CW,1000)[1]>28:
        display_title='标题摘录：'+video['title'][:50]+'…（原题全文见附件）'
        attachment=directory/'附件/作品信息.txt'
        attachment.parent.mkdir(exist_ok=True)
        details=metadata+'\n完整标题：'+video['title']+f"\n原片时长：{video['duration_seconds']}秒\n报告日期：{video['report_date']}\n数据日期：{video['data_snapshot_date'] or '未取得'}\n"
        if attachment.exists():
            if attachment.read_text(encoding='utf-8')!=details:raise ValueError('作品信息附件已存在且不同，请用新目录保留原件')
        else:
            with attachment.open('x',encoding='utf-8') as stream:stream.write(details)
    shift=p.header(page_title(0,'先看结论：这次只改一件事'),0,1,display_title,metadata=metadata,first_lead=lead(0,'') if flow else None)
    p.box(M,(173+shift) if version2 else 143,CW,119 if version2 else 149,INK)
    p.text(view['headline'],M+16,(183+shift) if version2 else 156,CW-32,20,True,'#FFFFFF',maxh=58)
    p.text(view['verdict'],M+16,(246+shift) if version2 else 222,CW-32,10.5,color='#E1EEE7',maxh=46 if version2 else 56)
    stats=[('视频时长',f"{packet['duration_s']:.0f}秒"),('快照播放',metrics.get('plays','未取得')),('主页访问',metrics.get('profile_visits','未取得')),('新增关注',metrics.get('new_follows','未取得'))]
    gap=10;sw=(CW-3*gap)/4
    for i,(label,value) in enumerate(stats):
        x=M+i*(sw+gap);p.box(x,309+shift,sw,67)
        p.text(label,x+10,317+shift,sw-20,9.5,color=MUTED)
        p.text(compact(value),x+10,336+shift,sw-20,21,True,maxh=32)
    width=(CW-20)/3
    for i,item in enumerate(view['decisions']):
        x=M+i*(width+10);p.box(x,395+shift,width,122,SAND if item['kind']=='first' else PALE)
        label={'keep':'保留','change':'调整','first':'先做'}[item['kind']]
        title=item['label']
        if title.startswith(label+'：'):title=title[len(label)+1:]
        p.text(label+' · '+title,x+12,408+shift,width-24,11,True,maxh=34)
        p.text(item['text'],x+12,447+shift,width-24,10,maxh=60)
    p.text('讲给谁',M,538,80,12,True)
    p.text(view['audience'],M+85,538,CW-85,10.5,maxh=42)
    p.text('让人记住什么' if version2 else '账号取舍',M,593,80,12,True)
    p.text(view['positioning'],M+85,593,CW-85,10.5,maxh=58)
    p.box(M,670,CW,52,SAND)
    p.text('现在先做：'+view['priority'],M+12,681,CW-24,11,True,maxh=34)
    scope=view['scope_note']
    p.text('资料范围：'+scope,M,743,CW,9.5,color=MUTED,maxh=43)
    md+=['# 1 定目的｜'+page_title(0,'先看结论')]
    if version2:md +=[metadata,'作品：'+video['title'],f"时长：{video['duration_seconds']:.2f}秒｜数据日期：{video['data_snapshot_date'] or '未取得'}｜报告日期：{video['report_date']}",lead(0,'')]
    md +=[view['headline'],view['verdict'],'；'.join(f'{k}：{v}' for k,v in stats)]
    md +=[f"- {item['label']}：{item['text']}" for item in view['decisions']]
    md +=['观众：'+view['audience'],'账号取舍：'+view['positioning'],'先做：'+view['priority'],'资料范围：'+scope]
    p.end()

    # 2: independent percent measures are not drawn as a funnel.
    p.header(page_title(1,'看数据：把数字变成判断'),1,2,lead(1,'快照数据作检查线索；不同指标分别读取，不是同一批人的转化漏斗。' if metrics else '未取得运营指标时不填零、不造表现图，先用原片资料完成内容诊断。'))
    percent_keys=['exit_2s_rate_pct','retention_5s_rate_pct','completion_rate_pct']
    count_keys=['likes','comments','saves','shares','profile_visits','new_follows']
    if metrics:
        p.box(M,145,294,290)
        p.text('观看指标 / %',M+14,159,250,12,True)
        barx=M+14;barw=264
        for i,key in enumerate(percent_keys):
            top=194+i*70;value=metrics.get(key)
            p.text(LABELS[key]+(' ↓' if i==0 else ' ↑'),barx,top,185,10.5,True)
            p.text(f'{value:.2f}%' if value is not None else '未取得',barx+185,top,79,10.5,True)
            p.box(barx,top+25,barw,13,'#DCE8E1',r=4)
            if value is not None and value>0:p.box(barx,top+25,barw*value/100,13,AMBER if i==0 else TEAL,r=min(4,barw*value/200))
        p.c.setFont('YaHei',9.5);p.c.setFillColor(colors.HexColor(MUTED))
        p.c.drawString(barx,H-417,'0')
        p.c.drawCentredString(barx+barw/2,H-417,'50')
        p.c.drawRightString(barx+barw,H-417,'100')
        p.text('↓ 较低更好   ↑ 较高更好；此处不判行业高低。',barx,381,barw,9.5,color=MUTED,maxh=15)
        x=M+310;rw=CW-310
        p.box(x,145,rw,290)
        p.text('互动与人物兴趣 / 次',x+12,159,rw-24,11,True)
        maximum=max([metrics.get(key,0) for key in count_keys]+[1])
        for i,key in enumerate(count_keys):
            top=196+i*36;value=metrics.get(key)
            p.text(LABELS[key],x+12,top,74,9.5)
            if value is not None and value>0:p.box(x+89,top+3,(rw-126)*value/maximum,9,TEAL,r=2)
            p.text(compact(value) if value is not None else '未取',x+rw-29,top,28,10,True)
    else:
        p.text('运营数字未取得，先盘点现有依据',M,153,CW,14,True)
        known=[['时间稿',f"{sum(x.get('kind')=='machine_transcript' for x in evidence.values())}段机器时间稿",'用于功能分段；原话仍按实际核读范围使用'],
               ['画面',f"{sum(x.get('kind')=='frame' for x in evidence.values())}张原片静帧",'用于可见画面诊断，不据此判断连续动效'],
               ['观众反馈','评论正文已取得' if detail['data_reading']['comments']['status']=='available' else '没有评论正文','不以评论总数代替需求或认可'],
               ['账号目标','已确认' if detail['purpose']['goal_status']=='confirmed' else '已有背景作工作判断','目标范围保留，不自动替用户改成招生或销售']]
        p.table(196,[90,160,CW-250],['资料','这次已知什么','怎么使用'],known,maxbottom=435)
    p.text('数字与资料，接到下一步判断',M,457,CW,13,True)
    width=(CW-20)/3
    notes=list(view['data_notes'])
    gaps=[{'metric_key':'资料范围','evaluation':'只使用实际取得的数字，缺失不填零。','action':'按同一发布后窗口保存快照。'},
          {'metric_key':'先查内容','evaluation':'没有数字仍可检查承诺、推进和画面证据。','action':'把优先改法落实到台词与镜头。'},
          {'metric_key':'下次回收','evaluation':'无可比基准时先建立新记录。','action':'另记真实追问和可归属的交流。'}]
    notes.extend(gaps[len(notes):])
    for i,item in enumerate(notes):
        x=M+i*(width+10);p.box(x,486,width,179)
        p.text(item.get('title') if version2 and item.get('title') else LABELS.get(item['metric_key'],item['metric_key']),x+12,499,width-24,11,True,maxh=32)
        h=p.text(item['evaluation'],x+12,535,width-24,10,maxh=73)
        p.text(('' if version2 else '查：')+item['action'],x+12,544+h,width-24,10,bold=True,maxh=111-h)
    p.box(M,691,CW,66,SAND)
    p.text('评论怎么用',M+12,702,100,11,True)
    comments=detail['data_reading']['comments']
    comment_text=comments['conclusion'] if comments['status']=='available' else '没有评论正文。先检查作品是否回答了选题里的问题；取得真实追问后，再决定下一条回答什么。'
    p.text(comment_text,M+112,701,CW-124,10,maxh=47)
    md+=['# 2 看数据｜'+page_title(1,'图表读数')]
    if version2:md.append(lead(1,''))
    md+=['| 指标 | 数值 |','|---|---|']
    md +=[f"| {LABELS.get(key,key)} | {metrics.get(key,'未取得')}{'%' if key.endswith('_pct') and key in metrics else ''} |" for key in percent_keys+count_keys]
    if not metrics:
        md+=['| 资料 | 已知内容 | 怎么使用 |','|---|---|---|']
        md +=[f'| {row[0]} | {row[1]} | {row[2]} |' for row in known]
    md +=[item['evaluation']+' 检查：'+item['action'] for item in notes]
    md +=['评论怎么用：'+comment_text]
    p.end()

    # 3: duration is derived from the selected real segment boundary.
    p.header(page_title(2,'拆脚本：重点在哪里，顺序怎么改'),2,3,lead(2,'用实际时长看内容分配，再把优先调整的两处落到新顺序。'))
    focus=view['focus'];start=detail['timeline'][focus['timeline_index']]['start_s'];duration=packet['duration_s']
    ratio=start/duration
    cx,cy,r=122,H-226,67
    p.c.setFillColor(colors.HexColor('#DEE9E3'));p.c.circle(cx,cy,r,fill=1,stroke=0)
    if ratio>0:
        p.c.setFillColor(colors.HexColor(AMBER));p.c.wedge(cx-r,cy-r,cx+r,cy+r,90,-360*ratio,fill=1,stroke=0)
    p.c.setFillColor(colors.white);p.c.circle(cx,cy,r-17,fill=1,stroke=0)
    p.text(f'{start:.0f}秒',cx-43,210,86,22,True,maxh=35)
    p.text(f'占全片 {ratio*100:.0f}%',cx-49,247,98,9.5,color=MUTED)
    p.box(222,151,CW-186,145,PALE)
    p.text(focus['label'],236,165,CW-214,14,True,maxh=42)
    p.text(focus['meaning'],236,216,CW-214,10.5,maxh=65)
    p.text('环图为进入重点前的时长占比，不代表观众流失。',M,310,CW,9.5,color=MUTED)
    timeline=detail['timeline']
    labels=view['timeline_labels']
    if view.get('timeline_groups'):
        timeline=[{'start_s':detail['timeline'][item['start_index']]['start_s'],'end_s':detail['timeline'][item['end_index']]['end_s']} for item in view['timeline_groups']]
        labels=[item['label'] for item in view['timeline_groups']]
    palette=[INK,TEAL,'#4A8C7B','#71A78D',AMBER,'#D7A45D','#94A69C','#526B60']
    x=M
    for i,segment in enumerate(timeline):
        width=CW*(segment['end_s']-segment['start_s'])/duration
        p.box(x,342,width,23,palette[i%len(palette)],r=0)
        p.text(str(i+1),x+2,347,max(width-3,8),9.5,True,'#FFFFFF',maxh=16)
        x+=width
    for i,label in enumerate(labels):
        x=M+(i%4)*CW/4;top=377+(i//4)*23
        p.text(f'{i+1} · {label}',x,top,CW/4-8,9.5,maxh=28)
    p.text('下一条，用这个顺序',M,433,CW,12,True)
    p.flow(457,view['replicable_steps'])
    p.text('两处优先调整',M,530,CW,12,True)
    p.table(556,[151,151,CW-302],['原片怎么讲','为什么要改','下一条这样改'],[[item['original'],item['why'],item['next']] for item in view['diagnosis_rows']])
    md+=['# 3 拆原因｜'+page_title(2,'脚本')]
    if version2:md.append(lead(2,''))
    md+=[f"{focus['label']}：{start:.2f}秒，占全片{ratio*100:.2f}%。",focus['meaning'],'| 原片时间 | 内容 |','|---|---|']
    md +=[f"| {stamp(seg['start_s'])}–{stamp(seg['end_s'])} | {label} |" for seg,label in zip(timeline,labels)]
    md+=['新顺序：'+' → '.join(view['replicable_steps']),'| 原片 | 为什么 | 改法 |','|---|---|---|']
    md +=[f"| {item['original']} | {item['why']} | {item['next']} |" for item in view['diagnosis_rows']]
    p.end()

    # 4: original stills plus a clearly labeled proposal, never a fake app screenshot.
    p.header(page_title(3,'看画面：原片证据与下一条拍法'),2,4,lead(3,'原片证据放在左边；建议拍法与示意放在同一页。'))
    pw=(CW-20)/3
    for i,item in enumerate(view['visual_frames']):
        x=M+i*(pw+10);frame=evidence[item['evidence_ref']]
        p.box(x,145,pw,267)
        p.text('原片 '+stamp(frame['at_s']),x+10,154,pw-20,10.5,True)
        path=report.asset(frame['path'])
        if path is None:raise ValueError('图解截图缺失')
        from reportlab.lib.utils import ImageReader
        picture=ImageReader(str(path));iw,ih=picture.getSize();scale=min((pw-18)/iw,198/ih)
        dw,dh=iw*scale,ih*scale
        p.c.drawImage(picture,x+(pw-dw)/2,H-177-dh,dw,dh,mask='auto')
        p.text(item['callout'],x+10,379,pw-20,9.5,maxh=29)
    x=M+2*(pw+10)
    p.box(x,145,pw,267,SAND)
    p.text('下次拍法 · 示意',x+10,154,pw-20,10.5,True)
    diagram=view['visual_schematic']
    for top,label in [(192,diagram['input']),(282,diagram['output'])]:
        p.box(x+12,top,pw-24,55,'#FFFFFF',LINE)
        p.text(label,x+23,top+18,pw-46,11,True,maxh=32)
    p.text('↓ '+diagram['relation'],x+16,252,pw-32,9.5,True,maxh=28)
    p.text(diagram['note'],x+12,353,pw-24,9.5,maxh=48)
    p.text('四组检查，直接接到拍法',M,435,CW,12,True)
    p.table(459,[80,199,CW-279],['看哪里','原片观察','下一条怎么拍'],[[item['label'],item['original'],item['shot']] for item in view['visual_groups']],maxbottom=724)
    p.box(M,740,CW,46,SAND)
    p.text(diagram['takeaway'],M+12,752,CW-24,11,True,maxh=32)
    md+=['# 3 拆原因｜'+page_title(3,'画面')]
    if version2:md.append(lead(3,''))
    for item in view['visual_frames']:
        frame=evidence[item['evidence_ref']]
        md +=[f"![原片{stamp(frame['at_s'])}]({frame['path']})",item['callout']]
    md+=['建议示意：'+diagram['input']+' → '+diagram['output']+'。'+diagram['relation']+'。'+diagram['note'],diagram['takeaway'],'| 看哪里 | 原片 | 拍法 |','|---|---|---|']
    md +=[f"| {item['label']} | {item['original']} | {item['shot']} |" for item in view['visual_groups']]
    p.end()

    # 5: the reader can prepare and shoot from this one page.
    p.header(page_title(4,'开始行动：按这张表准备下一条'),3,5,lead(4,'先备材料，再试讲；台词与镜头一行对一行。'))
    width=(CW-20)/3
    for i,item in enumerate(view['prep_items']):
        x=M+i*(width+10);p.box(x,145,width,102)
        p.text(f'{i+1}  '+item['label'],x+10,157,width-20,11,True,maxh=18)
        p.text(item['text'],x+10,185,width-20,10,maxh=58)
    p.table(271,[58,260,CW-318],['段落','可以这样说 / 局部示范','这样拍'],[[item['section'],item['say'],item['show']] for item in view['shot_rows']],maxbottom=624)
    follow=view['follow_up']
    p.box(M,645,CW,99,PALE)
    p.text(follow['window'] if follow['window'].startswith('发布后') else '发布后：'+follow['window'],M+12,656,CW-24,11,True,maxh=18)
    p.text('看什么：'+follow['watch'],M+12,684,(CW-38)/2,10,maxh=58)
    p.text('怎么决定：'+follow['next'],M+CW/2+7,684,(CW-38)/2,10,maxh=58)
    p.text(view['invitation'],M,762,CW,11,True,maxh=30)
    md+=['# 4 给改法｜'+page_title(4,'行动表')]
    if version2:md.append(lead(4,''))
    md +=[f"- {item['label']}：{item['text']}" for item in view['prep_items']]
    md+=['| 段落 | 台词示范 | 拍法 |','|---|---|---|']
    md +=[f"| {item['section']} | {item['say']} | {item['show']} |" for item in view['shot_rows']]
    md+=['观察时间：'+follow['window'],'记录：'+follow['watch'],'下一步：'+follow['next'],view['invitation']]
    p.end()
    return p.finish(),('\n\n'.join(md)+'\n').encode('utf-8')
