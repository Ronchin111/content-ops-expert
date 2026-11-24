"""Detailed mode for the existing four-step report; shares its fonts and styles."""
import importlib.util
import io
from pathlib import Path
from reportlab.lib import colors
from reportlab.platypus import BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate, PageBreak, PageTemplate, Spacer, Table, TableStyle

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('content_ops_review_analysis',HERE/'review_analysis.py')
analysis=importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)

METRICS={'plays':'播放','likes':'点赞','comments':'评论','saves':'收藏','shares':'分享','profile_visits':'主页访问','new_follows':'新增关注',
         'exit_2s_rate_pct':'2秒跳出率','retention_5s_rate_pct':'5秒完播率','completion_rate_pct':'完整看完比例',
         'save_rate_pct':'收藏率','follow_rate_pct':'吸粉率','share_rate_pct':'分享率','like_rate_pct':'点赞率','comment_rate_pct':'评论率'}
ASPECTS=dict(zip(analysis.ASPECTS,('构图','人物','灯光与明暗','背景','道具与证据','字幕','装饰','动效与节奏')))
DECISIONS={'keep':'保留','shorten':'压缩','move':'调整顺序','replace':'替换','drop':'删去','improve':'调整'}


def stamp(seconds):
    seconds=int(seconds)
    return f'{seconds//60:02d}:{seconds%60:02d}'


