"""Read explicit MediaExport results offline; write new, grouped feedback only."""
import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import uuid

VERSION = "0.1.0"
PLATFORMS = {"douyin": "抖音", "wechat": "视频号", "xhs": "小红书"}
STATUSES = {"OK", "OK(兜底)", "NO_DATA", "FAIL"}
MAX_BYTES = 8 * 1024 * 1024
MARKER = "[EXPORT_RESULT] "
PREFIX = re.compile(r"^(?:\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] )?\[EXPORT_RESULT\] (.*)$")


class InvalidResult(ValueError):
    pass


class InputLimit(InvalidResult):
    pass


def safe_text(value, limit=1600):
    """Avoid echoing arbitrary log content; mask common credential assignments."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidResult("结果中的文字字段类型错误")
    value = re.sub(r"(?i)\b(authorization|cookie|set-cookie)\s*[:=][^\r\n]*", r"\1=[已遮盖]", value)
    value = re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\s*[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+", r"\1=[已遮盖]", value)
    return value[:limit]


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResult("结果JSON含重复字段")
        result[key] = value
    return result


def load_json(text):
    try:
        return json.loads(text, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as exc:
        raise InvalidResult("结果JSON无法解析或含重复字段") from exc


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_source(path, platform):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise InputLimit("超过本结果整理入口的8MiB处理上限，未核对该来源；这不代表平台导出失败，请指定本轮平台manifest")
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise InvalidResult("结果文件不存在、不可读或不是UTF-8") from exc
    provenance = {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}
    if text.lstrip().startswith("{"):
        packet = load_json(text)
        provenance["kind"] = "manifest_or_packet"
    else:
        # Parent WeChat packets include a manifest; child packets do not.
        # Do not let child packets or an old run silently replace the final one.
        final_packets = []
        for line in text.splitlines():
            match = PREFIX.fullmatch(line)
            if not match:
                continue
            candidate = load_json(match.group(1))
            if not isinstance(candidate, dict):
                raise InvalidResult("结构化日志结果不是对象")
            if candidate.get("platform") == platform and candidate.get("manifest"):
                if candidate not in final_packets:
                    final_packets.append(candidate)
        if len(final_packets) != 1:
            raise InvalidResult("日志缺少唯一的本轮最终平台包，请指定该平台manifest")
        packet = final_packets[0]
        provenance["kind"] = "structured_run_log"
    if not isinstance(packet, dict) or packet.get("platform") != platform:
        raise InvalidResult("结果平台与本轮指定平台不一致")
    if "schema_version" in packet and (type(packet["schema_version"]) is not int or packet["schema_version"] != 1):
        raise InvalidResult("结果schema_version不受支持")
    if "schema_version" in packet and (not isinstance(packet.get("manifest"), str) or not packet["manifest"].strip()):
        raise InvalidResult("结构化结果缺少最终manifest，不能用子任务包确认整个平台完成")
    if "schema_version" not in packet:
        run_at = packet.get("run_at")
        if not isinstance(run_at, str) or not run_at.strip():
            raise InvalidResult("无schema的输入缺少manifest必有的run_at，不能确认是平台清单")
        try:
            datetime.fromisoformat(run_at)
        except ValueError as exc:
            raise InvalidResult("manifest的run_at不是有效ISO时间，不能确认结果来源") from exc
    items = packet.get("items")
    if not isinstance(items, list):
        raise InvalidResult("结果items缺失或不是列表，不能确认平台结果")
    if not items:
        raise InvalidResult("清单没有任何任务结果，不能据此判断平台无数据；请提供本轮完整清单或有空态证据的NO_DATA项目")
    tasks = set()
    for item in items:
        if not isinstance(item, dict):
            raise InvalidResult("结果项目不是对象")
        task = item.get("task")
        if not isinstance(task, str) or not task.strip() or task in tasks:
            raise InvalidResult("结果项目名称为空、类型错误或重复")
        tasks.add(task)
    return items, provenance


def failure(platform, task, reason, kind, source_status=None, period=None):
    return {"platform": platform, "task": task, "status": "FAIL", "source_status": source_status,
            "period": period, "file": None, "rows": None, "verification": "not_confirmed",
            "error": reason, "issue_type": kind,
            "next_action": "保留已有成果；核对本轮日志、清单和输出目录，再处理本项问题。"}


def local_file(value, root):
    if not isinstance(value, str) or not value.strip():
        raise InvalidResult("导出器声明成功，但未提供文件路径")
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root):
        raise InvalidResult("报表路径超出指定output根目录，未读取该文件")
    if not path.is_file() or path.stat().st_size == 0:
        raise InvalidResult("导出器声明成功，但本地报表缺失或为空")
    return path


def normalize(item, platform, root):
    task = safe_text(item["task"], 200)
    status = item.get("status")
    period = safe_text(item.get("period"), 300)
    if not isinstance(status, str) or status not in STATUSES:
        return failure(platform, task, "导出结果状态未知，不能确认成功或无数据", "invalid_status", period=period)
    base = {"platform": platform, "task": task, "status": status, "source_status": status,
            "period": period, "file": None, "rows": None}
    rows = item.get("rows")
    if rows is not None and (type(rows) is not int or rows < 0):
        return failure(platform, task, "结果行数类型或数值异常，需核对清单", "invalid_rows", status, period)
    if status == "NO_DATA":
        reason, evidence = safe_text(item.get("reason")), safe_text(item.get("evidence"))
        checks = item.get("checks")
        if (not reason or not reason.strip() or not evidence or not evidence.strip()
                or not isinstance(checks, dict) or checks.get("platform_empty") is not True
                or item.get("file") not in (None, "") or item.get("error") or rows not in (None, 0)):
            return failure(platform, task, "无数据结果缺少平台空态证据，或与文件／错误／行数字段冲突", "invalid_no_data", status, period)
        return dict(base, reason=reason, evidence=evidence, rows=rows,
                    verification="exporter_empty_state_record_only")
    if status == "FAIL":
        reason = safe_text(item.get("error")) or "导出器未提供具体原因，请查看本次日志"
        return failure(platform, task, reason, "exporter_error", status, period)
    if item.get("error"):
        return failure(platform, task, "成功状态与非空error冲突，需核对导出结果", "conflicting_result", status, period)
    try:
        path = local_file(item.get("file"), root)
        fingerprint = sha256(path)
        size = path.stat().st_size
    except (InvalidResult, OSError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, InvalidResult) else "成功报表无法读取，未确认本地文件"
        return failure(platform, task, reason, "file_verification", status, period)
    return dict(base, file=str(path), sha256=fingerprint, bytes=size, rows=rows,
                verification="local_exists_nonempty_sha256_only")


def summarize(expected, sources, root):
    groups = {"downloaded": [], "no_data": [], "failed": []}
    provenance = {}
    for platform in expected:
        if platform not in sources:
            groups["failed"].append(failure(platform, "平台运行结果", "本轮缺少该平台结果，不能确认完成", "missing_result"))
            continue
        provenance[platform] = {"path": str(sources[platform].absolute()), "sha256": None,
                                "kind": "unverified_source"}
        try:
            items, provenance[platform] = read_source(sources[platform], platform)
        except InvalidResult as exc:
            kind = "input_limit" if isinstance(exc, InputLimit) else "invalid_source"
            groups["failed"].append(failure(platform, "平台运行结果", str(exc), kind))
            continue
        for item in items:
            try:
                row = normalize(item, platform, root)
            except (InvalidResult, TypeError, ValueError):
                status = item.get("status")
                source_status = status if isinstance(status, str) and status in STATUSES else None
                row = failure(platform, safe_text(item["task"], 200), "结果字段类型异常，不能确认成功或无数据", "invalid_item", source_status)
            key = "downloaded" if row["status"] in {"OK", "OK(兜底)"} else ("no_data" if row["status"] == "NO_DATA" else "failed")
            groups[key].append(row)
    counts = {"ok": len(groups["downloaded"]), "no_data": len(groups["no_data"]), "fail": len(groups["failed"]),
              "fallback": sum(row["status"] == "OK(兜底)" for row in groups["downloaded"]),
              "physical_files": len({row["file"] for row in groups["downloaded"]})}
    return {"schema_version": 1, "adapter_version": VERSION,
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "expected_platforms": expected, "tool_output_root": str(root), "sources": provenance,
            "state": "completed" if counts["fail"] == 0 else "needs_attention", "summary": counts, **groups}


def md(value):
    text = html.escape(str(value), quote=False).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def render(result):
    counts = result["summary"]
    lines = ["# 本轮导出结果", "", f"已下载 {counts['ok']} 项｜平台无数据跳过 {counts['no_data']} 项｜执行失败 {counts['fail']} 项", "",
             f"本地已找到 {counts['physical_files']} 份文件；项目数与文件数分别统计。", "",
             "已核对成功文件存在且非空。无数据原因来自导出器的页面记录；本次未重新访问后台，业务指标尚未复核。", ""]
    for key, title in (("downloaded", "已下载"), ("no_data", "平台无数据，已跳过下载"), ("failed", "执行失败／结果核对问题")):
        lines += [f"## {title}", ""]
        if not result[key]:
            lines += ["无。", ""]
        for row in result[key]:
            lines.append(f"- {md(PLATFORMS[row['platform']])}／{md(row['task'])}；统计范围：{md(row.get('period') or '未提供，不能据此比较')}。")
            if key == "downloaded":
                lines.append(f"  文件：{md(row['file'])}。")
                if row["status"] == "OK(兜底)":
                    lines.append("  导出器使用了兜底路径，已保留原状态。")
            elif key == "no_data":
                lines += [f"  原因：{md(row['reason'])}。", f"  导出器空态证据：{md(row['evidence'])}。"]
            else:
                lines += [f"  原因：{md(row['error'])}。", f"  下一步：{md(row['next_action'])}"]
        lines.append("")
    return "\n".join(lines)


def write_feedback(result, save_to):
    directory = save_to / (datetime.now().strftime("feedback_%Y%m%d_%H%M%S_") + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    for name, content in (("result.json", json.dumps(result, ensure_ascii=False, indent=2)), ("feedback.md", render(result))):
        with (directory / name).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content + "\n")
    return directory


def main(argv=None):
    if sys.version_info < (3, 9):
        print("本结果整理入口需要Python 3.9或更高版本，建议3.11+；未读取结果或写入反馈。", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="本地整理本轮三平台结果，不执行导出或网络请求")
    parser.add_argument("--expected", nargs="+", choices=PLATFORMS, required=True)
    parser.add_argument("--result", action="append", default=[], metavar="PLATFORM=FILE")
    parser.add_argument("--tool-output", type=Path, required=True)
    parser.add_argument("--save-to", type=Path, required=True)
    args = parser.parse_args(argv)
    if len(set(args.expected)) != len(args.expected):
        parser.error("expected平台不能重复")
    sources = {}
    for entry in args.result:
        platform, sep, path = entry.partition("=")
        if not sep or not path or platform not in args.expected or platform in sources:
            parser.error("result须为已选平台=文件，且每平台只能指定一次")
        sources[platform] = Path(path)
    try:
        root = args.tool_output.resolve(strict=True)
        if not root.is_dir():
            parser.error("tool-output须是已有工具output目录")
        result = summarize(args.expected, sources, root)
        destination = write_feedback(result, args.save_to.resolve())
    except (OSError, ValueError):
        print("结果读取或反馈保存失败；已有文件保留。请核对output目录和反馈保存权限。", file=sys.stderr)
        return 2
    print(f"已下载 {result['summary']['ok']} 项；平台无数据跳过 {result['summary']['no_data']} 项；执行失败 {result['summary']['fail']} 项")
    print(f"反馈已保存：{destination}")
    return int(result["summary"]["fail"] > 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
