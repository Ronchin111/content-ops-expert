"""Fetch one selected note's media using its full URL in memory; no cookies."""
import json
from pathlib import Path
import subprocess
import os
import shutil
import urllib.request
from urllib.parse import urlsplit

NODE=Path(os.environ.get('CONTENT_OPS_NODE') or shutil.which('node') or 'node.exe')

def trusted(url):
    try:
        p=urlsplit(url)
        return p.scheme=='https' and (p.hostname or '').endswith('.xhscdn.com') and not p.username and not p.password and p.port in (None,443)
    except (ValueError,TypeError):
        return False

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not trusted(newurl):raise ValueError('untrusted media redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(profile,target,output):
    """Returns public receipt only. Does not log, save or return signed URLs."""
    note_id=urlsplit(target).path.rstrip('/').rsplit('/',1)[-1]
    request={'profile':profile,'url':target,'noteId':note_id}
    try:
        run=subprocess.run([str(NODE),str(Path(__file__).with_name('read_selected_media.mjs'))],
                           input=json.dumps(request).encode(),capture_output=True,timeout=60)
        data=json.loads(run.stdout.decode('utf-8'))
    except (ValueError,UnicodeError,OSError,subprocess.TimeoutExpired):
        return {'ok':False,'code':'MEDIA_PROBE_OUTCOME_UNKNOWN'}
    if not isinstance(data,dict) or not data.get('ok'):
        code=data.get('code') if isinstance(data,dict) else None
        allowed={'SECURITY_BLOCK','AUTH_REQUIRED','NOTE_ID_MISMATCH','SIGNED_SELECTED_URL_REQUIRED','MEDIA_PAGE_READ_FAILED'}
        return {'ok':False,'code':code if code in allowed else 'MEDIA_PROBE_FAILED'}
    if data.get('noteId')!=note_id:return {'ok':False,'code':'NOTE_ID_MISMATCH'}
    media=data.get('media',[])
    chosen=next((x for x in media if x.get('type')=='video' and trusted(x.get('url'))),None)
    if chosen is None:return {'ok':False,'code':'NO_SELECTED_VIDEO'}
    output.mkdir(exist_ok=False)
    dest=output/'selected.mp4.part'
    try:
        req=urllib.request.Request(chosen['url'],headers={'Referer':'https://www.xiaohongshu.com/','User-Agent':'Mozilla/5.0'})
        with urllib.request.build_opener(SafeRedirect()).open(req,timeout=30) as res,dest.open('xb') as stream:
            total=0
            while True:
                block=res.read(1024*1024)
                if not block:break
                total+=len(block)
                if total>150*1024*1024:raise ValueError('sample size exceeded')
                stream.write(block)
        import av
        with av.open(str(dest)) as container:
            if not container.streams.video or next(container.decode(video=0),None) is None:raise ValueError('video not decodable')
        final=output/'selected.mp4'
        if final.exists():raise ValueError('target exists')
        dest.rename(final)
        return {'ok':True,'code':'VIDEO_DOWNLOADED_NO_COOKIES','file':str(final),'bytes':total}
    except Exception:
        return {'ok':False,'code':'MEDIA_FETCH_OR_DECODE_FAILED','partial_file_retained':dest.exists()}
