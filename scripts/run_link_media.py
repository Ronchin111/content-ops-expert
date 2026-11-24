"""Single selected public link -> bounded download -> local ASR/frames.

Input JSON comes from stdin. Default is an offline plan. No search, installs,
cookie extraction, account switching or cloud audio upload. Transient Douyin
page requests may retry once; platform restrictions always stop.
"""
import argparse
import base64
from datetime import datetime, timezone
from html import unescape
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import os
import shutil
import sys
import time
import wave
import socket
import ssl
import urllib.error
from http.client import IncompleteRead, RemoteDisconnected
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

HERE=Path(__file__).resolve().parent

def sibling(name):
    # Embedded Windows Python does not automatically add the script directory.
    spec=importlib.util.spec_from_file_location('content_ops_'+name,HERE/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

d=sibling('download_link_sample')
processing_module=sibling('process_video_sample')
xhs_browser=sibling('xhs_browser_ready')
process=processing_module.process
write_json=processing_module.write_json
PARSER='https://sph.miuistore.com'
CHANNEL_MEDIA_HOSTS=('qq.com','qpic.cn','myqcloud.com','qcloudcdn.com','tencentcloudcdn.com')
DEFAULT_CHROME=Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
DEFAULT_NODE=Path(os.environ.get('CONTENT_OPS_NODE') or shutil.which('node') or 'node.exe')
OPENCLI_DAEMON=(HERE.parent/'vendor'/'opencli-2.0.3'/'node_modules'/
                '@jackwener'/'opencli'/'dist'/'src'/'daemon.js')


def identify(text):
    # A pasted Markdown-escaped share link is still the same selected URL.
    text=re.sub(r'\\([:/.])',r'\1',str(text))
    urls=re.findall(r'https?://[^\s<>"\u300a\u300b]+',str(text))
    if len(urls)!=1:raise d.Limit('ONE_SELECTED_SHARE_URL_REQUIRED')
    url=d.https_url(urls[0].rstrip('。，；！）)'))
    if d.trusted(url,d.PAGE_HOSTS['douyin']):
        platform='douyin';work=d.work_id(url,platform)
        if not work and not re.fullmatch(r'/[A-Za-z0-9_-]+/?',urlsplit(url).path):raise d.Limit('DOUYIN_WORK_LINK_REQUIRED')
        if not work and urlsplit(url).hostname!='v.douyin.com':raise d.Limit('DOUYIN_WORK_LINK_REQUIRED')
    elif d.trusted(url,('www.xiaohongshu.com',)):
        platform='xiaohongshu';work=d.work_id(url,platform)
        if not work:raise d.Limit('FULL_XHS_DETAIL_URL_REQUIRED')
    elif d.trusted(url,('xhslink.com','xhslink.cn')):
        if urlsplit(url).hostname not in ('xhslink.com','www.xhslink.com','xhslink.cn','www.xhslink.cn') or urlsplit(url).path=='/':raise d.Limit('XHS_WORK_SHARE_LINK_REQUIRED')
        platform='xiaohongshu';work=None
    elif d.trusted(url,('weixin.qq.com','channels.weixin.qq.com')):
        platform='wechat_channels';p=urlsplit(url)
        if p.hostname=='weixin.qq.com' and re.fullmatch(r'/sph/[A-Za-z0-9_-]+/?',p.path):work=p.path.rstrip('/').split('/')[-1]
        elif p.hostname=='channels.weixin.qq.com' and p.path=='/finder-preview/pages/sph':work=parse_qs(p.query).get('id',[''])[0]
        else:raise d.Limit('CHANNELS_VIDEO_SHARE_LINK_REQUIRED')
        if not work or len(work)>1024:raise d.Limit('CHANNELS_SHARE_ID_INVALID')
    else:raise d.Limit('PLATFORM_OR_LINK_NOT_SUPPORTED')
    return platform,url,work


def canonical(platform,work):
    if not work:return None
    if platform=='douyin':return 'https://www.douyin.com/video/'+work
    if platform=='xiaohongshu':return 'https://www.xiaohongshu.com/explore/'+work
    # Only public short share IDs are displayed; opaque finder query values stay private.
    if re.fullmatch(r'[A-Za-z0-9_-]{1,100}',work):return 'https://weixin.qq.com/sph/'+work
    return None


def public_identity(title,author,payload):
    title=d.public_text(title);author=d.public_text(author)
    if not title:raise d.Limit('SELECTED_TITLE_MISSING')
    expected=payload.get('expected_title')
    compact=lambda s:re.sub(r'\s+','',str(s))
    if expected and not compact(title).startswith(compact(expected)):raise d.Limit('EXPECTED_TITLE_MISMATCH')
    if payload.get('expected_author') and compact(author)!=compact(payload['expected_author']):raise d.Limit('EXPECTED_AUTHOR_MISSING_OR_MISMATCH')
    return {'title':title,'author':author or None,'author_check':'observed_on_selected_page' if author else 'pending'}


def json_get(url,hosts):
    with d.open_url(url,hosts,d.PC_UA) as response:
        raw=response.read(4*1024*1024+1)
    if len(raw)>4*1024*1024:raise d.Limit('PARSER_RESPONSE_SIZE_LIMIT')
    data=json.loads(raw.decode('utf-8'))
    if not isinstance(data,dict):raise d.Limit('PARSER_RESPONSE_NOT_OBJECT')
    return data


def check_parser(data):
    if data.get('error') in (0,'0'):return
    message=str(data.get('message') or '')
    if re.search(r'频率|频控|频繁|rate.limit',message,re.I):raise d.Limit('PARSER_RATE_LIMITED_STOPPED')
    if re.search(r'登录|验证码|验证|access|captcha',message,re.I):raise d.Limit('PARSER_ACCESS_OR_LOGIN_REQUIRED_STOPPED')
    if re.search(r'付费|支付|积分|充值',message):raise d.Limit('PARSER_PAYMENT_OR_QUOTA_REQUIRED_STOPPED')
    raise d.Limit('PARSER_REPORTED_FAILURE_STOPPED')


def network_failure(exc):
    reason=exc.reason if isinstance(exc,urllib.error.URLError) else exc
    if isinstance(reason,ssl.SSLCertVerificationError):
        code,retry='TLS_CERTIFICATE_FAILED',False
    elif isinstance(reason,(TimeoutError,socket.timeout)):
        code,retry='TIMEOUT',True
    elif isinstance(reason,(ConnectionError,RemoteDisconnected,IncompleteRead,ssl.SSLEOFError)):
        code,retry='CONNECTION_INTERRUPTED',True
    elif isinstance(reason,socket.gaierror):
        code,retry='DNS_FAILED',reason.errno==socket.EAI_AGAIN
    else:
        code,retry='NETWORK_FAILED',False
    return code,retry,type(reason).__name__


def read_douyin_page(url,hosts,ua,stage,limit,referer=None,attempts=None):
    """Retry only a failed read-only request, never a complete media job."""
    for attempt in range(2):
        try:
            with d.open_url(url,hosts,ua,referer) as response:
                final=response.geturl()
                raw=response.read(limit+1) if limit else b''
            if limit and len(raw)>limit:raise d.Limit(stage+'_SIZE_LIMIT')
            if attempts is not None:
                attempts.append({'stage':stage,'attempt':attempt+1,'status':'received'})
            return final,raw
        except d.Limit as exc:
            if attempts is not None:
                attempts.append({'stage':stage,'attempt':attempt+1,'status':str(exc)})
            raise
        except (urllib.error.URLError,OSError,RemoteDisconnected,IncompleteRead) as exc:
            code,retry,cause=network_failure(exc)
            if attempts is not None:
                attempts.append({'stage':stage,'attempt':attempt+1,'status':code,
                                 'cause':cause})
            if not retry or attempt:
                raise d.Limit(stage+'_'+code) from None
            time.sleep(1)


def resolve_douyin(url,expected,payload,attempts=None):
    # Resolve an official short link first so the parser receives a stable,
    # work-ID-bound URL. Redirects are restricted to platform domains.
    if urlsplit(url).hostname == 'v.douyin.com':
        final_url,_=read_douyin_page(url,d.PAGE_HOSTS['douyin'],d.MOBILE_UA,
                                    'DOUYIN_SHORT_REDIRECT',0,attempts=attempts)
        redirected_id=d.work_id(final_url,'douyin')
        if not redirected_id:raise d.Limit('DOUYIN_SHORT_LINK_ID_MISSING')
        if expected and redirected_id!=expected:raise d.Limit('RETURNED_WORK_ID_MISMATCH')
        expected=redirected_id
        url=canonical('douyin',redirected_id)
    _,raw=read_douyin_page(PARSER+'/sph/public/dy-check?'+urlencode({'data':url}),
                          ('sph.miuistore.com',),d.PC_UA,'DOUYIN_PARSER_CHECK',
                          4*1024*1024,attempts=attempts)
    try:data=json.loads(raw.decode('utf-8'))
    except (ValueError,UnicodeError):raise d.Limit('DOUYIN_PARSER_INVALID_JSON') from None
    if not isinstance(data,dict):raise d.Limit('PARSER_RESPONSE_NOT_OBJECT')
    check_parser(data)
    landing=urljoin(PARSER+'/',data.get('url',''))
    if not d.trusted(landing,('sph.miuistore.com',)) or urlsplit(landing).path!='/sph/public/dy-d':raise d.Limit('UNEXPECTED_PARSER_ROUTING_URL')
    _,raw=read_douyin_page(landing,('sph.miuistore.com',),d.PC_UA,
                          'DOUYIN_PARSER_LANDING',4*1024*1024,attempts=attempts)
    page=raw.decode('utf-8',errors='replace')
    match=re.search(r'\$\.get\("(https://apikr\.convry\.com/sph/public/dy-r\?[^\"]+)"',page)
    if not match:raise d.Limit('PARSER_RESULT_ENDPOINT_MISSING')
    _,raw=read_douyin_page(unescape(match[1]),('apikr.convry.com',),d.PC_UA,
                          'DOUYIN_PARSER_RESULT',6*1024*1024,PARSER+'/',attempts)
    html=raw.decode('utf-8',errors='replace')
    visible=re.sub(r'<!--[\s\S]*?-->','',html)
    match=re.search(r'视频ID\s*[：:]?\s*</(?:td|div)>\s*<(?:td|div)\b[^>]*>\s*(\d+)',visible)
    title_match=re.search(r'window\.document\.title\s*=\s*("(?:\\.|[^"\\])*")',html)
    if not match or not title_match:raise d.Limit('PARSER_SELECTED_ID_OR_TITLE_MISSING')
    work=match[1]
    if expected and work!=expected:raise d.Limit('RETURNED_WORK_ID_MISMATCH')
    identity=public_identity(json.loads(title_match[1],strict=False),None,payload)
    identity.update(work_id=work,source='miuistore_to_apikr_convry',work_check='id_matched' if expected else 'input_bound_service_result_pending_visual_review')
    links=[unescape(u) for u in re.findall(r'<a\b[^>]*href=[\"\x27]([^\"\x27]+)',visible,re.I)]
    media=next((d.https_url(u) for u in links if d.trusted(d.https_url(u),d.MEDIA_HOSTS['douyin'])),None)
    if not media:raise d.Limit('NO_ALLOWED_DOUYIN_MEDIA')
    return identity,media


def resolve_xhs(url,expected,payload,args):
    if not args.context_id:raise d.Limit('AUTHORIZED_CHROME_CONTEXT_REQUIRED')
    if not args.node_path.is_file():raise d.Limit('EXISTING_NODE_NOT_FOUND_NO_INSTALL')
    try:
        xhs_browser.prepare(args.context_id,args.node_path,OPENCLI_DAEMON,
                            args.chrome_path,args.launch_authorized_default_chrome)
    except xhs_browser.BrowserUnavailable as exc:
        raise d.Limit(str(exc)) from exc
    run=subprocess.run([str(args.node_path),str(HERE/'read_open_note.mjs')],
        input=json.dumps({'url':url,'contextId':args.context_id}).encode(),capture_output=True,timeout=45)
    data=json.loads(run.stdout.decode('utf-8'))
    if data.get('code')!='SELECTED_NOTE_READ':
        allowed={'ACCESS_RESTRICTED_STOPPED','AUTH_REQUIRED_STOPPED','CONTENT_UNAVAILABLE_STOPPED','RETURNED_WORK_ID_MISMATCH','UNEXPECTED_LINK_DESTINATION_STOPPED','SELECTED_NOTE_LOAD_TIMEOUT_STOPPED'}
        if data.get('bridge_error_code') in ('daemon-not-running','extension-not-connected','profile-disconnected'):
            raise d.Limit('XHS_BRIDGE_DROPPED')
        raise d.Limit(data.get('code') if data.get('code') in allowed else 'BROWSER_READ_FAILED_STOPPED')
    actual=data.get('note_id')
    if not re.fullmatch(r'[a-f0-9]{24}',str(actual)) or (expected and actual!=expected):raise d.Limit('RETURNED_WORK_ID_MISMATCH')
    if data.get('note_type')!='video':raise d.Limit('SELECTED_NOTE_NOT_VIDEO')
    if not data.get('title') or not data.get('author'):raise d.Limit('SELECTED_TITLE_OR_AUTHOR_MISSING')
    identity=public_identity(data['title'],data['author'],payload)
    identity.update(work_id=actual,source='authorized_chrome_agent_opened_link',work_check='selected_page_state_id_matched' if expected else 'input_shortlink_redirect_and_page_state_id_matched',navigation='agent_opened_selected_link',user_manual_open_required=False)
    media=next((u for u in data.get('media',[]) if d.trusted(u,d.MEDIA_HOSTS['xiaohongshu'])),None)
    if not media:raise d.Limit('NO_ALLOWED_XHS_MEDIA')
    return identity,media


def resolve_channels(url,work,args):
    if not args.chrome_path.is_file():raise d.Limit('EXISTING_CHROME_NOT_FOUND_NO_INSTALL')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=str(args.chrome_path),headless=True)
        try:
            context=browser.new_context(accept_downloads=False)
            phase={'parsing':False}
            def route(r):
                host=(urlsplit(r.request.url).hostname or '').lower()
                if re.search(r'googlesyndication|googletagmanager|google-analytics|doubleclick|profitablecpmratenetwork',host):r.abort()
                elif phase['parsing'] and r.request.resource_type in ('media','fetch','xhr') and not any(host==h or host.endswith('.'+h) for h in ('miuistore.com','convry.com')):r.abort()
                else:r.continue_()
            context.route('**/*',route)
            page=context.new_page()
            page.add_init_script("Object.defineProperty(window,'showSaveFilePicker',{value:undefined})")
            try:page.goto(PARSER+'/',wait_until='commit',timeout=45000)
            except Exception as exc:
                if not any(s in str(exc) for s in ('ERR_ABORTED','interrupted by another navigation')):raise
            field=page.locator('input.search-input')
            field.wait_for(state='visible',timeout=45000)
            page.wait_for_function("typeof window.jQuery==='function' && typeof window.AlgoSign==='function'",timeout=45000)
            field.fill(url);phase['parsing']=True
            with page.expect_response(lambda r:urlsplit(r.url).hostname=='sph.miuistore.com' and urlsplit(r.url).path=='/sph/public/quick',timeout=60000) as event:
                page.get_by_role('button',name='下载',exact=True).click()
            response=event.value
            if response.status in (401,403,429):raise d.Limit('PARSER_ACCESS_RESTRICTED_STOPPED')
            if parse_qs(urlsplit(response.url).query).get('id',[''])[0]!=work+'##1':raise d.Limit('REQUESTED_SHARE_ID_MISMATCH')
            data=response.json();check_parser(data)
            return data
        finally:browser.close()


