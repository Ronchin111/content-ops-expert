"""Report-only retry candidate. Requires the isolated tool adapter; never falls back to all."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request


def sibling(name):
    spec = importlib.util.spec_from_file_location('report_retry_' + name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = sibling('run_batch')
selection = sibling('report_selection')
bridge, feedback = batch.bridge, batch.feedback
VERSION = 'PAY-S3-10-candidate1'
require, local, read_json, write_json = batch.require, batch.local, batch.read_json, batch.write_json


class SelectedPanel(bridge.Panel):
    def submit_selected(self, platform, ids):
        body = batch.encoded(selection.payload(platform, ids)).encode('utf-8')
        req = urllib.request.Request(self.base + '/api/run-selected/' + platform, data=body,
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with self.opener.open(req, timeout=12) as response:
            raw = response.read(bridge.MAX_HTTP + 1)
        require(len(raw) <= bridge.MAX_HTTP, 'selected_ack_too_large')
        return feedback.load_json(raw.decode('utf-8'))


def capabilities(client):
    require(client.json('/api/report-capabilities') == selection.catalogue(), 'report_selection_not_supported')


def original_request(value):
    require(isinstance(value, dict), 'request_object_required')
    return batch.request(value['platforms'], value['account_aliases'], value['tool_root'], value['panel'], value['seconds'])


def result_tasks(result, platform):
    names = [row['task'] for group in ('downloaded', 'no_data', 'failed') for row in result[group]]
    allowed = {title for _, title in selection.REPORTS[platform]}
    require(len(names) == len(set(names)) and set(names) == allowed, 'unknown_duplicate_or_missing_report_task')


def load_parent(root, platform):
    root = local(root)
    require(not (root / 'resume.claim.json').exists(), 'parent_already_resumed_use_recorded_child')
    hashes, forced_ids = {}, None
    if (root / 'retry-context.json').exists():
        context, digest = read_json(root / 'retry-context.json')
        require(isinstance(context, dict) and context.get('version') == VERSION, 'retry_context_invalid')
        req = original_request(context['request'])
        hashes[str(root / 'retry-context.json')] = digest
        marker, digest = read_json(root / 'preserved-ready.json')
        require(marker == dict(version=VERSION, platforms=req['platforms']), 'retry_preservation_incomplete')
        hashes[str(root / 'preserved-ready.json')] = digest
        if (root / 'retry-packet.json').exists():
            _, packet_hash = read_json(root / 'retry-packet.json')
            hashes[str(root / 'retry-packet.json')] = packet_hash
        if (root / 'retry-complete.json').exists():
            complete, digest = read_json(root / 'retry-complete.json')
            require(complete.get('version') == VERSION and complete.get('finished') is True, 'retry_completion_invalid')
            hashes[str(root / 'retry-complete.json')] = digest
            base, action = root, 'export_selected'
        else:
            require(context['platform'] == platform, 'unfinished_retry_platform_must_match')
            forced_ids = selection.validate(platform, context['ids'])
            base = root / 'preserved'
            if (root / 'retry-packet.json').exists():
                _, digest = read_json(root / 'retry-packet.json')
                hashes[str(root / 'retry-packet.json')] = digest
                action = 'archive_local'
            elif not (root / 'retry_attempt').exists():
                action = 'export_selected'
            else:
                raise batch.BatchError('retry_start_or_finish_unconfirmed_no_repost')
    else:
        saved, _ = read_json(root / 'request.json')
        req = original_request(saved)
        states, hashes = batch.inspect_parent(root, req)
        require(all(s in ('completed', 'partial_failed', 'failed') for s in states.values()),
                'finish_or_reconcile_original_batch_first')
        base, action = root, 'export_selected'
    require(platform in req['platforms'], 'platform_not_in_original_batch')
    results = {}
    for p in req['platforms']:
        result, recorded = batch.checked_result(base / p, p)
        result_tasks(result, p)
        results[p] = result
        hashes.update(recorded)
    failed = results[platform]['failed']
    require(failed and all(r.get('source_status') == 'FAIL' for r in failed), 'only_confirmed_failed_reports_can_retry')
    failed_names = {r['task'] for r in failed}
    ids = [i for i,title in selection.REPORTS[platform] if title in failed_names]
    selection.validate(platform, ids)
    require(len(ids) == len(failed) and (forced_ids is None or forced_ids == ids), 'failed_report_selection_changed')
    return req, base, action, ids, hashes


def retry_sources(req):
    hashes = batch.source_hashes(req)
    for path in (Path(__file__).resolve(), Path(__file__).with_name('run_batch.py'),
                 Path(__file__).with_name('report_selection.py'), Path(sys.executable),
                 local(req['tool_root']) / 'scripts/report_selection.py'):
        path = local(path)
        hashes[str(path)] = feedback.sha256(path)
    return hashes


def make_plan(parent, platform, target):
    require(platform in selection.REPORTS, 'report_platform_invalid')
    parent, target = local(parent), local(target)
    require(not target.exists() and target.parent.is_dir(), 'new_retry_target_required')
    require(target != parent and not target.is_relative_to(parent) and not parent.is_relative_to(target), 'retry_target_overlaps_parent')
    req, base, action, ids, hashes = load_parent(parent, platform)
    tool = local(req['tool_root'])
    require(target != tool and not target.is_relative_to(tool), 'retry_target_inside_tool')
    inspection = None
    if action == 'export_selected':
        client = SelectedPanel(req['panel'])
        inspection = bridge.preflight(client, tool)
        capabilities(client)
    return dict(schema_version=1, version=VERSION, parent=str(parent), base=str(base), target=str(target),
                request=req, platform=platform, ids=ids, action=action, parent_hashes=hashes,
                source_hashes=retry_sources(req), preflight=inspection, authorization_granted=False,
                account_identity_verified=False, successful_reports_reexported=False)


def copy_rows(rows, reports, prefix):
    copied, answer = {}, []
    for index, row in enumerate(rows):
        source = local(row['file'])
        require(source.is_file() and feedback.sha256(source) == row['sha256'], 'preserved_report_changed')
        if source not in copied:
            suffix = source.suffix if source.suffix.lower() in {'.csv', '.xlsx', '.txt', '.json'} else '.bin'
            destination = reports / (prefix + str(index) + suffix)
            with source.open('rb') as reader, destination.open('xb') as writer:
                bridge.shutil.copyfileobj(reader, writer)
            require(feedback.sha256(destination) == row['sha256'], 'preserved_copy_hash_mismatch')
            copied[source] = destination
        answer.append(dict(row, file=str(copied[source])))
    return answer


def copy_result(source, target, platform):
    result, _ = batch.checked_result(source, platform)
    target.mkdir(exist_ok=False)
    reports = target / 'reports'
    reports.mkdir()
    result['downloaded'] = copy_rows(result['downloaded'], reports, 'preserved_')
    result['archived_output_root'] = str(reports)
    result['summary']['physical_files'] = len({r['file'] for r in result['downloaded']})
    batch.persist_result(target, result)


def merge_result(base, retried, directory, platform, packet_path):
    old, _ = batch.checked_result(base, platform)
    directory.mkdir(exist_ok=False)
    reports = directory / 'reports'
    reports.mkdir()
    old['downloaded'] = copy_rows(old['downloaded'], reports, 'preserved_') + copy_rows(retried['downloaded'], reports, 'retried_')
    old['no_data'] += retried['no_data']
    old['failed'] = retried['failed']
    old['summary'] = dict(ok=len(old['downloaded']), no_data=len(old['no_data']), fail=len(old['failed']),
                          fallback=sum(r['status'] == 'OK(兜底)' for r in old['downloaded']),
                          physical_files=len({r['file'] for r in old['downloaded']}))
    old['state'] = 'completed' if not old['failed'] else 'needs_attention'
    old['archived_output_root'] = str(reports)
    old['report_retry_provenance'] = dict(preserved_result=str(base / 'result.json'), retry_packet=str(packet_path))
    result_tasks(old, platform)
    batch.persist_result(directory, old)


def execute(path, approved):
    path = local(path)
    saved, digest = read_json(path)
    require(isinstance(approved, str) and digest == approved.lower(), 'approval_hash_mismatch')
    require(isinstance(saved, dict) and saved.get('schema_version') == 1 and saved.get('version') == VERSION, 'retry_plan_version_invalid')
    plan = make_plan(saved['parent'], saved['platform'], saved['target'])
    require(batch.encoded(plan).encode('utf-8') == path.read_bytes(), 'retry_plan_stale_or_modified')
    root, parent, base = local(plan['target']), local(plan['parent']), local(plan['base'])
    root.mkdir(exist_ok=False)
    write_json(parent / 'resume.claim.json', dict(child=str(root), approved_plan_sha256=digest, kind='report_retry'))
    write_json(root / 'approved-plan.json', plan)
    context = dict(version=VERSION, request=plan['request'], platform=plan['platform'], ids=plan['ids'])
    write_json(root / 'retry-context.json', context)
    try:
        preserved = root / 'preserved'
        preserved.mkdir()
        for p in plan['request']['platforms']:
            copy_result(base / p, preserved / p, p)
        write_json(root / 'preserved-ready.json', dict(version=VERSION, platforms=plan['request']['platforms']))
        p = plan['platform']
        attempt = root / 'retry_attempt'
        if plan['action'] == 'export_selected':
            client = SelectedPanel(plan['request']['panel'])
            bridge.preflight(client, local(plan['request']['tool_root']))
            capabilities(client)
            attempt.mkdir()
            write_json(attempt / 'intent.json', context)
            reply = client.submit_selected(p, plan['ids'])
            require(isinstance(reply, dict) and isinstance(reply.get('task_id'), str) and reply['task_id']
                    and reply.get('selected_reports') == plan['ids'], 'selected_start_ack_unconfirmed')
            write_json(attempt / 'task.json', dict(task_id=reply['task_id'], platform=p, selected_reports=plan['ids']))
            packet, success = bridge.stream_result(client, reply['task_id'], p, plan['request']['seconds'])
            saved_packet = dict(packet=packet, success=success)
        else:
            saved_packet, _ = read_json(parent / 'retry-packet.json')
            attempt.mkdir()
        write_json(root / 'retry-packet.json', saved_packet)
        packet, success = saved_packet['packet'], saved_packet['success']
        expected = {dict(selection.REPORTS[p])[i] for i in plan['ids']}
        items = packet['items']
        require(type(success) is bool and isinstance(items, list) and len(items) == len(expected)
                and {r['task'] for r in items} == expected, 'retry_packet_report_set_mismatch')
        retried = bridge.archive_result(packet, success, p, local(plan['request']['tool_root']), attempt)
        batch.persist_result(attempt, retried)
        require(not any(r.get('issue_type') == 'archive_failed' for r in retried['failed']), 'retry_archive_pending_local_repair_required')
        actual = [r['task'] for key in ('downloaded', 'no_data', 'failed') for r in retried[key]]
        require(len(actual) == len(expected) and set(actual) == expected, 'retry_result_report_set_mismatch')
        for platform in plan['request']['platforms']:
            if platform == p:
                merge_result(preserved / p, retried, root / p, p, root / 'retry-packet.json')
            else:
                copy_result(preserved / platform, root / platform, platform)
        totals = {k: 0 for k in ('ok', 'no_data', 'fail', 'physical_files')}
        for platform in plan['request']['platforms']:
            result, _ = batch.checked_result(root / platform, platform)
            for key in totals:
                totals[key] += result['summary'][key]
        write_json(root / 'retry-complete.json', dict(version=VERSION, finished=True, action=plan['action'], totals=totals))
        print(json.dumps(dict(state='completed' if not totals['fail'] else 'needs_attention', totals=totals,
                              result=str(root / 'retry-complete.json')), ensure_ascii=False))
        return int(totals['fail'] > 0)
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        write_json(root / 'retry-error.json', dict(state='needs_attention', error_code='report_retry_incomplete',
                                                  error_type=type(exc).__name__, automatic_retry=False))
        print(json.dumps(dict(state='needs_attention', error_code='report_retry_incomplete', result=str(root)), ensure_ascii=False))
        return 1


def main(argv=None):
    if sys.version_info < (3, 10):
        print(json.dumps(dict(error_code='python_3_10_or_later_required')), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description='报表级恢复；默认只规划，要求已批准的筛选适配面板')
    sub = parser.add_subparsers(dest='command', required=True)
    plan_parser = sub.add_parser('plan')
    plan_parser.add_argument('--parent', type=Path, required=True)
    plan_parser.add_argument('--platform', choices=selection.REPORTS, required=True)
    plan_parser.add_argument('--save-to', type=Path, required=True)
    plan_parser.add_argument('--plan-out', type=Path, required=True)
    run = sub.add_parser('execute')
    run.add_argument('--plan', type=Path, required=True)
    run.add_argument('--approved-sha256', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'execute':
            return execute(args.plan, args.approved_sha256)
        value = make_plan(args.parent, args.platform, args.save_to)
        path = local(args.plan_out)
        for protected in (local(args.parent), local(args.save_to), local(value['request']['tool_root'])):
            require(not path.is_relative_to(protected), 'plan_output_overlaps_data_or_tool')
        write_json(path, value)
        print(json.dumps(dict(plan=str(path), sha256=feedback.sha256(path), action=value['action'], platform=value['platform'],
                              ids=value['ids'], execute=False), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        code = str(exc) if isinstance(exc, batch.BatchError) else 'report_retry_input_or_preflight_failed'
        print(json.dumps(dict(state='report_retry_incomplete', error_code=code, error_type=type(exc).__name__,
                              automatic_retry=False)), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
