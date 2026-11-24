# -*- coding: utf-8 -*-
"""
视频号 - 一键全量数据导出
自动连接 Chrome CDP，顺序执行全部导出任务
"""

# Load this exact local file without changing module search precedence.
import os, sys
import importlib.util as _maintenance_importlib
_MAINTENANCE_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_maintenance_spec = _maintenance_importlib.spec_from_file_location(
    "mediaexport_maintenance", os.path.join(_MAINTENANCE_MODULE_DIR, "mediaexport_maintenance.py"))
_maintenance_module = _maintenance_importlib.module_from_spec(_maintenance_spec)
_maintenance_spec.loader.exec_module(_maintenance_module)
check_start = _maintenance_module.check_start
require_available = _maintenance_module.require_available
MaintenanceBlocked = _maintenance_module.MaintenanceBlocked
check_start()

import asyncio, os, sys, subprocess, datetime, time, json, urllib.request
from playwright.async_api import async_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(HERE, "wechat")
OUTPUT_DIR = os.environ.get(
    "MEDIAEXPORT_OUTPUT_DIR",
    os.path.join(HERE, "..", "output")
)
BACKUP_DIR = os.path.join(HERE, "..", "backup")
for _p in (HERE, os.path.dirname(HERE)):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from tool_utils import find_chrome, get_chrome_port, get_output_dir, get_downloads_dir
from run_paths import start_run
from report_selection import environment_selection, choose
REPORT_SELECTION_PROTOCOL = 'PAY-S3-10-candidate1'
from adaptive import validate_file_contract, write_manifest
from export_results import NO_DATA_EXIT_CODE, RESULT_PREFIX, parse_result_line, summarize_items, emit_result
OUTPUT_BASE = get_output_dir()
DOWNLOADS_BASE = get_downloads_dir()
TASK_OUTPUT_DIRS = {}
TASK_NO_DATA = {}

CHROME_PATH = find_chrome() or r'C:\Program Files\Google\Chrome\Application\chrome.exe'

CHROME_PORT = get_chrome_port()



TASKS = [
    ("全部视频",    "run_video_all_export.py"),
    ("单篇视频",    "run_single_video_export_fresh.py"),
    ("关注者数据",  "run_follower_export.py"),
    ("来源分布",    "run_source_dist_export.py"),
    ("粉丝画像",    "run_fans_portrait_export.py"),
]

TASK_CONTRACTS = {
    "全部视频":   ("wechat_video_all_{date}.csv", "wechat_video_all"),
    "单篇视频":   ("wechat_video_single_{date}.csv", "wechat_video_single"),
    "关注者数据": ("wechat_follower_{date}.csv", "wechat_follower"),
    "来源分布":   ("wechat_source_dist_{date}.txt", "wechat_source_dist"),
    "粉丝画像":   ("wechat_fans_portrait_{date}.txt", "wechat_fans_portrait"),
}


def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"[{ts}] {msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace')}", flush=True)


def log_step(step, msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{ts}] [{step}] {msg}", flush=True)
    except UnicodeEncodeError:
        safe = msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
        print(f"[{ts}] [{step}] {safe}", flush=True)


def _channels_pages():
    """返回 9222/配置端口上属于视频号后台的 page（用于识别是不是我们自己的实例）"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json", timeout=3) as r:
            return [p for p in json.loads(r.read())
                    if p.get("type") == "page" and "channels.weixin.qq.com" in p.get("url", "")]
    except Exception:
        return []


def is_chrome_ready():
    """仅当端口上存在视频号后台页面时，才认为是我们自己的 Chrome 实例"""
    if not _channels_pages():
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json/version", timeout=3) as r:
            return json.loads(r.read()).get("webSocketDebuggerUrl")
    except Exception:
        return None


def bring_front():
    """需要扫码时把视频号 Chrome 窗口带到前台（配合 --start-minimized）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json/version", timeout=3) as r:
            ws = json.loads(r.read()).get("webSocketDebuggerUrl")
    except Exception:
        return
    if not ws:
        return

    async def _do():
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(ws)
                pages = browser.contexts[0].pages if browser.contexts else []
                if pages:
                    await pages[0].bring_to_front()
                await browser.close()
        except Exception:
            pass

    try:
        asyncio.run(_do())
    except Exception:
        pass