def render(data,directory,report):
    check=analysis.validate(data,directory)
    if check['errors']:
        raise ValueError('详细分析未通过：\n'+'\n'.join(check['errors']))
    if 'report_view' in data:
        spec=importlib.util.spec_from_file_location('content_ops_render_review_visual',HERE/'render_review_visual.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.render(data,directory,report)
    detail=data['analysis_detail']
    packet=analysis.read(analysis.local_file(directory,detail['fact_packet']))
    evidence={item['id']:item for item in packet['evidence']}
    methods={item['id']:item for item in packet['methods']}
    report.styles['heading'].keepWithNext=True
    report.styles['title'].keepWithNext=True
    def add(step,text,style='body'):
        report.add(step,text,style,template_only=False)
    def heading(step,number,title):add(step,f'{step+1}.{number} {title}','heading')
    def basis(refs):
        clips=[evidence[x] for x in refs if evidence[x]['kind']=='machine_transcript']
        frames=[evidence[x] for x in refs if evidence[x]['kind']=='frame']
        parts=[]
        if clips:parts.append('原片 '+stamp(min(x['start_s'] for x in clips))+'–'+stamp(max(x['end_s'] for x in clips)))
        if frames:parts.append('截图 '+ '、'.join(stamp(x['at_s']) for x in frames))
        return '；'.join(parts)
    def evidence_note(step,refs):
        value=basis(refs)
        if value:add(step,value,'note')
    def fields(step,obj,pairs):
        for key,label in pairs:
            if obj.get(key):add(step,label+obj[key])
    def finding_block(item,start=None):
        if start is None:start=len(report.pages[2])
        add(2,DECISIONS[item['decision']]+' · '+item['title'],'heading')
        evidence_note(2,item['evidence_refs'])
        fields(2,item,[('observation','原片：'),('explanation','为什么：'),('action','怎么做：'),('verify','怎样检查：')])
        report.pages[2][start:]=[KeepTogether(report.pages[2][start:])]

    # Step 1 answers purpose and audience before offering a verdict.
    purpose=detail['purpose']
    add(0,'作品复盘：'+data['video']['title'],'title')
    add(0,'先看结论','heading')
    add(0,purpose['overall_judgment'],'suggested')
    heading(0,1,'讲给谁，想让人看懂什么')
    fields(0,purpose,[('audience','观众：'),('situation','处境：'),('viewer_question','要回答的问题：'),('goal','目标：')])
    add(0,'主要任务：'+purpose['primary_task']+'；'+purpose['success_evidence'])
    heading(0,2,'这条内容怎样帮助账号')
    fields(0,purpose['account_fit'],[('helps','能留下的识别：'),('mismatch','需要收拢的地方：'),('decision','建议取舍：')])
    heading(0,3,'这次先做什么')
    add(0,detail['plan']['priority'],'suggested')
    if purpose['goal_status']!='confirmed':
        add(0,'目标依据：沿用现有项目背景作工作判断，尚未以这条作品主页核对账号简介。','note')

    # Step 2 prints only known values and interpretations, no empty benchmark columns.
    readings=detail['data_reading']
    add(1,'数据评价：数字能说明什么','title')
    heading(1,1,'先回答主要目标')
    add(1,readings['goal_result'])
    raw='；'.join(f'{METRICS.get(key,key)} {value:g}' for key,value in packet['metrics'].items() if key in ('plays','likes','comments','saves','shares','profile_visits','new_follows'))
    if raw:add(1,raw)
    for i,row in enumerate(readings['rows'],2):
        key=row['metric_key']
        value=packet['metrics'][key]
        value=f'{value:.2f}%' if key.endswith('_pct') else str(value)
        heading(1,i,METRICS.get(key,key)+'：'+value)
        fields(1,row,[('observation','读数：'),('meaning','说明：'),('not_proven','不能据此认定：'),('inspect_next','回原片检查：')])
    comments=readings['comments']
    heading(1,len(readings['rows'])+2,'评论与观众需求')
    fields(1,comments,[('sample_scope','样本：'),('conclusion','判断：'),('content_action','接到内容上：')])
    add(1,'数据范围：'+packet['metrics_source'],'note')

    # Step 3 restores functional segments, then joins all visual discussion in one place.
    add(2,'原片诊断：人物、脚本与画面','title')
    heading(2,1,'每段讲了什么，怎样接下去')
    for item in detail['timeline']:
        add(2,stamp(item['start_s'])+'–'+stamp(item['end_s'])+'  '+item['task'],'heading')
        add(2,item['content'])
        add(2,'承接：'+item['handoff']+' '+DECISIONS[item['decision']]+'：'+item['reason'])
    for dimension,title,number in [('persona','账号人设：观众怎样记住你',2),('script','脚本：保留什么，先改哪里',3)]:
        section_start=len(report.pages[2])
        heading(2,number,title)
        first=True
        for finding in detail['findings']:
            if finding['dimension']!=dimension:continue
            finding_block(finding,section_start if first else None)
            first=False
    visual_start=len(report.pages[2])
    heading(2,4,'画面：看见了什么，怎样拍得更清楚')
    # A compact evidence sheet beside the one consolidated visual section.
    frame_items=[item for item in packet['evidence'] if item['kind']=='frame']
    if frame_items:
        cited={ref for item in detail['visual_audit']+detail['findings'] for ref in item['evidence_refs']}
        unique=[item for item in frame_items if item['id'] in cited]
        cells=[]
        for item in unique:
            path=analysis.local_file(directory,item['path'])
            picture=Image(str(path))
            scale=min(138/picture.imageWidth,220/picture.imageHeight)
            picture.drawWidth*=scale
            picture.drawHeight*=scale
            caption=report.paragraph('原片 '+stamp(item['at_s']),'note',template_only=False)
            cells.append([picture,caption])
            report.markdown[2].append(f"![原片 {stamp(item['at_s'])}]({item['path']})")
        for start in range(0,len(cells),3):
            row=cells[start:start+3]
            grid=[Table([row],colWidths=[492/len(row)]*len(row)),Spacer(1,8)]
            if start==0:
                report.pages[2][visual_start:]=[KeepTogether(report.pages[2][visual_start:]+grid)]
            else:
                report.pages[2].append(KeepTogether(grid))
    for item in detail['visual_audit']:
        add(2,ASPECTS[item['aspect']]+('（尚未核定）' if item['status']=='unknown' else ''),'heading')
        evidence_note(2,item['evidence_refs'])
        add(2,item['observation']+' '+item['impact'])
        add(2,'拍法：'+item['action'])
    for finding in detail['findings']:
        if finding['dimension']=='visual':
            finding_block(finding)

    # Step 4 links action, local rewrite, materials and verification to the same findings.
    plan=detail['plan']
    add(3,'改进行动：下一条怎样说、怎样拍','title')
    heading(3,1,'先落实这几件事')
    add(3,plan['priority'],'suggested')
    for item in plan['actions']:
        add(3,item['instruction'])
        add(3,'素材：'+item['needed_material']+'；'+{'ready':'已有','to_prepare':'待准备','not_needed':'无需另备'}[item['material_status']])
        add(3,'替代做法：'+item['fallback'])
    heading(3,2,'下一条的顺序与每段拍法')
    for item in plan['structure']:
        add(3,item['name']+' · '+item['task'],'heading')
        add(3,'内容：'+item['content'])
        add(3,'画面：'+item['shot'])
    heading(3,3,'关键位置的改写示范')
    for item in plan['replacements']:
        add(3,item['label'],'heading')
        evidence_note(3,item['source_refs'])
        add(3,'建议说法：','note')
        add(3,item['suggested_text'],'suggested')
        fields(3,item,[('visual_instruction','配合画面：'),('reason','改动理由：'),('transition','接下一段：')])
    heading(3,4,'提炼成以后能复用的结构')
    fields(3,plan['transfer'],[('skeleton','结构：'),('variables','要换的材料：'),('conditions','适用条件：'),('failure_risks','容易失效的情况：')])
    heading(3,5,'发完怎样判断这次改得值不值')
    fields(3,plan['follow_up'],[('window','观察时间：'),('watch','记录：'),('interpretation','怎样读：'),('next_decision','下一步：')])
    add(3,plan['invitation'],'suggested')
    used=sorted({ref for finding in detail['findings'] for ref in finding['method_refs']})
    add(3,'方法依据：'+'；'.join(methods[ref]['name']+'（'+methods[ref]['locator']+'）' for ref in used)+'。这些是据已读资料整理的分析办法，不代表改稿效果已验证。','note')

    buffer=io.BytesIO()
    left=right=51.024
    width=595.28-left-right
    bottom=56.693
    top=841.89-90.709
    height=top-bottom
    doc=BaseDocTemplate(buffer,pagesize=(595.28,841.89),leftMargin=left,rightMargin=right,topMargin=90.709,bottomMargin=bottom)
    for step in range(4):
        frame=Frame(left,bottom,width,height,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
        doc.addPageTemplates(PageTemplate(id=f'step{step}',frames=[frame],onPage=lambda canvas,document,s=step:report.decorate(canvas,s,physical_page=document.page),autoNextPageTemplate=f'step{step}'))
    story=[]
    labels=['分类定目的','看数据达成','拆原因','给改进建议']
    for step in range(4):
        report.markdown[step].insert(0,' → '.join(f'{i+1} {label}'+('（当前）' if i==step else '') for i,label in enumerate(labels))+f'；第 {step+1} 步 / 共4步')
        if step:story.extend([NextPageTemplate(f'step{step}'),PageBreak()])
        story.extend(report.pages[step])
    doc.build(story)
    markdown='\n\n<!-- step -->\n\n'.join('\n\n'.join(page) for page in report.markdown)+'\n'
    return buffer.getvalue(),markdown.encode('utf-8')
