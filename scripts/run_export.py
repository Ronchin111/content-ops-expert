"""Single-platform bridge to an already-running, trusted local MediaExport panel."""
import argparse
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

_feedback_spec = importlib.util.spec_from_file_location("content_ops_feedback", Path(__file__).resolve().with_name("summarize_export.py"))
feedback = importlib.util.module_from_spec(_feedback_spec)
_feedback_spec.loader.exec_module(feedback)

MAX_HTTP = 256 * 1024
MAX_EVENT = 1024 * 1024


class BridgeError(ValueError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Panel:
    def __init__(self, base):
        try:
            parsed = urllib.parse.urlsplit(base)
            valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                     and not parsed.username and not parsed.password and not parsed.query
                     and not parsed.fragment and parsed.path in ("", "/") and parsed.port)
        except ValueError:
            valid = False
        if not valid:
            raise BridgeError("面板地址必须是http://127.0.0.1:端口，不允许代理或跳转")
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def open(self, path, method="GET", timeout=12):
        request = urllib.request.Request(self.base + path, method=method)
        return self.opener.open(request, timeout=timeout)

    def json(self, path, method="GET"):
        with self.open(path, method) as response:
            raw = response.read(MAX_HTTP + 1)
        if len(raw) > MAX_HTTP:
            raise BridgeError("面板响应超过处理范围，未继续操作")
        try:
            return feedback.load_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise BridgeError("面板返回的结构化数据不可读") from exc


def preflight(panel, tool_root):
    tool_root = tool_root.resolve(strict=True)
    if not tool_root.is_dir():
        raise BridgeError("工具根目录不存在")
    try:
        (tool_root / ".mediaexport-maintenance").lstat()
    except FileNotFoundError:
        pass
    else:
        raise BridgeError("现有工具处于维护状态，未发起导出")
    local_html = (tool_root / "panel.html").read_text(encoding="utf-8").replace("\r\n", "\n")
    with panel.open("/") as response:
        raw = response.read(MAX_EVENT + 1)
    if len(raw) > MAX_EVENT or raw.decode("utf-8").replace("\r\n", "\n") != local_html:
        raise BridgeError("面板页面与指定工具副本不匹配，未发起导出")
    env = panel.json("/api/env")
    if (not isinstance(env, dict) or not isinstance(env.get("python"), str)
            or not env["python"] or env.get("playwright") is not True
            or env.get("chrome") is not True or env.get("output") is not True):
        raise BridgeError("现有面板环境未就绪；需先处理Python、浏览器或依赖，未自动安装")
    tasks = panel.json("/api/tasks")
    if not isinstance(tasks, dict) or any(not isinstance(t, dict) or t.get("done") is not True for t in tasks.values()):
        raise BridgeError("面板有运行中任务或无法确认空闲，未发起重复导出")
    return {"panel": panel.base, "tool_root": str(tool_root), "panel_matches": True,
            "python": env["python"], "playwright": True, "chrome": True,
            "active_tasks": 0, "login": "由原导出器访问真实后台检查，未把profile存在当成已登录"}


def stream_result(panel, task_id, platform, seconds):
    start, last_notice = time.monotonic(), time.monotonic()
    packets = []
    with panel.open("/api/stream?" + urllib.parse.urlencode({"task_id": task_id})) as response:
        while True:
            if time.monotonic() - start > seconds:
                raise BridgeError("等待超过本轮上限；面板任务可能仍在运行，不自动重发或取消其他任务")
            raw = response.readline(MAX_EVENT + 1)
            if not raw:
                raise BridgeError("结果连接提前结束；任务状态未确认，不自动重复导出")
            if len(raw) > MAX_EVENT:
                raise BridgeError("单条进度事件超出1MiB，未继续读取；请保留本轮面板任务")
            if time.monotonic() - last_notice >= 15:
                print(f"{feedback.PLATFORMS[platform]}仍在运行，已等待{int(time.monotonic()-start)}秒", flush=True)
                last_notice = time.monotonic()
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = feedback.load_json(line[6:])
            if not isinstance(event, dict):
                raise BridgeError("面板事件格式错误")
            if event.get("type") == "line":
                text = event.get("text")
                # P1-05: forward worker [NOTIFY] markers (e.g. QR login) to the
                # session so the agent can message the user immediately; other
                # plain log lines stay unforwarded.
                if isinstance(text, str) and text.startswith("[NOTIFY]"):
                    print(text, flush=True)
                if isinstance(text, str) and text.startswith(feedback.MARKER):
                    packet = feedback.load_json(text[len(feedback.MARKER):])
                    if not isinstance(packet, dict):
                        raise BridgeError("平台结果包格式错误")
                    if packet.get("manifest"):
                        if packet.get("platform") != platform or type(packet.get("schema_version")) is not int or packet["schema_version"] != 1:
                            raise BridgeError("最终结果包与本轮平台或版本不一致")
                        if packet not in packets:
                            packets.append(packet)
            if event.get("type") == "done":
                if len(packets) != 1 or type(event.get("success")) is not bool:
                    raise BridgeError("未收到唯一最终平台结果及有效结束状态，不确认导出完成")
                return packets[0], event["success"]