def start_chrome():
    """Launch Chrome with remote debugging port via subprocess, then wait for CDP ready"""
    log("检查 Chrome 调试端口...")
    ws = is_chrome_ready()
    if ws:
        log("Chrome 调试端口已就绪 - 复用现有实例")
        return ws

    # 端口已有响应、但不是我们的视频号 Chrome：拒绝复用/关闭，避免误操作陛下浏览器
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json/version", timeout=2) as r:
            r.read()
        log(f"[ERROR] 端口 {CHROME_PORT} 被其他程序占用，且不是本工具的视频号 Chrome，已停止")
        return None
    except Exception:
        pass

    log("Chrome 未就绪，尝试启动...")
    profile_dir = os.path.join(os.path.dirname(HERE), "chrome_profile", "WechatAutomation")
    os.makedirs(profile_dir, exist_ok=True)

    cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={CHROME_PORT}",
        f"--user-data-dir={profile_dir}",
        "--disable-blink-features=AutomationControlled",
        "--start-minimized",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=DownloadBubble,DownloadBubbleV2",
        "--disable-download-notification",
        "https://channels.weixin.qq.com/micro/statistic",
    ]
    try:
        subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        log("Chrome 已启动（调试端口 9222）")
    except Exception as exc:
        log(f"[ERROR] Chrome 启动失败: {exc}")
        return None

    # 等待 CDP 端口就绪
    log("等待 Chrome CDP 端口就绪...")
    for i in range(30):
        time.sleep(1)
        ws = is_chrome_ready()
        if ws:
            log("CDP 端口已就绪")
            break
        if i % 5 == 4:
            log(f"  等待 CDP... ({i+1}/30秒)")
    else:
        log("[ERROR] CDP 端口启动超时（30秒）")
        return None

    # 等待登录完成
    log("【需要扫码】视频号登录可能已过期，请在弹出的浏览器窗口中扫码")
    bring_front()
    log("等待登录检测...")
    for i in range(180):
        time.sleep(1)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json", timeout=2) as r2:
                pages = json.loads(r2.read())
            in_platform = any("channels.weixin.qq.com/platform" in p2.get("url", "") for p2 in pages)
            if in_platform:
                log("登录检测成功")
                ws2 = is_chrome_ready()
                if ws2:
                    return ws2
        except Exception:
            pass
        if i % 15 == 0 and i > 0:
            log(f"  等待登录... ({i}s)")

    log("[ERROR] 登录超时（3分钟）")
    return None
    return None
def backup_existing():
    """New runs use a fresh directory; historical files stay in place."""
    log("本次使用独立输出目录，历史文件原地保留")


