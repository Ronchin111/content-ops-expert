"""Export outcome contract shared by workers, the panel and future Skill callers."""
import json

RESULT_PREFIX = '[EXPORT_RESULT] '
NO_DATA_EXIT_CODE = 20
STATUSES = {'OK', 'OK(兜底)', 'NO_DATA', 'FAIL'}


class NoDataAvailable(Exception):
    """Only raise after an explicit platform empty-state has been observed."""
    def __init__(self, reason, period=None, evidence=None):
        if not reason or not evidence:
            raise ValueError('无数据判断必须有明确的平台提示证据')
        super().__init__(reason)
        self.reason, self.period, self.evidence = reason, period, evidence


def no_data_item(task, reason, period=None, evidence=None):
    if not reason or not evidence:
        raise ValueError('无数据结果缺少平台证据')
    return {'task': task, 'status': 'NO_DATA', 'file': None, 'error': None,
            'reason': reason, 'period': period, 'rows': 0,
            'checks': {'platform_empty': True}, 'evidence': evidence, 'strategy': []}


def summarize_items(items):
    counts = {'ok': 0, 'fallback': 0, 'no_data': 0, 'fail': 0}
    for item in items:
        status = item.get('status')
        if status in {'OK', 'OK(兜底)'}:
            counts['ok'] += 1
            counts['fallback'] += int(status == 'OK(兜底)')
        elif status == 'NO_DATA':
            counts['no_data'] += 1
        else:
            counts['fail'] += 1
    return counts


def result_packet(platform, items, manifest=None):
    return {'schema_version': 1, 'platform': platform, 'items': items,
            'summary': summarize_items(items), 'manifest': manifest}


def parse_result_line(line):
    if not line.startswith(RESULT_PREFIX):
        return None
    try:
        packet = json.loads(line[len(RESULT_PREFIX):])
        if packet.get('platform') not in {'douyin', 'wechat', 'xhs'}:
            return None
        items = packet['items']
        if not isinstance(items, list) or not items:
            return None
        for item in items:
            if not isinstance(item, dict) or item.get('status') not in STATUSES or not isinstance(item.get('task'), str):
                return None
            if item['status'] == 'NO_DATA' and (item.get('file') or not item.get('reason') or not item.get('evidence')):
                return None
        packet['summary'] = summarize_items(items)
        return packet
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def emit_result(platform, items, manifest=None, log=print):
    packet = result_packet(platform, items, manifest)
    counts = packet['summary']
    log(f"运行结果：已下载 {counts['ok']} 项；平台无数据跳过 {counts['no_data']} 项；执行失败 {counts['fail']} 项")
    for item in items:
        status = item['status']
        detail = item.get('reason') if status == 'NO_DATA' else (item.get('error') if status == 'FAIL' else item.get('file'))
        log(f"[{status}] {item['task']}：{detail or '详见运行日志'}")
    print(RESULT_PREFIX + json.dumps(packet, ensure_ascii=False), flush=True)
    return packet