def failed_result(platform, message, kind):
    return {"schema_version": 1, "adapter_version": feedback.VERSION,
            "expected_platforms": [platform], "sources": {}, "state": "needs_attention",
            "summary": {"ok": 0, "no_data": 0, "fail": 1, "fallback": 0, "physical_files": 0},
            "downloaded": [], "no_data": [], "failed": [feedback.failure(platform, "本轮运行结果", message, kind)]}


def archive_result(packet, success, platform, tool_root, run):
    if not isinstance(packet.get("manifest"), str):
        raise BridgeError("最终manifest路径无效")
    manifest = Path(packet["manifest"]).resolve()
    output = (tool_root / "output").resolve(strict=True)
    source_root = manifest.parent
    if not manifest.is_relative_to(output) or source_root == output or not source_root.name.startswith(platform + "_"):
        raise BridgeError("结果清单不在指定工具的本轮输出目录，未读取")
    marker = source_root / ".run.json"
    if marker.resolve().parent != source_root:
        raise BridgeError("运行标记路径越界，未读取")
    with marker.open("rb") as stream:
        marker_raw = stream.read(MAX_HTTP + 1)
    if len(marker_raw) > MAX_HTTP:
        raise BridgeError("本轮运行标记异常")
    context = feedback.load_json(marker_raw.decode("utf-8"))
    if (not isinstance(context, dict) or context.get("platform") != platform
            or context.get("run_id") != source_root.name
            or not isinstance(context.get("output_dir"), str)
            or Path(context["output_dir"]).resolve() != source_root):
        raise BridgeError("输出目录与本轮平台运行标记不一致")
    items, _ = feedback.read_source(manifest, platform)
    if items != packet.get("items"):
        raise BridgeError("流式结果与本轮持久化清单不一致")
    result = feedback.summarize([platform], {platform: manifest}, source_root)
    reports = run / "reports"
    archive_ready = True
    try:
        reports.mkdir(exist_ok=False)
    except OSError:
        archive_ready = False
    copied, downloaded = {}, []
    for index, row in enumerate(result["downloaded"], 1):
        source = Path(row["file"])
        destination = None
        try:
            if not archive_ready:
                raise BridgeError("归档目录未能创建")
            if source not in copied:
                suffix = source.suffix if source.suffix.lower() in {".csv", ".xlsx", ".txt", ".json"} else ".bin"
                destination = reports / f"report_{index:02d}{suffix}"
                with source.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                if feedback.sha256(destination) != row["sha256"]:
                    raise BridgeError("归档副本指纹不一致")
                copied[source] = destination
            downloaded.append(dict(row, source_file=str(source), file=str(copied[source])))
        except (OSError, ValueError) as exc:
            reason = "本地归档目录未能创建" if not archive_ready else "本地归档未完成"
            detail = feedback.safe_text(f"{type(exc).__name__}: {exc}")
            failed = feedback.failure(platform, row["task"], feedback.safe_text(f"{reason}（{detail}）；原报表保留：{source}"), "archive_failed", row["source_status"], row["period"])
            failed["source_file"] = str(source)
            failed["attempted_archive_file"] = str(destination) if destination else None
            failed["partial_file"] = str(destination) if destination and destination.exists() else None
            failed["partial_file_verified"] = False
            result["failed"].append(failed)
    result["downloaded"] = downloaded
    if not success and not result["failed"]:
        result["failed"].append(feedback.failure(platform, "导出器结束状态", "面板进程结束标记失败，已保留收到的项目结果", "process_failed"))
    result["summary"] = {"ok": len(downloaded), "no_data": len(result["no_data"]), "fail": len(result["failed"]),
                         "fallback": sum(r["status"] == "OK(兜底)" for r in downloaded),
                         "physical_files": len({r["file"] for r in downloaded})}
    result["state"] = "completed" if not result["failed"] else "needs_attention"
    result["archived_output_root"] = str(reports)
    return result