def download_channels(data,output):
    media=data.get('media') or {}
    if media.get('is_live') in (True,1,'1'):raise d.Limit('LIVE_NOT_SUPPORTED')
    url=data.get('url')
    if not d.trusted(url,CHANNEL_MEDIA_HOSTS):raise d.Limit('CHANNEL_MEDIA_HOST_NOT_ALLOWED')
    raw_key=data.get('_data') or ''
    if len(raw_key)>6*1024*1024:raise d.Limit('HEADER_DATA_SIZE_LIMIT')
    key=base64.b64decode(raw_key,validate=True)
    if len(key)>4*1024*1024:raise d.Limit('HEADER_DATA_SIZE_LIMIT')
    alternate=None
    if data.get('enurl'):
        with d.open_url(data['enurl'],CHANNEL_MEDIA_HOSTS,d.PC_UA) as response:alternate=response.read(len(key))
        if len(alternate)!=len(key):raise d.Limit('ALTERNATE_HEADER_TRUNCATED')
    part=output/'video.mp4.part';total=0;digest=hashlib.sha256();started=time.monotonic()
    with d.open_url(url,CHANNEL_MEDIA_HOSTS,d.PC_UA,PARSER+'/') as response,part.open('xb') as target:
        if any(t in response.headers.get('Content-Type','').lower() for t in ('text/html','application/json')):raise d.Limit('MEDIA_RESPONSE_NOT_VIDEO')
        while True:
            block=response.read(1024*1024)
            if not block:break
            if total+len(block)>d.MEDIA_LIMIT or time.monotonic()-started>180:raise d.Limit('MEDIA_SIZE_OR_TIME_LIMIT')
            block=bytearray(block)
            for i in range(min(len(block),max(0,len(key)-total))):block[i]=(alternate[total+i] if alternate is not None else block[i])^key[total+i]
            target.write(block);digest.update(block);total+=len(block)
    import av
    with av.open(str(part)) as container:
        if not container.streams.video or next(container.decode(video=0),None) is None:raise d.Limit('MEDIA_NOT_DECODABLE')
    final=output/'video.mp4'
    if final.exists():raise d.Limit('OUTPUT_ALREADY_EXISTS')
    part.rename(final)
    return {'file':str(final),'bytes':total,'sha256':digest.hexdigest(),'header_restore_bytes':len(key)}


