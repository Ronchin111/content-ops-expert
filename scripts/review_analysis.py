"""Prepare local evidence and validate detailed single-work analysis; no network."""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys

HERE=Path(__file__).resolve().parent
LIBRARY=HERE.parent/'references/review-methods.json'
CHECKS=('opening','audience','progression','proof','sharing','transfer','comments')
ASPECTS=('composition','person','lighting','background','props','subtitles','decoration','motion')


def visual_contract():
    spec=importlib.util.spec_from_file_location('content_ops_review_visual_contract',HERE/'review_visual_contract.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prose_contract():
    spec=importlib.util.spec_from_file_location('content_ops_review_prose',HERE/'render_review_prose.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)


def local_file(base,value):
    if not isinstance(value,str) or not value:
        raise ValueError('附件路径为空')
    path=(base/value).resolve()
    if Path(value).is_absolute() or not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError('附件必须是报告目录中存在的相对文件：'+value)
    return path


def skeleton():
    return {'version':1,'fact_packet':'fact_packet.json',
        'purpose':dict.fromkeys(('audience','situation','viewer_question','primary_task','goal','goal_status','success_evidence','overall_judgment'),'')|{'evidence_refs':[],'account_fit':dict.fromkeys(('helps','mismatch','decision'),'')},
        'data_reading':{'goal_result':'','rows':[],'comments':{'status':'missing','sample_scope':'','evidence_refs':[],'conclusion':'','content_action':''}},
        'timeline':[],'findings':[],
        'coverage':{key:{'status':'','finding_refs':[],'reason':''} for key in CHECKS},
        'visual_audit':[{ 'aspect':key,'status':'','evidence_refs':[],'observation':'','impact':'','action':''} for key in ASPECTS],
        'plan':{'priority':'','actions':[],'structure':[],'replacements':[],
                'transfer':dict.fromkeys(('skeleton','variables','conditions','failure_risks'),''),
                'follow_up':dict.fromkeys(('window','watch','interpretation','next_decision'),''),'invitation':''}}


def prepare(receipt_path,context_path,output):
    receipt=read(receipt_path)
    context=read(context_path)
    if not isinstance(receipt,dict) or not isinstance(context,dict):
        raise ValueError('素材回执和上下文须为JSON对象')
    media=receipt.get('media',{})
    processing=receipt.get('processing',{})
    video=Path(media.get('file','')).resolve()
    parent=Path(receipt_path).resolve().parent
    process_dir=Path(receipt.get('processing_directory','')).resolve()
    if not video.is_file() or not video.is_relative_to(parent) or not process_dir.is_relative_to(parent):
        raise ValueError('回执媒体/处理目录不属于该素材批次')
    digest=hashlib.sha256(video.read_bytes()).hexdigest()
    if digest!=media.get('sha256') or digest!=processing.get('source_sha256'):
        raise ValueError('媒体与处理回执hash不一致')
    transcript=read(process_dir/'transcript.json')
    duration=processing.get('duration_s')
    if not isinstance(duration,(int,float)) or not duration>0 or not transcript.get('segments'):
        raise ValueError('没有可用的时长/时间稿')
    output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    frame_dir=output/'附件/原片'
    frame_dir.mkdir(parents=True)
    if (process_dir/'口播转写.txt').is_file():
        shutil.copy2(process_dir/'口播转写.txt',output/'附件/机器时间稿.txt')
    evidence=[]
    for i,segment in enumerate(transcript['segments'],1):
        evidence.append({'id':f'T{i:03d}','kind':'machine_transcript','start_s':segment['start_s'],
                         'end_s':segment['end_s'],'text':segment['text'],'quote_status':'unreviewed_machine_text'})
    frames=[]
    for i,frame in enumerate(processing.get('frames',[]),1):
        source=Path(frame['file']).resolve()
        if not source.is_relative_to(process_dir) or not source.is_file():
            raise ValueError('帧图来源不属于处理目录')
        relative=f'附件/原片/F{i:02d}{source.suffix}'
        shutil.copy2(source,output/relative)
        item={'id':f'F{i:02d}','kind':'frame','at_s':frame['actual_s'],'path':relative,
              'scope':'one still frame; not evidence of continuous movement or absence of effects'}
        evidence.append(item)
        frames.append({'path':relative,'caption':f"原片 {frame['actual_s']:.2f} 秒"})
    for i,item in enumerate(context.get('facts',[]),1):
        if not item.get('text') or not item.get('source'):
            raise ValueError('上下文事实须带文字和来源')
        evidence.append(dict(item,id=f'C{i:02d}',kind='context'))
    for i,item in enumerate(context.get('comments',[]),1):
        if not item.get('text') or not item.get('source'):
            raise ValueError('评论须有原文和来源')
        evidence.append(dict(item,id=f'R{i:02d}',kind='comment'))
    metrics=dict(context.get('metrics',{}))
    if any(not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<0
           or (key.endswith('_pct') and value>100) for key,value in metrics.items()):
        raise ValueError('指标须为有效非负数；百分数用0至100，未知指标应省略')
    plays=metrics.get('plays')
    if isinstance(plays,(int,float)) and not isinstance(plays,bool) and plays>0:
        for rate,count in [('save_rate_pct','saves'),('follow_rate_pct','new_follows'),('share_rate_pct','shares'),('like_rate_pct','likes'),('comment_rate_pct','comments')]:
            value=metrics.get(count)
            if isinstance(value,(int,float)) and not isinstance(value,bool):
                metrics[rate]=value/plays*100
    if metrics and not context.get('metrics_source'):
        raise ValueError('指标必须附来源、日期与匹配范围')
    evidence.append({'id':'M01','kind':'metrics','source':context.get('metrics_source','未提供作品指标'),
                     'metrics':metrics})
    library=read(LIBRARY)
    platform=receipt.get('platform')
    subject_role=receipt.get('work_role') or context.get('subject_role')
    platform_confirmed=platform in ('douyin','xiaohongshu','wechat_channels')
    profile_verified=context.get('account_profile_verified') is True
    packet={'version':1,'platform':platform,'subject_role':subject_role,'platform_confirmed':platform_confirmed,
            'data_snapshot_date':context.get('snapshot_date',''),'data_match_method':context.get('data_match_method','none'),
            'account_profile_verified':profile_verified,'identity':receipt.get('identity',{}),'public_work_url':receipt.get('public_work_url'),
            'duration_s':duration,'video_sha256':digest,'metrics':metrics,'evidence':evidence,
            'metrics_source':context.get('metrics_source','未提供作品指标'),
            'source_receipt':str(Path(receipt_path).resolve()),'methods':library['methods'],
            'limits':['机器时间稿未逐句听校；可以释义，不冒充精校原话。','静帧仅证明该时刻可见画面；无连续片段时动效记unknown。','账号历史背景不等于当前作品主页身份已核。']}
    write(output/'fact_packet.json',packet)
    report={'creator':{'name':context.get('report_label','自有作品')},
            'video':{'title':context.get('title') or receipt.get('identity',{}).get('title') or '指定作品',
                     'duration_seconds':duration,'snapshot_date':context.get('snapshot_date',''),
                     'platform':platform,'subject_role':subject_role,'platform_confirmed':platform_confirmed,
                     'account_profile_verified':profile_verified,'work_id':receipt.get('identity',{}).get('work_id'),
                     'report_date':datetime.now().astimezone().date().isoformat(),
                     'data_snapshot_date':context.get('snapshot_date',''),
                     'data_source':context.get('metrics_source','未提供作品指标'),
                     'data_match_method':context.get('data_match_method','none')},
            'metrics':metrics,'attachments':{'frames':frames},'analysis_detail':skeleton(),
            'report_prose':prose_contract().skeleton()}
    write(output/'report_data.json',report)
    (output/'执行任务.md').write_text('按 references/review-depth-contract.md、report-prose-layout.md 和事实包methods完成自己的作品复盘。先读事实、看帧图，填写analysis_detail与连贯正文report_prose；不改原始指标，不编业务承诺。执行 review_analysis.py validate 后由 build_report_pdf.py 自然分页为4～5页。旧report_view仅兼容历史稿，空模板不能交付。\n',encoding='utf-8')
    return output


def validate(data,base):
    errors=[]
    warnings=[]
    def fail(path,message):errors.append(path+': '+message)
    def text(obj,key,path):
        value=obj.get(key) if isinstance(obj,dict) else None
        if not isinstance(value,str) or not value.strip():fail(path+'.'+key,'须填写具体文字')
        return value
    def array(obj,key,path,required=True):
        value=obj.get(key) if isinstance(obj,dict) else None
        if not isinstance(value,list):fail(path+'.'+key,'须为数组');return []
        if required and not value:fail(path+'.'+key,'不可为空')
        return value
    def enum(obj,key,allowed,path):
        if not isinstance(obj,dict) or obj.get(key) not in allowed:fail(path+'.'+key,'允许值：'+','.join(allowed))
    detail=data.get('analysis_detail') if isinstance(data,dict) else None
    if not isinstance(detail,dict):return {'errors':['analysis_detail: 缺结构化分析'],'warnings':[]}
    if detail.get('version')!=1:fail('analysis_detail.version','不支持的版本')
    try:packet=read(local_file(Path(base),detail.get('fact_packet')))
    except (OSError,ValueError,TypeError) as exc:return {'errors':['fact_packet: '+str(exc)],'warnings':[]}
    if not isinstance(packet,dict):return {'errors':['fact_packet: 须为JSON对象'],'warnings':[]}
    # Ordinary model outputs can contain null or the wrong container type.
    # Reject the field explicitly before consuming it, never fall into a traceback.
    for key in ('purpose','data_reading','coverage','plan'):
        if not isinstance(detail.get(key),dict):fail(key,'须为对象，不能为null或其他类型')
    if errors:return {'errors':errors,'warnings':warnings}
    for parent,key in [('purpose','account_fit'),('data_reading','comments'),('plan','transfer'),('plan','follow_up')]:
        if not isinstance(detail[parent].get(key),dict):fail(parent+'.'+key,'须为对象')
    for key in CHECKS:
        if not isinstance(detail['coverage'].get(key),dict):fail('coverage.'+key,'须为对象')
    if not isinstance(packet.get('evidence'),list) or not isinstance(packet.get('metrics'),dict):fail('fact_packet','evidence须为数组，metrics须为对象')
    if errors:return {'errors':errors,'warnings':warnings}
    evidence={x['id']:x for x in packet.get('evidence',[]) if isinstance(x,dict) and isinstance(x.get('id'),str)}
    methods={x['id'] for x in read(LIBRARY)['methods']}
    def refs(obj,key,path,allowed,required=True):
        values=array(obj,key,path,required)
        for value in values:
            if not isinstance(value,str) or value not in allowed:fail(path+'.'+key,'未知引用：'+str(value))
        return [x for x in values if isinstance(x,str) and x in allowed]
    metrics=packet.get('metrics',{})
    if any(not isinstance(value,(int,float)) or isinstance(value,bool) or not math.isfinite(value) or value<0
           or (key.endswith('_pct') and value>100) for key,value in metrics.items()):fail('fact_packet.metrics','存在无效数字或百分数')
    if data.get('metrics',{})!=metrics:fail('metrics','须与事实包一致，不自行改指标')
    purpose=detail.get('purpose',{})
    for key in ('audience','situation','viewer_question','goal','success_evidence','overall_judgment'):text(purpose,key,'purpose')
    enum(purpose,'primary_task',('曝光','了解','成交'),'purpose')
    enum(purpose,'goal_status',('confirmed','context_assumption','unknown'),'purpose')
    refs(purpose,'evidence_refs','purpose',evidence)
    for key in ('helps','mismatch','decision'):text(purpose.get('account_fit',{}),key,'purpose.account_fit')
    readings=detail.get('data_reading',{})
    text(readings,'goal_result','data_reading')
    for i,row in enumerate(array(readings,'rows','data_reading',bool(metrics))):
        path=f'data_reading.rows[{i}]'
        if not isinstance(row,dict):fail(path,'须为对象');continue
        if not isinstance(row.get('metric_key'),str) or row['metric_key'] not in metrics:fail(path,'指标不在事实包')
        for key in ('observation','meaning','inspect_next'):text(row,key,path)
    comments=readings.get('comments',{})
    enum(comments,'status',('missing','available'),'comments')
    for key in ('sample_scope','conclusion','content_action'):text(comments,key,'comments')
    comment_refs=refs(comments,'evidence_refs','comments',evidence,comments.get('status')=='available')
    if comments.get('status')=='available' and not any(evidence[x].get('kind')=='comment' for x in comment_refs):fail('comments','评论分析没有评论原文依据')
    duration=packet.get('duration_s',0)
    timeline=array(detail,'timeline','analysis_detail')
    last=0.0
    for i,segment in enumerate(timeline):
        path=f'timeline[{i}]'
        if not isinstance(segment,dict):fail(path,'须为对象');continue
        start,end=segment.get('start_s'),segment.get('end_s')
        valid=all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in (start,end))
        if not valid or not (0<=start<end<=duration+.1):fail(path,'时间越界或起止无效')
        elif start<last-.1 or start>last+1:fail(path,'时间线重叠/遗漏，应覆盖全片功能段')
        if valid:last=end
        for key in ('content','task','handoff','reason'):text(segment,key,path)
        enum(segment,'decision',('keep','shorten','move','replace','drop'),path)
        segment_refs=refs(segment,'evidence_refs',path,evidence)
        if valid:
            def overlaps(item):
                if item.get('kind')=='machine_transcript':return item['start_s']<end and item['end_s']>start
                if item.get('kind')=='frame':return start<=item['at_s']<=end
                return False
            if not any(overlaps(evidence[ref]) for ref in segment_refs):fail(path,'证据没有对应到该时间段的口播或画面')
    if timeline and last<duration-1:fail('timeline','未覆盖片尾')
    findings=array(detail,'findings','analysis_detail')
    finding_ids=set()
    for i,finding in enumerate(findings):
        path=f'findings[{i}]'
        if not isinstance(finding,dict):fail(path,'须为对象');continue
        ident=text(finding,'id',path)
        if isinstance(ident,str) and ident in finding_ids:fail(path,'发现编号重复')
        if isinstance(ident,str):finding_ids.add(ident)
        for key in ('title','observation','explanation','action','verify'):text(finding,key,path)
        enum(finding,'dimension',('persona','script','visual'),path)
        enum(finding,'decision',('keep','improve'),path)
        refs(finding,'evidence_refs',path,evidence)
        refs(finding,'method_refs',path,methods)
        if finding.get('observation')==finding.get('explanation'):fail(path,'解释不能原样重复观察')
    coverage=detail.get('coverage',{})
    for key in CHECKS:
        item=coverage.get(key,{})
        enum(item,'status',('analyzed','missing','not_applicable'),'coverage.'+key)
        refs(item,'finding_refs','coverage.'+key,finding_ids,item.get('status')=='analyzed')
        if item.get('status')!='analyzed':text(item,'reason','coverage.'+key)
    visuals=array(detail,'visual_audit','analysis_detail')
    seen=set()
    for i,item in enumerate(visuals):
        path=f'visual_audit[{i}]'
        if not isinstance(item,dict):fail(path,'须为对象');continue
        enum(item,'aspect',ASPECTS,path)
        aspect=item.get('aspect')
        if aspect in seen:fail(path,'画面项目重复')
        seen.add(aspect)
        enum(item,'status',('observed','unknown'),path)
        for key in ('observation','impact','action'):text(item,key,path)
        visual_refs=refs(item,'evidence_refs',path,evidence,item.get('status')=='observed')
        if item.get('status')=='observed' and not any(evidence[x].get('kind') in ('frame','reviewed_clip') for x in visual_refs):fail(path,'可见画面观察缺帧/片段依据')
        if aspect=='motion' and item.get('status')=='observed' and not any(evidence[x].get('kind')=='reviewed_clip' for x in visual_refs):fail(path,'动效须有实际核读的连续片段，不能从静帧推断')
    for aspect in ASPECTS:
        if aspect not in seen:fail('visual_audit','漏项：'+aspect)
    plan=detail.get('plan',{})
    text(plan,'priority','plan')
    planned=set()
    for key,fields in [('actions',('instruction','needed_material','fallback')),('structure',('name','task','content','shot')),('replacements',('label','suggested_text','visual_instruction','reason','transition'))]:
        for i,item in enumerate(array(plan,key,'plan')):
            path=f'plan.{key}[{i}]'
            if not isinstance(item,dict):fail(path,'须为对象');continue
            for field in fields:text(item,field,path)
            linked=refs(item,'finding_refs',path,finding_ids)
            if key=='actions':
                planned.update(linked)
                enum(item,'material_status',('ready','to_prepare','not_needed'),path)
            if key=='replacements':refs(item,'source_refs',path,evidence)
    for finding in findings:
        if isinstance(finding,dict) and finding.get('decision')=='improve' and finding.get('id') not in planned:fail('plan.actions','优化发现未落实：'+str(finding.get('id')))
    for key in ('skeleton','variables','conditions','failure_risks'):text(plan.get('transfer',{}),key,'plan.transfer')
    for key in ('window','watch','interpretation','next_decision'):text(plan.get('follow_up',{}),key,'plan.follow_up')
    text(plan,'invitation','plan')
    for item in evidence.values():
        if item.get('kind')=='frame':
            try:local_file(Path(base),item.get('path'))
            except ValueError as exc:fail('frame',str(exc))
    if purpose.get('goal_status')!='confirmed':warnings.append('目标按已有背景暂定：核对正文是否清楚交代适用范围，不擅改业务目标。')
    if comments.get('status')=='missing':warnings.append('缺评论正文：不能归纳评论共识或编造观众反馈。')
    warnings.append('结构通过仍需内容复核：证据是否支持判断、解释是否有信息、改法是否落实、中文是否自然。')
    if 'report_prose' in data and 'report_view' in data:
        fail('report_prose','新正文与历史图解不能同时指定')
    if 'report_prose' in data and not errors:
        errors.extend(prose_contract().validate(data['report_prose'],detail,packet,data.get('video',{})))
    if 'report_view' in data and not errors:
        errors.extend(visual_contract().validate(data['report_view'],detail,packet,data.get('video',{})))
    return {'errors':errors,'warnings':warnings}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare')
    prep.add_argument('--media-receipt',type=Path,required=True)
    prep.add_argument('--context',type=Path,required=True)
    prep.add_argument('--output',type=Path,required=True)
    check=sub.add_parser('validate')
    check.add_argument('report',type=Path)
    args=parser.parse_args()
    try:
        if args.command=='prepare':print(str(prepare(args.media_receipt,args.context,args.output)));return 0
        result=validate(read(args.report),args.report.resolve().parent)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 2 if result['errors'] else 0
    except (OSError,ValueError,TypeError,KeyError) as exc:
        print('处理失败：'+str(exc),file=sys.stderr)
        return 2


if __name__=='__main__':sys.exit(main())