def run_task(name, script_rel):
    """run single export script (capture mode - reliable across pipe chains)"""
    script_path = os.path.join(SCRIPTS_DIR, script_rel)
    if not os.path.exists(script_path):
        log(f"[SKIP] script not found: {script_rel}")
        return False
    TASK_NO_DATA.pop(script_rel, None)
    log(f"-> start: {name}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    task_paths = start_run("wechat", OUTPUT_DIR, os.environ["MEDIAEXPORT_DOWNLOAD_DIR"])
    TASK_OUTPUT_DIRS.setdefault(script_rel, []).append(task_paths.output_dir)
    env.update(task_paths.env())
    env["MEDIAEXPORT_PARENT_RUN"] = task_paths.run_id
    result = subprocess.run(
        [sys.executable, "-u", script_path],
        cwd=SCRIPTS_DIR, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.stdout:
        canonical_name = next((label for label, script in TASKS if script == script_rel), name)
        for line in result.stdout.splitlines():
            packet = parse_result_line(line)
            skipped = next((item for item in (packet or {}).get('items', [])
                            if item['task'] == canonical_name and item['status'] == 'NO_DATA'), None)
            if result.returncode == NO_DATA_EXIT_CODE and skipped and packet['platform'] == 'wechat' and len(packet['items']) == 1:
                TASK_NO_DATA[script_rel] = skipped
            elif not line.startswith(RESULT_PREFIX):
                sys.stdout.write(line+'\n')
        sys.stdout.flush()
    no_data = result.returncode == NO_DATA_EXIT_CODE and script_rel in TASK_NO_DATA
    ok = result.returncode == 0 or no_data
    if ok:
        log(f"  [NO_DATA] {name}：{TASK_NO_DATA[script_rel]['reason']}，已跳过下载" if no_data else f"  [OK] {name} done")
    else:
        log(f"  [FAIL] {name} failed (exit: {result.returncode})")
    return ok
def _close_all_pages():
    """Close all Chrome CDP pages"""
    try:
        targets = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json", timeout=3).read())
        for t in targets:
            if t.get("type") == "page":
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{CHROME_PORT}/json/close/{t['id']}", timeout=3)
                except:
                    pass
    except:
        pass

def main():
    selected_reports = environment_selection("wechat")
    try:
        _main_impl(selected_reports)
    finally:
        log("Closing browser...")
        _close_all_pages()
        log("Browser closed")

def _main_impl(selected_reports=None):
    global OUTPUT_DIR
    tasks = choose(TASKS, selected_reports)
    paths = start_run("wechat", OUTPUT_BASE, DOWNLOADS_BASE)
    TASK_OUTPUT_DIRS.clear()
    TASK_NO_DATA.clear()
    OUTPUT_DIR = paths.output_dir
    os.environ.update(paths.env())
    log("=" * 50)
    log("视频号数据一键导出")
    log("=" * 50)
    log(f"输出目录: {OUTPUT_DIR}")
    log(f"备份目录: {BACKUP_DIR}")
    log(f"共 {len(tasks)} 个导出任务：")
    for idx, (name, _) in enumerate(tasks):
        log(f"  [{idx+1}/{len(tasks)}] {name}")

    # 1. 确保 Chrome 就绪
    log_step("准备", "检查 Chrome CDP 连接...")
    ws = is_chrome_ready()
    if not ws:
        log_step("准备", "Chrome 未就绪，尝试启动...")
        ws = start_chrome()
        if not ws:
            log("[ERROR] Chrome 启动失败，请手动检查")
            failed = [{'task':'浏览器准备','status':'FAIL','file':None,'error':'Chrome连接或启动失败'}]
            manifest = write_manifest(OUTPUT_DIR,'wechat',failed,summarize_items(failed))
            emit_result('wechat',failed,manifest,log=log)
            sys.exit(1)
    else:
        log_step("准备", "✓ Chrome CDP 已连接")

    # 2. 备份现有输出
    log_step("备份", "备份现有导出文件...")
    backup_existing()

    # 3. 执行所有任务
    results = {}
    manifest_items = []
    date_tag = datetime.date.today().strftime("%Y%m%d")
    for idx, (name, script_rel) in enumerate(tasks):
        log(f"\n{'─' * 40}")
        log_step(f"{idx+1}/{len(tasks)}", f"执行: {name}")
        # 上一个任务可能把浏览器搞崩了 —— 崩了就先重启再跑
        if not is_chrome_ready():
            log("  ⚠ Chrome 调试端口已断开（浏览器崩溃），重新启动浏览器...")
            if not start_chrome():
                log("  ✗ 浏览器重启失败，跳过该任务")
                results[name] = False
                manifest_items.append({"task": name, "file": None, "status": "FAIL", "strategy": [], "checks": {}, "rows": None, "period": None, "error": "浏览器重启失败"})
                continue
        ok = run_task(name, script_rel)
        # 任务失败且浏览器已经没了 → 重启后重试一次（本机 Chrome 下载崩溃是已知问题）
        if not ok and not is_chrome_ready():
            log(f"  ⚠ 「{name}」失败且浏览器已崩溃，重启浏览器后重试一次...")
            if start_chrome():
                ok = run_task(f"{name}（重试）", script_rel)
        results[name] = ok
        item = {"task": name, "file": None, "status": "OK" if ok else "FAIL", "strategy": [], "checks": {}, "rows": None, "period": None, "error": None}
        if ok and script_rel in TASK_NO_DATA:
            item.update(TASK_NO_DATA[script_rel])
        elif ok:
            fname, ckey = TASK_CONTRACTS[name]
            fname = fname.format(date=date_tag)
            fpath = os.path.join(TASK_OUTPUT_DIRS[script_rel][-1], fname)
            item["file"] = os.path.relpath(fpath, OUTPUT_DIR)
            if os.path.exists(fpath):
                contract = validate_file_contract(fpath, ckey)
                item["checks"] = {"contract": bool(contract.get("ok"))}
                if contract.get("ok"):
                    log(f"  ✓ 契约校验通过 {name}")
                else:
                    item["status"] = "FAIL"
                    item["error"] = contract.get("reason")
                    results[name] = False
                    log(f"  ✗ 契约校验未通过 {name}: {contract.get('reason')}")
            else:
                item["status"] = "FAIL"
                item["error"] = f"产物缺失: {fname}"
                results[name] = False
                log(f"  ✗ 产物缺失 {name}: {fname}")
        if not ok:
            item["error"] = "子任务未成功完成，本次下载和阶段产物已保留"
        if script_rel in TASK_OUTPUT_DIRS:
            item["attempt_dirs"] = [os.path.relpath(directory, OUTPUT_DIR)
                                    for directory in TASK_OUTPUT_DIRS[script_rel]]
            item["attempt_dir"] = item["attempt_dirs"][-1]
        manifest_items.append(item)

    # 4. 汇总
    log(f"\n{'=' * 50}")
    log("导出结果汇总")
    log("=" * 50)
    counts = summarize_items(manifest_items)
    ok_count, fail_count = counts['ok'], counts['fail']
    manifest_path = write_manifest(OUTPUT_DIR, "wechat", manifest_items, counts)
    emit_result('wechat', manifest_items, manifest_path, log=log)
    log("  → manifest 已保存")
    log(f"输出目录: {OUTPUT_DIR}")

    if fail_count == 0:
        log("视频号运行完成（无数据项已正常跳过）" if counts['no_data'] else "🎉 视频号全部数据导出成功！")
    elif ok_count > 0:
        log(f"⚠ 视频号部分导出成功（{fail_count}项执行失败）")
    else:
        log("✗ 视频号全部导出失败")

    log("=" * 50)

    if fail_count:
        sys.exit(1)

    # 打开输出目录

    os.startfile(OUTPUT_DIR)

    os.startfile(OUTPUT_DIR)


if __name__ == "__main__":
    main()