def reuse_artifacts(download_receipt,processing_receipt):
    """Read only an explicitly supplied pair; never scans previous runs or downloads again."""
    download=json.loads(download_receipt.read_text(encoding='utf-8'))
    processing=json.loads(processing_receipt.read_text(encoding='utf-8'))
    platform=download.get('platform')
    if platform not in ('douyin','xiaohongshu','wechat_channels'):raise d.Limit('REUSE_PLATFORM_UNKNOWN')
    media=download.get('media') or {}
    video=Path(media.get('file','')).resolve(strict=True)
    if not video.is_file():raise d.Limit('REUSE_MEDIA_MISSING')
    digest=hashlib.sha256()
    with video.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
    if digest.hexdigest()!=media.get('sha256') or digest.hexdigest()!=processing.get('source_sha256') or str(video)!=str(Path(processing.get('source_file','')).resolve()):raise d.Limit('REUSE_MEDIA_HASH_OR_SOURCE_MISMATCH')
    if video.stat().st_size!=media.get('bytes'):raise d.Limit('REUSE_MEDIA_SIZE_MISMATCH')
    if processing.get('decode')!='beginning_middle_end_video_decodable':raise d.Limit('REUSE_DECODE_NOT_PASSED')
    if processing.get('status')!='artifacts_ready_pending_identity_and_audio_review' or processing.get('transcription')!='generated_pending_listening_review':raise d.Limit('REUSE_PROCESSING_NOT_READY')
    from PIL import Image
    frames=processing.get('frames',[])
    if len(frames)!=5:raise d.Limit('REUSE_FIVE_FRAMES_REQUIRED')
    for frame in frames:
        if not 0<=frame['actual_s']<=processing['duration_s']:raise d.Limit('REUSE_FRAME_TIME_INVALID')
        with Image.open(frame['file']) as im:im.verify()
    processing_dir=processing_receipt.resolve().parent
    transcript=json.loads((processing_dir/'transcript.json').read_text(encoding='utf-8'))
    if not transcript.get('segments'):raise d.Limit('REUSE_TRANSCRIPT_EMPTY')
    for filename in ('口播转写.txt','transcript.srt'):
        if not (processing_dir/filename).is_file() or not (processing_dir/filename).stat().st_size:raise d.Limit('REUSE_TRANSCRIPT_FILE_MISSING')
    last=0
    for segment in transcript.get('segments',[]):
        if not last<=segment['start_s']<segment['end_s']<=processing['duration_s']+.1:raise d.Limit('REUSE_TRANSCRIPT_TIME_INVALID')
        last=segment['start_s']
    if len(processing.get('spotchecks',[]))!=3:raise d.Limit('REUSE_THREE_AUDIO_SPOTCHECKS_REQUIRED')
    for spot in processing.get('spotchecks',[]):
        with wave.open(spot['file'],'rb') as wav:
            if wav.getnframes()==0:raise d.Limit('REUSE_AUDIO_EMPTY')
    work=download.get('work_id') or download.get('input_work_id')
    if platform=='wechat_channels' and not work:
        _,_,work=identify(download.get('input_share_url',''))
    identity={'work_id':work,'title':d.public_text(download.get('title')),'author':d.public_text(download.get('author')) or None,
              'source':'explicit_existing_download_receipt','identity_check':'preserve_source_receipt_pending_checks'}
    # Preserve necessary status and artifact references, not arbitrary fields from a receipt.
    safe_processing={k:processing[k] for k in ('decode','transcription','status','duration_s','width','height','source_sha256')}
    safe_processing['frames']=[{'file':str(Path(f['file']).resolve()),'actual_s':f['actual_s']} for f in frames]
    safe_processing['spotchecks']=[{'file':str(Path(s['file']).resolve()),'start_s':s['start_s'],'duration_s':s['duration_s']} for s in processing['spotchecks']]
    safe_processing['segment_count']=len(transcript['segments'])
    safe_media={'file':str(video),'bytes':video.stat().st_size,'sha256':digest.hexdigest()}
    return platform,identity,safe_media,safe_processing,processing_dir


