"""Plan-bound sequential platform export; preserve prior sessions and never replay unknown starts."""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error

_spec = importlib.util.spec_from_file_location('content_ops_bridge', Path(__file__).with_name('run_export.py'))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)
feedback = bridge.feedback
VERSION = 'PAY-S3-09-revision2'
MAX_READ = 8 * 1024 * 1024


class BatchError(ValueError):
    pass


def local(value):
    p = Path(value)
    if not p.is_absolute() or str(p).startswith(('\\\\', '//')):
        raise BatchError('absolute_local_path_required')
    if os.name == 'nt':
        kind = ctypes.windll.kernel32.GetDriveTypeW(str(p.anchor))
        require(kind in (2, 3), 'network_or_unverified_drive_not_supported')
    for part in [*reversed(p.parents), p]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise BatchError('reparse_path_not_supported')
    return p.resolve()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    path = local(path)
    with path.open('rb') as stream:
        raw = stream.read(MAX_READ + 1)
    if len(raw) > MAX_READ:
        raise BatchError('input_too_large')
    return feedback.load_json(raw.decode('utf-8-sig')), digest_bytes(raw)


def write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded(value))


def require(condition, code):
    if not condition:
        raise BatchError(code)


def request(platforms, aliases, tool_root, panel, seconds):
    require(isinstance(platforms, list) and 1 <= len(platforms) <= 3
            and all(isinstance(p, str) and p in feedback.PLATFORMS for p in platforms)
            and len(set(platforms)) == len(platforms), 'selected_platforms_invalid')
    require(isinstance(aliases, dict) and set(aliases) == set(platforms), 'one_alias_per_selected_platform_required')
    require(all(isinstance(a, str) and a.strip() and len(a) <= 200
                and a == feedback.safe_text(a, 200) for a in aliases.values()), 'account_alias_invalid')
    require(type(seconds) is int and 30 <= seconds <= 1200, 'wait_limit_invalid')
    root = local(tool_root)
    require(root.is_dir(), 'tool_root_missing')
    client = bridge.Panel(panel)
    return dict(platforms=platforms, account_aliases=aliases, tool_root=str(root), panel=client.base, seconds=seconds)


def source_hashes(req):
    root = local(req['tool_root'])
    scripts = Path(__file__).resolve().parent
    files = [Path(__file__).resolve(), scripts / 'run_export.py', scripts / 'summarize_export.py',
             root / 'panel.html', root / 'launcher.py', root / 'scripts/mediaexport_maintenance.py']
    files += [root / 'scripts' / name for name in ('adaptive.py', 'tool_utils.py', 'resilience.py', 'run_paths.py', 'export_results.py')]
    files += [root / 'scripts' / (p + '_export_all.py') for p in req['platforms']]
    if 'wechat' in req['platforms']:
        files += [root / 'scripts/wechat' / name for name in ('run_video_all_export.py', 'run_source_dist_export.py',
                  'run_single_video_export_fresh.py', 'run_follower_export.py', 'run_fans_portrait_export.py', 'dl_utils.py')]
    return {str(local(p)): feedback.sha256(local(p)) for p in files}


def checked_result(directory, platform):
    result, digest = read_json(directory / 'result.json')
    require(isinstance(result, dict) and result.get('expected_platforms') == [platform], 'result_platform_invalid')
    for key in ('downloaded', 'no_data', 'failed'):
        require(isinstance(result.get(key), list) and all(isinstance(r, dict) and r.get('platform') == platform for r in result[key]), 'result_rows_invalid')
    counts = result.get('summary')
    require(isinstance(counts, dict) and counts.get('ok') == len(result['downloaded'])
            and counts.get('no_data') == len(result['no_data']) and counts.get('fail') == len(result['failed']), 'result_counts_invalid')
    files = {str(directory / 'result.json'): digest}
    for row in result['downloaded']:
        path = local(row['file'])
        require(path.is_relative_to(directory) and path.is_file(), 'archived_file_outside_attempt_or_missing')
        require(feedback.sha256(path) == row.get('sha256'), 'archived_file_hash_changed')
        files[str(path)] = row['sha256']
    return result, files


