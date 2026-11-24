# -*- coding: utf-8 -*-
"""
MediaExport - 自媒体数据导出控制面板
支持抖音 / 视频号 / 小红书 三平台一键导出
"""

# Load this exact local file without changing module search precedence.
import os, sys
import importlib.util as _maintenance_importlib
_MAINTENANCE_MODULE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
_maintenance_spec = _maintenance_importlib.spec_from_file_location(
    "mediaexport_maintenance", os.path.join(_MAINTENANCE_MODULE_DIR, "mediaexport_maintenance.py"))
_maintenance_module = _maintenance_importlib.module_from_spec(_maintenance_spec)
_maintenance_spec.loader.exec_module(_maintenance_module)
check_start = _maintenance_module.check_start
require_available = _maintenance_module.require_available
MaintenanceBlocked = _maintenance_module.MaintenanceBlocked
check_start()
_selection_spec = _maintenance_importlib.spec_from_file_location(
    "report_selection", os.path.join(_MAINTENANCE_MODULE_DIR, "report_selection.py"))
_selection_module = _maintenance_importlib.module_from_spec(_selection_spec)
_selection_spec.loader.exec_module(_selection_module)

import json, os, sys, subprocess, threading, queue, time, uuid, webbrowser, signal
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

# ── 路径配置 ──
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, "scripts")
PANEL_HTML = os.path.join(HERE, "panel.html")
OUTPUT_DIR = os.path.join(HERE, "output")
LOGS_DIR = os.path.join(HERE, "logs")
PORT = 8766

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(SCRIPTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

TASKS = {}  # task_id -> {queue, process, done, success}
TASK_LOCK = threading.Lock()

LOG_LOCK = threading.Lock()
LOG_FILE = os.path.join(LOGS_DIR, f"export_{time.strftime('%Y%m%d_%H%M%S')}.log")
_RUN_CTX = threading.local()  # 每个运行线程自己的按次日志路径


def write_log(text):
    """把面板日志同步写入本地文件，便于事后排查。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {text}\n")
            run_log = getattr(_RUN_CTX, "run_log_file", None)
            if run_log:
                with open(run_log, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] {text}\n")
    except Exception:
        pass


def open_browser(url):
    """自动打开浏览器（优先使用 Edge）"""
    edge_paths = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in edge_paths:
        if os.path.exists(p):
            subprocess.Popen(
                [p, "--new-tab", url],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            return
    # 回退到默认浏览器
    try:
        webbrowser.open(url)
    except Exception:
        os.startfile(url)


# ── 环境检测 ──
def check_env():
    results = {}
    try:
        r = subprocess.run([sys.executable, "--version"], capture_output=True, text=True, timeout=5)
        results["python"] = r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        results["python"] = None

    try:
        r = subprocess.run([sys.executable, "-c", "import playwright"], capture_output=True, timeout=5)
        results["playwright"] = r.returncode == 0
    except Exception:
        results["playwright"] = False

    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        chrome_candidates.append(os.path.join(local, r"Google\Chrome\Application\chrome.exe"))
    results["chrome"] = any(os.path.exists(p) for p in chrome_candidates)
    results["output"] = os.path.exists(OUTPUT_DIR)

    # 检查各平台 Profile（实际在工具目录 chrome_profile/ 下，不在系统 Chrome 用户目录）
    profile_dir = os.path.join(HERE, "chrome_profile")
    results["douyin_profile"] = os.path.exists(os.path.join(profile_dir, "DouyinAutomation"))
    results["xhs_profile"] = os.path.exists(os.path.join(profile_dir, "XiaohongshuAutomation"))
    results["wechat_profile"] = os.path.exists(os.path.join(profile_dir, "WechatAutomation"))
    return results


# ── 脚本执行器 ──
def task_cancelled(task_id):
    info = TASKS[task_id]
    parent = TASKS.get(info.get("parent_task_id"), {})
    return bool(info.get("cancel") or parent.get("cancel"))


def run_script(task_id, script_rel, label, is_subtask=False, selected_reports=None):
    """在子进程中运行一个脚本，输出通过队列发往 SSE"""
    prev_run_log = getattr(_RUN_CTX, "run_log_file", None)
    if not is_subtask:
        _RUN_CTX.run_log_file = os.path.join(LOGS_DIR, f"run_{time.strftime('%Y%m%d_%H%M%S')}_{task_id}.log")

    q = TASKS[task_id]["queue"]
    script_path = os.path.join(SCRIPTS_DIR, script_rel)

    if not os.path.exists(script_path):
        text = f"[ERROR] 脚本不存在: {script_path}"
        q.put({"type": "line", "text": text})
        write_log(text)
        TASKS[task_id].update({"done": True, "success": False})
        if not is_subtask:
            q.put({"type": "done", "success": False})
            _RUN_CTX.run_log_file = prev_run_log
        return

    env = os.environ.copy()
    env.pop(_selection_module.ENV_KEY, None)
    if selected_reports is not None:
        platform = script_rel.removesuffix('_export_all.py')
        env[_selection_module.ENV_KEY] = json.dumps(_selection_module.payload(platform, selected_reports))
    env["PYTHONIOENCODING"] = "utf-8"
    # 注入输出目录环境变量，供子脚本读取
    env["MEDIAEXPORT_OUTPUT_DIR"] = OUTPUT_DIR

    try:
        with TASK_LOCK:
            if task_cancelled(task_id):
                TASKS[task_id].update({"done": True, "success": False, "cancel": True})
                q.put({"type": "line", "text": "[INFO] 任务已取消，未启动脚本"})
                if not is_subtask:
                    q.put({"type": "done", "success": False})
                return
            require_available()
            proc = subprocess.Popen(
                [sys.executable, "-u", script_path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=SCRIPTS_DIR, env=env,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            TASKS[task_id]["process"] = proc
        q.put({"type": "line", "text": f"开始: {label}"})
        write_log(f"开始: {label}")

        for line in iter(proc.stdout.readline, ""):
            text = line.rstrip()
            q.put({"type": "line", "text": text})
            write_log(text)
            # 检查是否被取消
            if task_cancelled(task_id):
                proc.kill()
                break

        proc.wait()
        with TASK_LOCK:
            cancelled = task_cancelled(task_id)
            ok = proc.returncode == 0 and not cancelled
            TASKS[task_id].update({"done": True, "success": ok})
        if cancelled:
            text = "[INFO] 任务已取消"
            q.put({"type": "line", "text": text})
            write_log(text)
            if not is_subtask:
                q.put({"type": "done", "success": False})
        else:
            text = f"{'[OK]' if ok else '[FAIL]'} {label}"
            q.put({"type": "status", "text": text})
            write_log(text)
            if not is_subtask:
                q.put({"type": "done", "success": ok})
                _RUN_CTX.run_log_file = prev_run_log
    except Exception as e:
        TASKS[task_id].update({"done": True, "success": False})
        text = f"[{'MAINTENANCE' if isinstance(e, MaintenanceBlocked) else 'ERROR'}] {e}"
        q.put({"type": "status", "text": text})
        write_log(text)
        if not is_subtask:
            q.put({"type": "done", "success": False})
            _RUN_CTX.run_log_file = prev_run_log
    finally:
        if not is_subtask:
            _RUN_CTX.run_log_file = prev_run_log


def run_sequence(task_id, steps):
    """顺序执行多个子任务 (v2 enhanced logging)"""
    prev_run_log = getattr(_RUN_CTX, "run_log_file", None)
    _RUN_CTX.run_log_file = os.path.join(LOGS_DIR, f"run_{time.strftime('%Y%m%d_%H%M%S')}_{task_id}.log")

    q = TASKS[task_id]["queue"]
    all_ok = True
    total = len(steps)
    q.put({"type": "line", "text": f"[SEQ] run_sequence: {total} steps"})
    write_log(f"[SEQ] run_sequence: {total} steps")
    for idx, (script_rel, label) in enumerate(steps, 1):
        with TASK_LOCK:
            if task_cancelled(task_id):
                all_ok = False
                q.put({"type": "line", "text": "[INFO] 任务已取消，未启动剩余平台"})
                break
            subtask_id = f"{task_id}_{idx}"
            TASKS[subtask_id] = {"queue": q, "process": None, "done": False,
                                 "success": False, "cancel": False, "parent_task_id": task_id}
        task_label = f"[{idx}/{total}] {label}"
        q.put({"type": "line", "text": f""})
        q.put({"type": "line", "text": f"--- {task_label} ---"})
        write_log(f"--- {task_label} ---")
        try:
            run_script(subtask_id, script_rel, task_label, is_subtask=True)
        except Exception as exc:
            text = f"[ERROR] {label} exception: {exc}"
            q.put({"type": "line", "text": text})
            write_log(text)
            TASKS[subtask_id].update({"done": True, "success": False})
        if not TASKS[subtask_id].get("success"):
            all_ok = False
            q.put({"type": "line", "text": f"[WARN] {label} failed, continue"})
            write_log(f"[WARN] {label} failed, continue")
        else:
            q.put({"type": "line", "text": f"[OK] {label} done"})
            write_log(f"[OK] {label} done")
    with TASK_LOCK:
        all_ok = all_ok and not task_cancelled(task_id)
        TASKS[task_id].update({"done": True, "success": all_ok})
    q.put({"type": "done", "success": all_ok})
    _RUN_CTX.run_log_file = prev_run_log


# ── HTTP Handler ──
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默日志，避免干扰输出

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, task_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = TASKS.get(task_id, {}).get("queue")
        if not q:
            self.wfile.write(f"data: {json.dumps({'type': 'done', 'success': False})}\n\n".encode())
            return

        while True:
            try:
                msg = q.get(timeout=2)
            except queue.Empty:
                self.wfile.write(b": keepalive\n\n")
                continue
            self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            if msg.get("type") == "done":
                break

    def _read_html(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            html = self._read_html(PANEL_HTML)
            if html:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            else:
                self._send_json({"error": "panel.html not found"}, 500)
            return

        if path == "/api/report-capabilities":
            if not _selection_module.supports_workers(SCRIPTS_DIR, _selection_module.REPORTS):
                self._send_json({"error": "report_selection_workers_not_ready"}, 503)
                return
            self._send_json(_selection_module.catalogue())
            return

        if path == "/api/env":
            self._send_json(check_env())
            return

        if path == "/api/tasks":
            with TASK_LOCK:
                active = {tid: {
                    "done": info.get("done", False),
                    "success": info.get("success", False),
                    "cancel": info.get("cancel", False),
                } for tid, info in TASKS.items()}
            self._send_json(active)
            return

        if path.startswith("/api/stream"):
            raw = self.path
            if "task_id=" in raw:
                tid = raw.split("task_id=")[-1].split("&")[0]
                if tid in TASKS:
                    self._send_sse(tid)
                    return
            self._send_json({"error": "invalid task_id"}, 400)
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/cancel":
            # 取消所有活跃任务
            processes = []
            with TASK_LOCK:
                for info in TASKS.values():
                    if not info.get("done"):
                        info["cancel"] = True
                        proc = info.get("process")
                        if proc and proc.poll() is None:
                            processes.append(proc)
            for proc in processes:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._send_json({"ok": True})
            return

        if path.startswith("/api/run-selected/"):
            platform = path.split("/")[-1]
            try:
                require_available()
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("report_selection_size_invalid")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise ValueError("report_selection_content_type_invalid")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("report_selection_body_incomplete")
                selected = _selection_module.parse_payload(body.decode("utf-8"), platform)
                if not _selection_module.supports_workers(SCRIPTS_DIR, [platform]):
                    raise ValueError("report_selection_worker_not_ready")
            except MaintenanceBlocked:
                self._send_json({"error": "maintenance_blocked"}, 503)
                return
            except (ValueError, UnicodeError):
                self._send_json({"error": "report_selection_invalid"}, 400)
                return
            with TASK_LOCK:
                if any(not info.get("done") for info in TASKS.values()):
                    self._send_json({"error": "panel_busy"}, 409)
                    return
                tid = str(uuid.uuid4())[:8]
                TASKS[tid] = {"queue": queue.Queue(), "process": None, "done": False, "success": False, "cancel": False}
            t = threading.Thread(target=run_script,
                args=(tid, platform + "_export_all.py", "所选报表导出"), kwargs={"selected_reports": selected}, daemon=True)
            t.start()
            self._send_json({"task_id": tid, "selected_reports": selected})
            return

        if path.startswith("/api/run/"):
            try:
                require_available()
            except MaintenanceBlocked as exc:
                write_log("[MAINTENANCE] blocked export request")
                self._send_json({"error": str(exc), "maintenance": True}, 503)
                return
            platform = path.split("/")[-1]
            tid = str(uuid.uuid4())[:8]
            with TASK_LOCK:
                TASKS[tid] = {"queue": queue.Queue(), "process": None, "done": False, "success": False, "cancel": False}

            PLATFORM_SCRIPTS = {
                "douyin": ("douyin_export_all.py", "抖音数据导出"),
                "wechat": ("wechat_export_all.py", "视频号数据导出"),
                "xhs": ("xhs_export_all.py", "小红书数据导出"),
            }

            if platform == "all":
                steps = [
                    ("douyin_export_all.py", "抖音数据导出"),
                    ("wechat_export_all.py", "视频号数据导出"),
                    ("xhs_export_all.py", "小红书数据导出"),
                ]
                t = threading.Thread(target=run_sequence, args=(tid, steps), daemon=True)
                t.start()
                self._send_json({"task_id": tid})
            elif platform in PLATFORM_SCRIPTS:
                script_rel, label = PLATFORM_SCRIPTS[platform]
                t = threading.Thread(target=run_script, args=(tid, script_rel, label), daemon=True)
                t.start()
                self._send_json({"task_id": tid})
            else:
                self._send_json({"error": f"unknown platform: {platform}"}, 400)
            return

        self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── 启动 ──
def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"  MediaExport Panel v2.0")
    print(f"  http://127.0.0.1:{PORT}")
    print(f"  Ctrl+C 退出")
    open_browser(f"http://127.0.0.1:{PORT}")

    # 优雅退出
    def shutdown(sig, frame):
        print("\n  正在停止服务...")
        for info in list(TASKS.values()):
            proc = info.get("process")
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown(None, None)


if __name__ == "__main__":
    main()
