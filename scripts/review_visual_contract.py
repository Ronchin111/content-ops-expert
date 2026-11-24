"""Compact reader view of a complete evidence-based review (standard library)."""

def skeleton():
    return {'version':2,'layout':'five_page_visual','headline':'','verdict':'','audience':'','positioning':'','priority':'','scope_note':'',
        'page_flow':[{'title':'','lead':''} for _ in range(5)],
        'decisions':[], 'data_notes':[], 'timeline_labels':[],
        'focus':{'timeline_index':0,'label':'','meaning':''},'diagnosis_rows':[],
        'visual_frames':[],'visual_groups':[],'visual_schematic':{'input':'','output':'','relation':'','note':'','takeaway':''},'prep_items':[],'shot_rows':[],
        'replicable_steps':[],'follow_up':{'window':'','watch':'','next':''},'invitation':''}


def validate(view,detail,packet,video=None):
    errors=[]
    def fail(path,message):errors.append('report_view.'+path+': '+message)
    def obj(value,path):
        if not isinstance(value,dict):fail(path,'须为对象');return {}
        return value
    def text(value,path,limit):
        if not isinstance(value,str) or not value.strip():fail(path,'须填短句')
        elif len(value)>limit:fail(path,f'超过{limit}字，请选重点或转到底稿，不能缩字塞页')
    def rows(key,count):
        value=view.get(key)
        if not isinstance(value,list):fail(key,'须为数组');return []
        if len(value)!=count:fail(key,f'本版需要{count}项，请合并相近信息')
        return value
    if not isinstance(view,dict):return ['report_view: 须为对象']
    if view.get('version') not in (1,2) or view.get('layout')!='five_page_visual':fail('version/layout','不支持的版式')
    if view.get('version')==2:
        video=obj(video,'video')
        if video.get('platform') not in ('douyin','xiaohongshu','wechat_channels') or video.get('platform')!=packet.get('platform'):
            fail('video.platform','平台须来自已确认素材，不能靠正文提及或猜测')
        if video.get('subject_role') not in ('own','reference') or video.get('subject_role')!=packet.get('subject_role'):
            fail('video.subject_role','作品角色须与来源一致')
        if video.get('platform_confirmed') is not True or packet.get('platform_confirmed') is not True:
            fail('video.platform_confirmed','作品平台必须已确认')
        if not isinstance(video.get('account_profile_verified'),bool) or video.get('account_profile_verified')!=packet.get('account_profile_verified'):
            fail('video.account_profile_verified','账号主页核验状态须独立保存并与来源一致')
        if video.get('work_id')!=packet.get('identity',{}).get('work_id'):fail('video.work_id','作品ID与来源不一致')
        text(video.get('title'),'video.title',200)
        if video.get('duration_seconds')!=packet.get('duration_s'):fail('video.duration_seconds','须使用原片时长')
        if video.get('data_match_method') not in ('work_id','title','none'):fail('video.data_match_method','须说明按ID、按标题或未匹配')
        text(video.get('data_source'),'video.data_source',1000)
        if video.get('data_source')!=packet.get('metrics_source'):fail('video.data_source','须与事实包的实际数据来源一致')
        if video.get('data_match_method')!=packet.get('data_match_method'):fail('video.data_match_method','不得把标题匹配改成ID已核')
        if video.get('data_snapshot_date')!=packet.get('data_snapshot_date'):fail('video.data_snapshot_date','须与事实包快照日期一致')
        from datetime import date
        for key in ('report_date','data_snapshot_date'):
            value=video.get(key)
            if key=='data_snapshot_date' and not packet.get('metrics') and not value:continue
            try:date.fromisoformat(value)
            except (ValueError,TypeError):fail('video.'+key,'须为已知的YYYY-MM-DD日期')
        if video.get('snapshot_date')!=video.get('data_snapshot_date'):fail('video.data_snapshot_date','与兼容字段snapshot_date不一致')
        for i,item in enumerate(rows('page_flow',5)):
            item=obj(item,f'page_flow[{i}]')
            text(item.get('title'),f'page_flow[{i}].title',22)
            text(item.get('lead'),f'page_flow[{i}].lead',85)
    for key,limit in [('headline',36),('verdict',100),('audience',60),('positioning',80),('priority',70),('invitation',55),('scope_note',110)]:
        text(view.get(key),key,limit)
    findings={item['id'] for item in detail.get('findings',[]) if isinstance(item,dict) and isinstance(item.get('id'),str)}
    evidence={item['id']:item for item in packet.get('evidence',[]) if isinstance(item,dict)}
    def finding(value,path):
        if not isinstance(value,str) or value not in findings:fail(path,'未对应到底稿发现')
    decision_rows=rows('decisions',3)
    for i,item in enumerate(decision_rows):
        item=obj(item,f'decisions[{i}]')
        if item.get('kind') not in ('keep','change','first'):fail(f'decisions[{i}].kind','只用keep/change/first')
        text(item.get('label'),f'decisions[{i}].label',12)
        text(item.get('text'),f'decisions[{i}].text',60)
        finding(item.get('finding_ref'),f'decisions[{i}].finding_ref')
    kinds=[item['kind'] for item in decision_rows if isinstance(item,dict) and isinstance(item.get('kind'),str)]
    if sorted(kinds)!=['change','first','keep']:fail('decisions','保留、调整、先做各一项')
    notes=view.get('data_notes')
    if not isinstance(notes,list):fail('data_notes','须为0至3项数组');notes=[]
    if len(notes)>3:fail('data_notes','最多3项，请挑影响行动的判断')
    for i,item in enumerate(notes):
        item=obj(item,f'data_notes[{i}]')
        key=item.get('metric_key')
        if not isinstance(key,str) or key not in packet.get('metrics',{}):fail(f'data_notes[{i}].metric_key','指标不在事实包')
        for name,limit in [('evaluation',65),('action',50)]:text(item.get(name),f'data_notes[{i}].{name}',limit)
        if item.get('title'):text(item['title'],f'data_notes[{i}].title',14)
    if view.get('timeline_groups'):
        groups=view['timeline_groups']
        if not isinstance(groups,list) or len(groups)>8:
            fail('timeline_groups','须为不超过8组的数组')
        else:
            next_index=0
            for i,group in enumerate(groups):
                group=obj(group,f'timeline_groups[{i}]')
                start,end=group.get('start_index'),group.get('end_index')
                if not all(isinstance(x,int) and not isinstance(x,bool) for x in (start,end)) or not 0<=start<=end<len(detail.get('timeline',[])):
                    fail(f'timeline_groups[{i}]','须指向有效连续原片段')
                elif start!=next_index:fail(f'timeline_groups[{i}]','分组须连续覆盖原时间线，不得漏段或重叠')
                else:next_index=end+1
                text(group.get('label'),f'timeline_groups[{i}].label',10 if view.get('version')==2 else 12)
            if next_index!=len(detail.get('timeline',[])):fail('timeline_groups','未覆盖整条时间线')
    else:
        labels=rows('timeline_labels',len(detail.get('timeline',[])))
        for i,label in enumerate(labels):text(label,f'timeline_labels[{i}]',10 if view.get('version')==2 else 12)
        if len(labels)>8:fail('timeline_labels','正文最多8项，请用timeline_groups合并显示，完整分析保留')
    focus=obj(view.get('focus'),'focus')
    index=focus.get('timeline_index')
    if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(detail.get('timeline',[])):fail('focus.timeline_index','须指向已核原片功能段')
    text(focus.get('label'),'focus.label',16)
    text(focus.get('meaning'),'focus.meaning',70)
    for i,item in enumerate(rows('diagnosis_rows',2)):
        item=obj(item,f'diagnosis_rows[{i}]')
        finding(item.get('finding_ref'),f'diagnosis_rows[{i}].finding_ref')
        for name,limit in [('original',44),('why',44),('next',52)]:text(item.get(name),f'diagnosis_rows[{i}].{name}',limit)
    for i,item in enumerate(rows('visual_frames',2)):
        item=obj(item,f'visual_frames[{i}]')
        ref=item.get('evidence_ref')
        if not isinstance(ref,str) or evidence.get(ref,{}).get('kind')!='frame':fail(f'visual_frames[{i}].evidence_ref','须引用实际帧图')
        text(item.get('callout'),f'visual_frames[{i}].callout',40)
    schematic=obj(view.get('visual_schematic'),'visual_schematic')
    for key,limit in [('input',14),('output',14),('relation',18),('note',44),('takeaway',65)]:text(schematic.get(key),'visual_schematic.'+key,limit)
    aspects=[]
    for i,item in enumerate(rows('visual_groups',4)):
        item=obj(item,f'visual_groups[{i}]')
        text(item.get('label'),f'visual_groups[{i}].label',12)
        for name,limit in [('original',50),('shot',65)]:text(item.get(name),f'visual_groups[{i}].{name}',limit)
        keys=item.get('aspects')
        if not isinstance(keys,list) or not all(isinstance(key,str) for key in keys):fail(f'visual_groups[{i}].aspects','须列出所合并的画面检查项')
        else:aspects.extend(keys)
    expected=['composition','person','lighting','background','props','subtitles','decoration','motion']
    if sorted(aspects)!=sorted(expected):fail('visual_groups','八项画面检查应各出现一次，可合并为四组')
    for i,item in enumerate(rows('prep_items',3)):
        item=obj(item,f'prep_items[{i}]')
        text(item.get('label'),f'prep_items[{i}].label',10)
        text(item.get('text'),f'prep_items[{i}].text',55)
        index=item.get('action_index')
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(detail.get('plan',{}).get('actions',[])):fail(f'prep_items[{i}].action_index','须对应准备动作')
    for i,item in enumerate(rows('shot_rows',4)):
        item=obj(item,f'shot_rows[{i}]')
        for name,limit in [('section',8),('say',95),('show',65)]:text(item.get(name),f'shot_rows[{i}].{name}',limit)
        index=item.get('replacement_index')
        if not isinstance(index,int) or isinstance(index,bool) or not 0<=index<len(detail.get('plan',{}).get('replacements',[])):fail(f'shot_rows[{i}].replacement_index','须对应已分析的局部改句')
    for i,value in enumerate(rows('replicable_steps',4)):text(value,f'replicable_steps[{i}]',14)
    follow=obj(view.get('follow_up'),'follow_up')
    for key,limit in [('window',35),('watch',70),('next',85)]:text(follow.get(key),'follow_up.'+key,limit)
    return errors