def inspect_parent(root, req):
    root = local(root)
    saved, saved_hash = read_json(root / 'request.json')
    require(saved == req, 'resume_selection_account_or_environment_changed')
    require((root / 'prepared.json').is_file(), 'batch_preparation_incomplete_manual_reconciliation_required')
    prepared, prepared_hash = read_json(root / 'prepared.json')
    require(prepared == dict(version=VERSION, platforms=req['platforms']), 'prepared_marker_invalid')
    states, hashes = {}, {str(root / 'request.json'): saved_hash, str(root / 'prepared.json'): prepared_hash}
    require(not (root / 'resume.claim.json').exists(), 'parent_already_resumed_use_recorded_child')
    for platform in req['platforms']:
        directory = root / platform
        if not directory.exists():
            states[platform] = 'not_started'
            continue
        # An attempt directory alone may mean a crash immediately before its intent.
        # Be conservative: never infer that it is safe to POST again.
        require((directory / 'intent.json').is_file(), 'attempt_incomplete_manual_reconciliation_required')
        intent, h = read_json(directory / 'intent.json')
        require(isinstance(intent, dict) and intent.get('platform') == platform, 'attempt_platform_invalid')
        hashes[str(directory / 'intent.json')] = h
        packet_path = directory / 'packet.json'
        if not packet_path.exists():
            states[platform] = 'unconfirmed'
            continue
        packet, h = read_json(packet_path)
        require(isinstance(packet, dict) and type(packet.get('success')) is bool
                and isinstance(packet.get('packet'), dict) and packet['packet'].get('platform') == platform,
                'saved_packet_invalid')
        hashes[str(packet_path)] = h
        if not (directory / 'result.json').exists():
            states[platform] = 'archive_pending'
            continue
        result, file_hashes = checked_result(directory, platform)
        hashes.update(file_hashes)
        if not result['failed']:
            states[platform] = 'completed'
        elif any(row.get('issue_type') == 'archive_failed' for row in result['failed']):
            states[platform] = 'archive_pending'
        elif result['downloaded'] or result['no_data']:
            states[platform] = 'partial_failed'
        elif all(row.get('source_status') == 'FAIL' for row in result['failed']):
            states[platform] = 'failed'
        else:
            states[platform] = 'verification_failed'
    return states, hashes


def make_plan(req, target, parent=None, retry_failed=False):
    # Revalidate every field even when it came from a saved plan.
    require(isinstance(req, dict), 'request_object_required')
    req = request(req['platforms'], req['account_aliases'], req['tool_root'], req['panel'], req['seconds'])
    target = local(target)
    require(not target.exists() and target.parent.is_dir(), 'new_batch_target_required')
    tool = local(req['tool_root'])
    require(target != tool and not target.is_relative_to(tool), 'batch_target_inside_tool')
    states, parent_hashes = {}, {}
    if parent:
        parent = local(parent)
        require(target != parent and not target.is_relative_to(parent) and not parent.is_relative_to(target), 'batch_target_overlaps_parent')
        states, parent_hashes = inspect_parent(parent, req)
    actions, blockers = [], []
    for p in req['platforms']:
        state = states.get(p, 'not_started')
        if state == 'completed':
            action = 'inherit'
        elif state == 'archive_pending':
            action = 'archive_local'
        elif state == 'failed' and retry_failed:
            action = 'export'
        elif state == 'not_started':
            action = 'export'
        elif state in ('failed', 'partial_failed') and not retry_failed:
            action = 'inherit'
        else:
            action = 'blocked'
            blockers.append(p + ':' + state)
        actions.append(dict(platform=p, previous_state=state, action=action))
    inspection = None
    if any(a['action'] == 'export' for a in actions) and not blockers:
        inspection = bridge.preflight(bridge.Panel(req['panel']), tool)
    return dict(schema_version=1, version=VERSION, request=req, target=str(target),
                parent=str(parent) if parent else None, retry_failed=bool(retry_failed),
                actions=actions, blockers=blockers, source_hashes=source_hashes(req),
                parent_hashes=parent_hashes, preflight=inspection, export_started=False,
                account_identity_verified=False, authorization_granted=False,
                retry_unit='platform_only_when_zero_preserved_success_and_no_data')


def persist_result(directory, result):
    write_json(directory / 'result.json', result)
    with (directory / 'feedback.md').open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(feedback.render(result))


