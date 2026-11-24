"""Strict public catalogue and read-only worker protocol checks; no account/network access."""
import ast
import json
import os
from pathlib import Path

VERSION = 'PAY-S3-10-candidate1'
ENV_KEY = 'MEDIAEXPORT_REPORT_SELECTION'
REPORTS = {
    'douyin': [('play', '播放量数据'), ('works', '作品列表'), ('overview', '账号总览指标'), ('fans', '粉丝画像')],
    'wechat': [('all_videos', '全部视频'), ('single_video', '单篇视频'), ('followers', '关注者数据'), ('sources', '来源分布'), ('fans', '粉丝画像')],
    'xhs': [('watch', '观看数据'), ('interact', '互动数据'), ('growth', '涨粉数据'), ('publish', '发布数据'), ('content', '内容分析'), ('fans', '粉丝数据')],
}


def catalogue():
    return dict(schema_version=1, adapter_version=VERSION,
                reports={p: [dict(id=i, title=t) for i,t in rows] for p,rows in REPORTS.items()})


def supports_workers(directory, platforms):
    for platform in platforms:
        if platform not in REPORTS:
            return False
        try:
            path = Path(directory) / (platform + '_export_all.py')
            if path.stat().st_size > 524288:
                return False
            tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            markers = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'REPORT_SELECTION_PROTOCOL' for t in node.targets)]
            if markers != [VERSION]:
                return False
        except (OSError, ValueError, SyntaxError):
            return False
    return True


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_selection_field')
        value[key] = item
    return value


def validate(platform, ids):
    if platform not in REPORTS or not isinstance(ids, list) or not 1 <= len(ids) <= len(REPORTS[platform]):
        raise ValueError('report_selection_invalid')
    allowed = {i for i,_ in REPORTS[platform]}
    if any(not isinstance(i, str) or i not in allowed for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('report_selection_unknown_or_duplicate')
    return list(ids)


def payload(platform, ids):
    return dict(schema_version=1, platform=platform, ids=validate(platform, ids))


def parse_payload(text, platform):
    if not isinstance(text, str) or len(text.encode('utf-8')) > 4096:
        raise ValueError('report_selection_too_large')
    data = json.loads(text, object_pairs_hook=unique)
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'platform', 'ids'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1 or data['platform'] != platform):
        raise ValueError('report_selection_scope_mismatch')
    return validate(platform, data['ids'])


def environment_selection(platform):
    raw = os.environ.get(ENV_KEY)
    if raw is None:
        return None
    ids = parse_payload(raw, platform)
    names = dict(REPORTS[platform])
    return {names[i] for i in ids}


def choose(tasks, names):
    """Preserve original worker order; a report id selects exactly one emitted task."""
    return list(tasks) if names is None else [(name, worker) for name, worker in tasks if name in names]