def finish_result(output,result):
    write_json(output/'media-result.json',result)
    identity=result.get('identity',{})
    lines=['# 作品素材处理结果','',f"状态：{result['status']}",f"平台：{result.get('platform','未确定')}",
           f"作品：{identity.get('title') or '未取得'}",f"作者：{identity.get('author') or '未核实'}"]
    if result.get('reason'):lines+=['',f"停止原因：{result['reason']}"]
    if result.get('next_action'):lines+=[f"下一步：{result['next_action']}"]
    if result.get('media'):lines+=['',f"视频：{result['media']['file']}"]
    if result.get('processing_directory'):lines+=[f"文字、帧图及复听材料：{result['processing_directory']}"]
    lines+=['','机器转写不是原始创作稿，准确度待复听；身份、指标及业务验收以清单中的实际状态为准。']
    with (output/'feedback.md').open('x',encoding='utf-8') as f:f.write('\n'.join(lines)+'\n')


def run(args,payload):
    if args.reuse_download or args.reuse_processing:
        if not (args.reuse_download and args.reuse_processing):raise d.Limit('BOTH_EXPLICIT_REUSE_RECEIPTS_REQUIRED')
        if args.execute:raise d.Limit('REUSE_AND_NETWORK_EXECUTION_ARE_SEPARATE')
        platform,identity,media,processing,processing_dir=reuse_artifacts(args.reuse_download,args.reuse_processing)
        result={'status':'existing_artifacts_verified_pending_review','mode':'reuse_artifacts','platform':platform,
                'identity':identity,'media':media,'processing':processing,'processing_directory':str(processing_dir),
                'network_requests':0,'media_download_repeated':False,'transcription_repeated':False,
                'source_receipts':[str(args.reuse_download.resolve()),str(args.reuse_processing.resolve())]}
    else:
        platform,url,work=identify(payload.get('share_text',''))
        result={'status':'planned','mode':'link','platform':platform,'input_sha256':hashlib.sha256(url.encode()).hexdigest(),
                'public_work_url':canonical(platform,work),'work_role':payload.get('work_role','unspecified'),
                'cookies_extracted':False,'cloud_audio_upload':False,'automatic_retries':0,
                'network_attempts':[],
                'identity':{'work_id':work},'metric_trust':'not_collected',
                'required_condition':'authorized_chrome_bridge_session' if platform=='xiaohongshu' else 'public_parser_service_authorization'}
        if args.execute:
            if platform!='xiaohongshu' and not args.allow_parser_service:raise d.Limit('PUBLIC_PARSER_SERVICE_AUTHORIZATION_REQUIRED')
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    result['started_at']=datetime.now(timezone.utc).isoformat()
    try:
        if result['mode']=='link' and args.execute:
            result['status']='partial'
            result['active_stage']='link_resolution'
            if platform=='douyin':
                identity,media_url=resolve_douyin(url,work,payload,result['network_attempts'])
                result['identity']=identity
                result['public_work_url']=canonical(platform,identity['work_id'])
                result['active_stage']='media_download'
                result['media']=d.download(media_url,platform,output)
                result['network_attempts'].append({'stage':'MEDIA_DOWNLOAD','attempt':1,'status':'received'})
            elif platform=='wechat_channels':
                data=resolve_channels(url,work,args)
                identity=public_identity((data.get('media') or {}).get('title'),(data.get('media') or {}).get('nickname'),payload)
                public_id=work if urlsplit(url).hostname=='weixin.qq.com' else None
                identity.update(work_id=public_id,source='miuistore_normal_page',work_check='exact_share_id_bound_to_request')
                result['identity']=identity
                result['active_stage']='media_download'
                result['media']=download_channels(data,output)
            else:
                identity,media_url=resolve_xhs(url,work,payload,args)
                result['identity']=identity
                result['active_stage']='media_download'
                result['media']=d.download(media_url,platform,output)
            processing_dir=output/'processing'
            result['active_stage']='local_processing'
            result['processing']=process(Path(result['media']['file']),processing_dir,args.model_dir,False,platform+'_'+str(result['identity']['work_id']))
            result['processing_directory']=str(processing_dir)
            result['status']='artifacts_ready_pending_review' if result['processing']['status']=='artifacts_ready_pending_identity_and_audio_review' else 'partial'
            result.pop('active_stage',None)
        result['review']={'spoken_text':'pending_listening','identity':'pending_user_content_check','business_acceptance':'not_complete'}
    except d.Limit as exc:
        result.update(status='stopped',reason=str(exc))
        if getattr(exc,'destination_host',None):
            result['blocked_redirect_host']=exc.destination_host
        if result.get('active_stage')=='media_download':
            result['network_attempts'].append({'stage':'MEDIA_DOWNLOAD','attempt':1,'status':str(exc)})
    except (urllib.error.URLError,OSError,RemoteDisconnected,IncompleteRead) as exc:
        code,_,cause=network_failure(exc)
        stage={'link_resolution':'LINK_RESOLUTION','media_download':'MEDIA_DOWNLOAD',
               'local_processing':'LOCAL_PROCESSING'}.get(result.get('active_stage'),'LOCAL_OPERATION')
        result.update(status='partial' if result.get('media') else 'stopped',
                      reason=stage+'_'+code)
        result['network_attempts'].append({'stage':stage,'attempt':1,'status':code,'cause':cause})
    except Exception as exc:result.update(status='partial' if result.get('media') else 'stopped',reason='LOCAL_OR_NETWORK_'+type(exc).__name__)
    result['automatic_retries']=sum(item.get('attempt',1)>1 for item in result.get('network_attempts',[]))
    if result.get('reason') in xhs_browser.RECOVERY:
        result['next_action']=xhs_browser.RECOVERY[result['reason']]
    finish_result(output,result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--allow-parser-service',action='store_true')
    parser.add_argument('--context-id')
    parser.add_argument('--launch-authorized-default-chrome',action='store_true',
                        help='Launch the existing Chrome Default only if it is the authorized context.')
    parser.add_argument('--chrome-path',type=Path,default=DEFAULT_CHROME)
    parser.add_argument('--node-path',type=Path,default=DEFAULT_NODE)
    parser.add_argument('--model-dir',type=Path)
    parser.add_argument('--reuse-download',type=Path)
    parser.add_argument('--reuse-processing',type=Path)
    args=parser.parse_args()
    try:
        payload={} if args.reuse_download or args.reuse_processing else json.loads(sys.stdin.buffer.read(65536).decode('utf-8-sig'))
        result=run(args,payload)
    except d.Limit as exc:result={'status':'not_started','reason':str(exc)}
    except Exception as exc:result={'status':'not_started','reason':type(exc).__name__}
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status'] in ('planned','existing_artifacts_verified_pending_review','artifacts_ready_pending_review') else 2


if __name__=='__main__':sys.exit(main())