def inherit(parent, directory, platform):
    prior = parent / platform
    result, _ = checked_result(prior, platform)
    packet, _ = read_json(prior / 'packet.json')
    write_json(directory / 'packet.json', packet)
    reports = directory / 'reports'
    reports.mkdir()
    copied = {}
    for i, row in enumerate(result['downloaded']):
        source = local(row['file'])
        if source not in copied:
            destination = reports / ('preserved_' + str(i) + source.suffix)
            with source.open('rb') as reader, destination.open('xb') as writer:
                bridge.shutil.copyfileobj(reader, writer)
            require(feedback.sha256(destination) == row['sha256'], 'preserved_copy_hash_mismatch')
            copied[source] = destination
        row['file'] = str(copied[source])
    result['archived_output_root'] = str(reports)
    result['summary']['physical_files'] = len(copied)
    result['preserved_from'] = str(prior)
    persist_result(directory, result)
    return result


def run_action(plan, action, root):
    p = action['platform']
    req = plan['request']
    parent = local(plan['parent']) if plan['parent'] else None
    directory = root / p
    # Recheck idle immediately before each POST, before recording intent.
    client = bridge.Panel(req['panel'])
    if action['action'] == 'export':
        bridge.preflight(client, local(req['tool_root']))
    if action['action'] == 'archive_local':
        saved, _ = read_json(directory / 'packet.json')
    else:
        directory.mkdir(exist_ok=False)
        write_json(directory / 'intent.json', dict(platform=p, action=action['action'], parent=str(parent) if parent else None))
        # Intent precedes exactly one POST. No caught failure ever causes a replay.
        reply = client.json('/api/run/' + p, 'POST')
        require(isinstance(reply, dict) and isinstance(reply.get('task_id'), str) and reply['task_id'], 'start_acknowledgement_unconfirmed')
        write_json(directory / 'task.json', dict(platform=p, task_id=reply['task_id']))
        packet, success = bridge.stream_result(client, reply['task_id'], p, req['seconds'])
        saved = dict(packet=packet, success=success)
        write_json(directory / 'packet.json', saved)
    result = bridge.archive_result(saved['packet'], saved['success'], p, local(req['tool_root']), directory)
    persist_result(directory, result)
    return result


def execute_plan(path, approved_hash):
    path = local(path)
    value, digest = read_json(path)
    require(isinstance(approved_hash, str) and digest == approved_hash.lower(), 'approval_hash_mismatch')
    require(isinstance(value, dict) and value.get('schema_version') == 1 and value.get('version') == VERSION, 'plan_version_invalid')
    fresh = make_plan(value['request'], value['target'], value['parent'], value['retry_failed'])
    require(encoded(fresh).encode('utf-8') == path.read_bytes(), 'plan_stale_or_modified')
    require(not fresh['blockers'], 'unconfirmed_or_partial_result_blocks_execution')
    root = local(fresh['target'])
    root.mkdir(exist_ok=False)
    # Parent can have only one continuation, even across different output paths.
    # A crash leaves the claim in place; it is never automatically cleared.
    if fresh['parent']:
        write_json(local(fresh['parent']) / 'resume.claim.json', dict(child=str(root), approved_plan_sha256=digest))
    write_json(root / 'approved-plan.json', fresh)
    write_json(root / 'request.json', fresh['request'])
    summary = dict(version=VERSION, root=str(root), approved_plan_sha256=digest, results=[],
                   state='incomplete', stopped=False, export_started=False)
    inherited = {}
    # Preserve ALL completed results and known final packets before any new POST.
    # An interrupted preparation cannot be resumed as a fresh export session.
    try:
        for action in fresh['actions']:
            if action['action'] not in ('inherit', 'archive_local'):
                continue
            p = action['platform']
            directory = root / p
            directory.mkdir(exist_ok=False)
            write_json(directory / 'intent.json', dict(platform=p, action=action['action'], parent=fresh['parent']))
            parent = local(fresh['parent'])
            if action['action'] == 'inherit':
                inherited[p] = inherit(parent, directory, p)
            else:
                saved, _ = read_json(parent / p / 'packet.json')
                write_json(directory / 'packet.json', saved)
        write_json(root / 'prepared.json', dict(version=VERSION, platforms=fresh['request']['platforms']))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        summary.update(state='needs_attention', stopped=True, phase='prepare_inherited_results',
                       error='batch_preparation_incomplete', error_type=type(exc).__name__)
        write_json(root / 'batch-summary.json', summary)
        print('继承准备未完成，保留原件且不发起新导出：' + str(root / 'batch-summary.json'), flush=True)
        return 1
    for action in fresh['actions']:
        platform = action['platform']
        print(feedback.PLATFORMS[platform] + '：' + action['action'], flush=True)
        try:
            result = inherited[platform] if action['action'] == 'inherit' else run_action(fresh, action, root)
            summary['results'].append(dict(platform=platform, action=action['action'],
                                           counts=result['summary'], result=str(root / platform / 'result.json')))
            if action['action'] == 'export':
                summary['export_started'] = True
            if any(row.get('issue_type') == 'archive_failed' for row in result['failed']):
                summary['stopped'] = True
                break
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
            # Do not publish arbitrary HTTP/dependency messages. Intent/task/packet
            # files identify the last durable stage and remain authoritative.
            summary['results'].append(dict(platform=platform, action=action['action'],
                                           error='platform_step_incomplete', error_type=type(exc).__name__))
            summary['stopped'] = True
            summary['export_started'] = summary['export_started'] or (action['action'] == 'export' and (root / platform / 'intent.json').exists())
            break
    all_done = len(summary['results']) == len(fresh['actions']) and not summary['stopped']
    no_failures = all('counts' in r and r['counts']['fail'] == 0 for r in summary['results'])
    summary['state'] = 'completed' if all_done and no_failures else 'needs_attention'
    write_json(root / 'batch-summary.json', summary)
    print('本轮编排结果：' + str(root / 'batch-summary.json'), flush=True)
    return int(summary['state'] != 'completed')


