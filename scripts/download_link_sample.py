"""One public video from stdin JSON; no browser, cookies, search or automatic retries.

Candidate for feasibility checks only, not a production Skill route.
XHS field paths were checked against XHS-Downloader tag 2.8 (GPL-3.0);
this independently written adapter does not import or execute that application.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

PAGE_HOSTS = {
    'xiaohongshu': ('xiaohongshu.com', 'xhslink.com', 'xhslink.cn'),
    'douyin': ('douyin.com', 'iesdouyin.com'),
}
MEDIA_HOSTS = {
    'xiaohongshu': ('xhscdn.com',),
    'douyin': ('douyinvod.com', 'douyin.com', 'snssdk.com', 'bytecdn.cn',
               'bytecdn.com', 'bytedance.com', 'ibytedtos.com'),
}
MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
             'AppleWebKit/605.1.15 Version/16.0 Mobile/15E148 Safari/604.1')
PC_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
PAGE_LIMIT = 12 * 1024 * 1024
MEDIA_LIMIT = 300 * 1024 * 1024


class Limit(Exception):
    """Only stable, non-sensitive reason codes may leave the process."""


def trusted(url, hosts):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        return (p.scheme == 'https' and not p.username and not p.password
                and p.port in (None, 443)
                and any(host == h or host.endswith('.' + h) for h in hosts))
    except (ValueError, TypeError):
        return False


def https_url(url):
    if not isinstance(url, str):
        raise Limit('INVALID_URL')
    p = urlsplit(url)
    return urlunsplit(('https', p.netloc, p.path, p.query, p.fragment)) if p.scheme == 'http' else url


def selected_url(text, platform):
    urls = re.findall(r'https?://[^\s<>"\u300a\u300b]+', text)
    urls = [https_url(u.rstrip('。，；！）)')) for u in urls]
    if len(urls) != 1 or not trusted(urls[0], PAGE_HOSTS[platform]):
        raise Limit('ONE_SUPPORTED_PLATFORM_URL_REQUIRED')
    return urls[0]


class Redirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts
        self.count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > 6:
            raise Limit('REDIRECT_LIMIT')
        newurl = https_url(urljoin(req.full_url, newurl))
        if not trusted(newurl, self.hosts):
            error = Limit('REDIRECT_OUTSIDE_PLATFORM_ALLOWLIST')
            # Diagnose rejected CDN redirects without retaining signed URLs.
            error.destination_host = urlsplit(newurl).hostname
            raise error
        if blocked_url(newurl):
            raise Limit('ACCESS_OR_LOGIN_REQUIRED')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def blocked_url(url):
    return bool(re.search(r'(?:captcha|securityblock|/login|/passport|/verify)', urlsplit(url).path, re.I))


def open_url(url, hosts, ua, referer=None):
    if not trusted(url, hosts):
        raise Limit('DESTINATION_OUTSIDE_ALLOWLIST')
    if blocked_url(url):
        raise Limit('ACCESS_OR_LOGIN_REQUIRED')
    headers = {'User-Agent': ua, 'Accept': '*/*'}
    if referer:
        headers['Referer'] = referer
    # No CookieProcessor: response Set-Cookie values are never replayed or persisted.
    opener = urllib.request.build_opener(Redirect(hosts))
    try:
        return opener.open(urllib.request.Request(url, headers=headers), timeout=20)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise Limit('ACCESS_RESTRICTED_HTTP_' + str(code) if code in (401, 403, 429)
                    else 'HTTP_' + str(code)) from None


def page(url, platform, ua):
    with open_url(url, PAGE_HOSTS[platform], ua) as response:
        final_url = response.geturl()
        body = response.read(PAGE_LIMIT + 1)
        if len(body) > PAGE_LIMIT:
            raise Limit('PAGE_SIZE_LIMIT')
        html = body.decode('utf-8', errors='replace')
    # Detect explicit challenge pages, not ordinary login buttons on public pages.
    if (blocked_url(final_url) or re.search(r'<title>[^<]*(?:验证码|安全验证|访问受限|登录)', html)
            or re.search(r'(?:请完成验证后继续访问|访问过于频繁|当前访问存在异常)', html)):
        raise Limit('ACCESS_OR_LOGIN_REQUIRED')
    return final_url, html


def work_id(url, platform):
    p = urlsplit(url)
    if platform == 'xiaohongshu':
        match = re.fullmatch(r'/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?', p.path)
        # The last component of a user/profile/<author>/<note> share is a note.
        match = match or re.fullmatch(r'/user/profile/[0-9a-fA-F]{24}/([0-9a-fA-F]{24})/?', p.path)
        return match.group(1).lower() if match else None
    match = re.fullmatch(r'/(?:share/)?video/(\d+)/?', p.path)
    if match:
        return match.group(1)
    ids = parse_qs(p.query).get('modal_id') or parse_qs(p.query).get('aweme_id')
    return ids[0] if ids and re.fullmatch(r'\d+', ids[0]) else None


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = True
            self.scripts.append('')

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.scripts[-1] += data


def js_state(html, variable):
    parser = Scripts()
    parser.feed(html)
    for script in reversed(parser.scripts):
        match = re.search(r'(?:window\.)?' + re.escape(variable) + r'\s*=\s*', script)
        if not match:
            continue
        raw = script[match.end():].lstrip()
        # Replace JS undefined only outside quoted JSON strings; never eval JS.
        raw = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b|new Map\(\[\]\)',
                     lambda m: m[0] if m[0].startswith('"') else ('[]' if m[0].startswith('new ') else 'null'), raw)
        try:
            data, _ = json.JSONDecoder().raw_decode(raw)
            if isinstance(data, dict):
                return data
        except (ValueError, RecursionError):
            raise Limit('EMBEDDED_STATE_NOT_JSON') from None
    raise Limit('EMBEDDED_STATE_MISSING_OR_LOGIN_REQUIRED')


def xhs_note(state, expected):
    phone = state.get('noteData', {}).get('data', {}).get('noteData', {})
    details = state.get('note', {}).get('noteDetailMap', {})
    candidates = [phone] + [v.get('note', {}) for v in details.values() if isinstance(v, dict)]
    chosen = [n for n in candidates if isinstance(n, dict) and str(n.get('noteId', '')).lower() == expected]
    if not chosen:
        raise Limit('SELECTED_WORK_NOT_IN_PAGE')
    return chosen[0]


def xhs_media(note):
    video = note.get('video', {})
    key = video.get('consumer', {}).get('originVideoKey')
    if key:
        return 'https://sns-video-bd.xhscdn.com/' + key
    streams = video.get('media', {}).get('stream', {})
    items = [x for v in streams.values() if isinstance(v, list) for x in v if isinstance(x, dict)]
    items.sort(key=lambda x: x.get('height') or 0, reverse=True)
    for item in items:
        urls = item.get('backupUrls') or [item.get('masterUrl')]
        if urls and urls[0]:
            return https_url(urls[0])
    raise Limit('NO_PUBLIC_VIDEO_STREAM')


def douyin_item(state, expected):
    for loader in state.get('loaderData', {}).values():
        if not isinstance(loader, dict):
            continue
        for item in loader.get('videoInfoRes', {}).get('item_list', []) or []:
            if str(item.get('aweme_id')) == expected:
                return item
    raise Limit('SELECTED_WORK_NOT_IN_PAGE')


def yt_metadata(url, expected):
    import yt_dlp
    messages = []

    class Quiet:
        def debug(self, msg): pass
        def warning(self, msg): messages.append(msg)
        def error(self, msg): messages.append(msg)

    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'logger': Quiet(),
                              'cachedir': False, 'noplaylist': True, 'socket_timeout': 20,
                              'retries': 0, 'extractor_retries': 0, 'fragment_retries': 0,
                              'cookiefile': None, 'cookiesfrombrowser': None}) as ydl:
            info = ydl.extract_info(url, download=False, ie_key='Douyin')
    except Exception as exc:
        message = ' '.join(messages) + ' ' + str(exc)
        if re.search(r'certificate.verify.failed|certificate verify failed|CERTIFICATE_VERIFY_FAILED', message, re.I):
            raise Limit('TLS_CERTIFICATE_VERIFICATION_FAILED_NOT_BYPASSED') from None
        if re.search(r'captcha|security verification|verification challenge|HTTP Error (?:401|403|429)|sign in|log.?in', message, re.I):
            raise Limit('ACCESS_OR_LOGIN_REQUIRED') from None
        if 'Fresh cookies' in message:
            raise Limit('YT_DLP_REQUIRES_FRESH_COOKIES_NOT_READ') from None
        raise Limit('YT_DLP_METADATA_FAILED') from None
    if not info or str(info.get('id')) != expected or info.get('_type', 'video') != 'video':
        raise Limit('SELECTED_WORK_MISMATCH')
    formats = [f for f in info.get('formats', []) if f.get('protocol') in ('http', 'https')
               and f.get('vcodec') != 'none' and f.get('acodec') != 'none'
               and trusted(https_url(f.get('url', '')), MEDIA_HOSTS['douyin'])]
    formats.sort(key=lambda f: (f.get('height') or 0, f.get('tbr') or 0), reverse=True)
    if not formats:
        raise Limit('NO_ALLOWED_COMBINED_HTTP_VIDEO')
    return info, https_url(formats[0]['url'])


def download(media_url, platform, output):
    path = output/'video.mp4.part'
    referer = 'https://www.xiaohongshu.com/' if platform == 'xiaohongshu' else 'https://www.douyin.com/'
    started = time.monotonic()
    digest = hashlib.sha256()
    total = 0
    with open_url(media_url, MEDIA_HOSTS[platform], MOBILE_UA, referer) as response, path.open('xb') as target:
        if any(x in response.headers.get('Content-Type', '').lower() for x in ('text/html', 'application/json')):
            raise Limit('MEDIA_RESPONSE_IS_NOT_VIDEO')
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > MEDIA_LIMIT or time.monotonic() - started > 180:
                raise Limit('MEDIA_SIZE_OR_DURATION_LIMIT')
            digest.update(block)
            target.write(block)
    import av
    with av.open(str(path)) as container:
        if not container.streams.video or next(container.decode(video=0), None) is None:
            raise Limit('DOWNLOADED_MEDIA_NOT_DECODABLE')
    final = output/'video.mp4'
    if final.exists():
        raise Limit('OUTPUT_ALREADY_EXISTS')
    path.rename(final)
    return {'file': str(final.resolve()), 'bytes': total, 'sha256': digest.hexdigest()}


def public_text(value):
    text = re.sub(r'https?://\S+', '[链接省略]', str(value or ''))
    return re.sub(r'(?i)(?:xsec_token|cookie|token)\s*[=:]\s*[^\s&]+', '[访问参数省略]', text)


def execute(payload, platform, route, output):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    result = {'platform': platform, 'route': route, 'started_at': datetime.now(timezone.utc).isoformat(),
              'status': 'failed', 'attempt_count': 1, 'browser_cookies_read': False,
              'access_parameters_logged': False, 'identity_check': 'pending_user_visual_review'}
    try:
        result['tool_versions'] = {'python': sys.version.split()[0],
                                   'yt_dlp': importlib.metadata.version('yt-dlp'),
                                   'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                   'xhs_field_reference': 'XHS-Downloader/tag/2.8'}
        url = selected_url(payload['share_text'], platform)
        result['input_sha256'] = hashlib.sha256(url.encode()).hexdigest()
        result['input_title'] = public_text(payload.get('title', ''))
        result['input_author'] = public_text(payload.get('author', ''))
        original_id = work_id(url, platform)
        if platform == 'douyin' and route == 'yt-dlp' and original_id:
            final, html = url, ''
        else:
            final, html = page(url, platform, MOBILE_UA if platform == 'douyin' else PC_UA)
        expected = work_id(final, platform)
        if not expected or (original_id and original_id != expected):
            raise Limit('REDIRECT_WORK_ID_MISSING_OR_CHANGED')
        result['work_id'] = expected
        result['canonical_url'] = ('https://www.xiaohongshu.com/explore/' if platform == 'xiaohongshu'
                                   else 'https://www.douyin.com/video/') + expected
        if platform == 'xiaohongshu':
            note = xhs_note(js_state(html, '__INITIAL_STATE__'), expected)
            if note.get('type') != 'video':
                raise Limit('SELECTED_NOTE_IS_NOT_VIDEO')
            result.update(title=note.get('title', ''), author=note.get('user', {}).get('nickname') or note.get('user', {}).get('nickName', ''),
                          metadata_source='selected_share_page_initial_state', metric_trust='not_collected')
            media = xhs_media(note)
        elif route == 'yt-dlp':
            # The extractor only supports /video/<id>; preserve the landing query in memory.
            query = urlsplit(final).query
            target = result['canonical_url'] + ('?' + query if query else '')
            info, media = yt_metadata(target, expected)
            result.update(title=info.get('title', ''), author=info.get('channel') or info.get('uploader', ''),
                          metadata_source='yt_dlp_selected_detail', metric_trust='not_collected')
        else:
            if 'iesdouyin.com' not in (urlsplit(final).hostname or ''):
                _, html = page('https://www.iesdouyin.com/share/video/' + expected + '/'
                               + ('?' + urlsplit(final).query if urlsplit(final).query else ''), platform, MOBILE_UA)
            item = douyin_item(js_state(html, '_ROUTER_DATA'), expected)
            result.update(title=item.get('desc', ''), author=item.get('author', {}).get('nickname', ''),
                          metadata_source='selected_mobile_share_router_data', metric_trust='not_collected')
            urls = item.get('video', {}).get('play_addr', {}).get('url_list', [])
            media = next((https_url(u) for u in urls if trusted(https_url(u), MEDIA_HOSTS[platform])), None)
            if not media:
                raise Limit('NO_ALLOWED_PUBLIC_VIDEO_STREAM')
        result['title'] = public_text(result.get('title', ''))
        result['author'] = public_text(result.get('author', ''))
        result['media'] = download(media, platform, output)
        result['status'] = 'downloaded_pending_content_identity_review'
    except Limit as exc:
        result['reason'] = str(exc)
    except Exception as exc:
        result['reason'] = 'LOCAL_OR_NETWORK_' + type(exc).__name__
    # Failure branches can bypass the successful-path cleanup above.
    for field in ('title', 'author', 'input_title', 'input_author'):
        if field in result:
            result[field] = public_text(result[field])
    with (output/'download-result.json').open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=PAGE_HOSTS, required=True)
    parser.add_argument('--route', choices=('yt-dlp', 'share-page'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.platform == 'xiaohongshu' and args.route != 'share-page':
        parser.error('XHS candidate uses share-page only')
    try:
        payload = json.loads(sys.stdin.buffer.read(65536).decode('utf-8-sig'))
        result = execute(payload, args.platform, args.route, args.output)
    except Exception as exc:
        print(json.dumps({'status': 'not_started', 'reason': type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'].startswith('downloaded_') else 2


if __name__ == '__main__':
    sys.exit(main())