def save_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def execute(panel, platform, tool_root, save_to, account_alias, seconds, check):
    run = save_to / (datetime.now().strftime("operation_%Y%m%d_%H%M%S_") + uuid.uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    save_json(run / "start_intent.json", {"platform": platform, "account_alias": account_alias,
              "preflight": check, "execute_requested": True})
    task_id = None
    phase = "启动回执"
    try:
        # Exactly one POST. A lost acknowledgement must never trigger a retry.
        reply = panel.json("/api/run/" + platform, "POST")
        if not isinstance(reply, dict) or not isinstance(reply.get("task_id"), str) or not reply["task_id"]:
            raise BridgeError("未获得本轮任务编号；可能已启动，不自动重试")
        task_id = reply["task_id"]
        save_json(run / "task.json", {"platform": platform, "task_id": task_id, "panel": panel.base})
        print(f"已提交{feedback.PLATFORMS[platform]}任务；原工具将先检查真实后台登录，再执行导出", flush=True)
        phase = "结果接收"
        packet, success = stream_result(panel, task_id, platform, seconds)
        phase = "结果核对或归档"
        result = archive_result(packet, success, platform, tool_root, run)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            message = f"面板返回HTTP {exc.code}（{phase}）；启动状态未确认，不自动重试，请核对面板任务"
        elif isinstance(exc, BridgeError):
            message = str(exc)
        else:
            detail = feedback.safe_text(f"{type(exc).__name__}: {exc}")
            message = f"{phase}未完成（{detail}）"
            if phase in {"启动回执", "结果接收"}:
                message += "；任务可能仍在运行，不自动重发或取消"
        result = failed_result(platform, message, "bridge_unconfirmed")
    result["execution"] = {"platform": platform, "task_id": task_id, "account_alias": account_alias,
                           "panel": panel.base, "post_attempts": 1, "login_checked_by": "existing_exporter"}
    destination = feedback.write_feedback(result, run)
    print(f"已下载{result['summary']['ok']}项；平台无数据跳过{result['summary']['no_data']}项；执行或核对问题{result['summary']['fail']}项", flush=True)
    print(f"反馈已保存：{destination}", flush=True)
    return int(result["summary"]["fail"] > 0)


def main(argv=None):
    if sys.version_info < (3, 9):
        print("需要Python3.9或更高版本", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="单平台本机导出闭环；默认只预检，--execute才发起真实任务")
    parser.add_argument("--platform", choices=feedback.PLATFORMS, required=True)
    parser.add_argument("--tool-root", type=Path, required=True)
    parser.add_argument("--save-to", type=Path, required=True)
    parser.add_argument("--account-alias", required=True)
    parser.add_argument("--panel", default="http://127.0.0.1:8766")
    parser.add_argument("--seconds", type=int, default=480)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not 30 <= args.seconds <= 1200 or not args.account_alias.strip():
        parser.error("等待范围30至1200秒，账号代号不能为空")
    try:
        panel = Panel(args.panel)
        tool_root = args.tool_root.resolve(strict=True)
        check = preflight(panel, tool_root)
        if not args.execute:
            print(json.dumps(dict(check, platform=args.platform, execute=False), ensure_ascii=False))
            return 0
        return execute(panel, args.platform, tool_root, args.save_to.resolve(), feedback.safe_text(args.account_alias, 200), args.seconds, check)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        detail = str(exc) if isinstance(exc, BridgeError) else "请核对现有面板、工具目录及保存位置"
        print("预检或反馈保存失败：" + detail + "；未自动安装、重试或取消其他任务。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