def main(argv=None):
    if sys.version_info < (3, 10):
        print(json.dumps(dict(state='batch_incomplete', error_code='python_3_10_or_later_required',
                              automatic_retry=False, automatic_cancel=False)), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description='多平台顺序调用与安全恢复候选，默认只生成计划')
    sub = parser.add_subparsers(dest='command', required=True)
    plan = sub.add_parser('plan')
    plan.add_argument('--platforms', nargs='+', choices=feedback.PLATFORMS)
    plan.add_argument('--account-alias', action='append', default=[])
    plan.add_argument('--tool-root', type=Path)
    plan.add_argument('--panel', default='http://127.0.0.1:8766')
    plan.add_argument('--seconds', type=int, default=480)
    plan.add_argument('--save-to', type=Path, required=True)
    plan.add_argument('--resume-from', type=Path)
    plan.add_argument('--retry-failed', action='store_true')
    plan.add_argument('--plan-out', type=Path, required=True)
    execute = sub.add_parser('execute')
    execute.add_argument('--plan', type=Path, required=True)
    execute.add_argument('--approved-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'execute':
            return execute_plan(args.plan, args.approved_sha256)
        if args.resume_from:
            require(not args.platforms and not args.account_alias and args.tool_root is None
                    and args.panel == 'http://127.0.0.1:8766' and args.seconds == 480,
                    'resume_must_reuse_original_request')
            req, _ = read_json(local(args.resume_from) / 'request.json')
        else:
            aliases = {}
            for entry in args.account_alias:
                p, sep, alias = entry.partition('=')
                require(sep and p not in aliases, 'account_alias_format_or_duplicate')
                aliases[p] = alias
            req = request(args.platforms, aliases, args.tool_root, args.panel, args.seconds)
        value = make_plan(req, args.save_to, args.resume_from, args.retry_failed)
        destination = local(args.plan_out)
        target = local(args.save_to)
        require(not destination.is_relative_to(target), 'plan_cannot_create_batch_target')
        if args.resume_from:
            require(not destination.is_relative_to(local(args.resume_from)), 'plan_cannot_modify_parent_batch')
        write_json(destination, value)
        print(json.dumps(dict(plan=str(destination), sha256=feedback.sha256(destination),
                              actions=value['actions'], blockers=value['blockers'], execute=False), ensure_ascii=False))
        return int(bool(value['blockers']))
    except (OSError, ValueError, TypeError, KeyError, urllib.error.URLError) as exc:
        code = str(exc) if isinstance(exc, BatchError) else 'batch_input_preflight_or_io_failed'
        print(json.dumps(dict(state='batch_incomplete', error_code=code, error_type=type(exc).__name__,
                              automatic_retry=False, automatic_cancel=False), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
