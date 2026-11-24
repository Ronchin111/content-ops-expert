# -*- coding: utf-8 -*-
"""
Single Video Export - Fixed Version (2026-05-10)
Based on working export_final.py logic
"""

# Load this exact local file without changing module search precedence.
import os, sys
import importlib.util as _maintenance_importlib
_MAINTENANCE_MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_maintenance_spec = _maintenance_importlib.spec_from_file_location(
    "mediaexport_maintenance", os.path.join(_MAINTENANCE_MODULE_DIR, "mediaexport_maintenance.py"))
_maintenance_module = _maintenance_importlib.module_from_spec(_maintenance_spec)
_maintenance_spec.loader.exec_module(_maintenance_module)
check_start = _maintenance_module.check_start
require_available = _maintenance_module.require_available
MaintenanceBlocked = _maintenance_module.MaintenanceBlocked
check_start()

import asyncio
import os
import sys
import httpx
from datetime import date
from playwright.async_api import async_playwright
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(HERE, "..", "..", "downloads")
SAVE_DIR = os.environ.get("MEDIAEXPORT_OUTPUT_DIR", os.path.join(HERE, "..", "..", "output"))

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from dl_utils import artifact_dirs, snapshot_dirs, wait_for_new_file, verify_csv, load_wc_selectors, save_csv_with_header   # noqa: E402
from tool_utils import get_chrome_port, get_output_dir, get_downloads_dir
from run_paths import child_run
from adaptive import CONTRACTS
from export_results import NoDataAvailable, no_data_item, emit_result, NO_DATA_EXIT_CODE

CHROME_PORT = get_chrome_port()
SEL = load_wc_selectors()

def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def log_step(step, msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{step}] {msg}", flush=True)


def find_stat_page(context):
    """Find the WeChat Channels page"""
    for page in context.pages:
        if 'channels.weixin.qq.com' in page.url:
            return page
    return context.pages[-1] if context.pages else None


async def wait_for_csv(download_dir, timeout=20):
    """Wait for CSV file to appear in download directory"""
    existing = set(os.listdir(download_dir))
    for _ in range(timeout * 2):
        await asyncio.sleep(0.5)
        files = [f for f in os.listdir(download_dir) if f.endswith('.csv') and f not in existing]
        if files:
            return files[0]
    return None


def clean_downloads(download_dir):
    """Compatibility hook: preserve all prior downloads."""
    return 0


def save_csv(src_path, dst_name):
    """Keep the contract header and all data, regardless of column order."""
    return save_csv_with_header(src_path, os.path.join(SAVE_DIR, dst_name),
                                CONTRACTS["wechat_video_single"]["required_columns"])


async def click_menu(page, text):
    """Click main menu item"""
    await page.evaluate(f"""
        () => {{
            const items = document.querySelectorAll('{SEL["top_menu_selector"]}');
            for (const item of items) {{ if (item.innerText.includes('{text}')) {{ item.click(); return; }} }}
        }}
    """)
    await asyncio.sleep(0.8)


async def click_submenu(page, text):
    """Click submenu item"""
    await page.evaluate(f"""
        () => {{
            const items = document.querySelectorAll('{SEL["sub_menu_selector"]}');
            for (const item of items) {{ if (item.innerText.includes('{text}')) {{ item.click(); return; }} }}
        }}
    """)


async def check_fp(page, key):
    required = SEL.get("fingerprint", {}).get(key, [])
    missing = [t for t in required if not await page.get_by_text(t, exact=True).count()]
    if missing:
        log(f"  ⚠ 疑似页面改版（{key}），缺关键文本: {missing}")


async def export_video_single(page, DOWNLOAD_DIR, SAVE_DIR):
    """Export single video data - based on export_final.py logic"""
    log_step("导出", "正在查找数据 iframe...")
    clean_downloads(DOWNLOAD_DIR)

    # Find the statistic iframe
    for f in page.frames:
        if 'micro/statistic/post' in f.url:
            log_step("导出", "✓ 找到数据 iframe")

            # Step 1: Click "单篇视频" tab first!
            log_step("导出", "切换到「单篇视频」标签...")
            tab_result = await f.evaluate("""
                () => {
                    const navs = document.querySelectorAll('.weui-desktop-tab__nav a');
                    for (const nav of navs) {
                        if (nav.innerText.trim() === '单篇视频') {
                            nav.click();
                            return 'ok';
                        }
                    }
                    return 'not found';
                }
            """)
            if tab_result == "ok":
                log_step("导出", "✓ 已切换到「单篇视频」")
            else:
                log_step("导出", f"⚠ 切换标签结果: {tab_result}")
                return False
            await asyncio.sleep(2)

            # Step 2: Click "近30天" filter
            log_step("导出", "切换时间范围为「近30天」...")
            r = await f.evaluate("""
                () => {
                    const iter = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                    let node;
                    while (node = iter.nextNode()) {
                        if (node.textContent.trim() === "近30天") {
                            let label = node.parentElement;
                            while (label && label.tagName !== "LABEL") { label = label.parentElement; }
                            if (!label) continue;
                            const inputs = label.querySelectorAll('input[type=radio]');
                            for (const inp of inputs) {
                                if (inp.value === '2') { inp.click(); return "ok"; }
                            }
                        }
                    }
                    return "not found";
                }
            """)
            if r == "ok":
                log_step("导出", "✓ 近30天已切换")
            else:
                log_step("导出", f"⚠ 近30天切换结果: {r}")
                return False
            await asyncio.sleep(5)

            empty_period = False
            try:
                empty_period = await f.evaluate(
                    "() => document.body.innerText.includes('当前周期暂无视频')")
            except Exception:
                log("  未能核对空周期提示，继续按下载文件校验。")
            if empty_period is True:
                raise NoDataAvailable('近30天平台暂无视频', '近30天',
                                      '单篇视频页面显示“当前周期暂无视频”')

            # Click download button
            log_step("导出", "点击「下载表格」按钮...")
            dl_dirs = [os.path.abspath(DOWNLOAD_DIR)] + artifact_dirs()
            before = snapshot_dirs(dl_dirs)
            dl = await f.evaluate("""
                () => {
                    const iter = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                    let node;
                    while (node = iter.nextNode()) {
                        if (node.textContent.trim() === "下载表格") {
                            node.parentElement.click();
                            return 'ok';
                        }
                    }
                    return 'not found';
                }
            """)
            if dl == "ok":
                log_step("导出", "✓ 已点击下载，等待文件...")
            else:
                log_step("导出", f"✗ 下载按钮未找到: {dl}")
                return False

            csv_path, partial = await wait_for_new_file(dl_dirs, before, timeout=45, log=log)
            if csv_path and partial:
                log("  ⚠ 浏览器在下载期间崩溃了（本机 Chrome 已知问题），已收编中途文件")
            if csv_path:
                ok, rows_raw = verify_csv(csv_path)
                if not ok:
                    log_step("导出", f"✗ 文件不完整（{rows_raw} 行），下载原件已保留")
                    return False
                filename = "wechat_video_single_" + date.today().strftime('%Y%m%d') + ".csv"
                try:
                    rows, start, end = save_csv(csv_path, filename)
                except (ValueError, OSError) as exc:
                    log_step("导出", f"✗ 保存失败，下载原件保留: {exc}")
                    return False
                log_step("导出", "✓ 下载完成")
                log(f"  文件名: {filename}")
                log(f"  数据量: {rows}行")
                log(f"  时间范围: {start} -> {end}")
                log(f"  保存路径: {os.path.join(SAVE_DIR, filename)}")
                return True
            else:
                log_step("导出", "✗ 下载超时，未收到CSV")
                return False
            break
    log_step("导出", "✗ 未找到数据 iframe，导出失败")
    return False


async def main():
    log("=" * 50)
    log("视频号 - 单篇视频数据导出")
    log("=" * 50)
    log(f"输出目录: {SAVE_DIR}")

    # Connect to Chrome via CDP
    log_step("1/5", "连接 Chrome CDP...")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{CHROME_PORT}/json/version", timeout=5)
            ws_url = resp.json()["webSocketDebuggerUrl"]
            log_step("1/5", "✓ Chrome CDP 连接成功")
    except Exception as e:
        log(f"✗ CDP 连接失败: {e}")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(ws_url)
        context = browser.contexts[0]
        page = find_stat_page(context)

        if not page:
            log("✗ 未找到视频号后台页面")
            sys.exit(1)

        # 下载容错：让 Chrome 直接把文件落到 downloads/，浏览器崩了也能收编 .crdownload
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": os.path.abspath(DOWNLOAD_DIR),
                "eventsEnabled": False,
            })
            log(f"  下载目录: {os.path.abspath(DOWNLOAD_DIR)}")
        except Exception as e:
            log(f"  ⚠ 指定下载目录失败（改用默认）: {str(e)[:100]}")

        # Navigate
        log_step("2/5", "导航到 数据中心 → 视频数据 → 单篇视频...")
        await click_menu(page, SEL["menus"]["数据中心"])
        await asyncio.sleep(1)

        log_step("3/5", "点击「视频数据」子菜单...")
        await click_submenu(page, SEL["menus"]["视频数据"])
        await asyncio.sleep(2)

        log_step("4/5", "点击「单篇视频」子菜单...")
        await click_submenu(page, SEL["menus"]["单篇视频"])
        await asyncio.sleep(3)
        await check_fp(page, "video_single")

        # Export
        log_step("5/5", "执行单篇视频数据导出...")
        empty_result = None
        try:
            ok = await export_video_single(page, DOWNLOAD_DIR, SAVE_DIR)
        except NoDataAvailable as empty:
            empty_result = no_data_item('单篇视频', empty.reason, empty.period, empty.evidence)
            ok = True

        await browser.close()

    if empty_result:
        emit_result('wechat', [empty_result], log=log)
        sys.exit(NO_DATA_EXIT_CODE)
    log("=" * 50)
    log(f"{'✓ 单篇视频导出完成' if ok else '✗ 单篇视频导出失败'} | 保存到: {SAVE_DIR}")
    log("=" * 50)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    paths = child_run("wechat", get_output_dir(), get_downloads_dir())
    SAVE_DIR, DOWNLOAD_DIR = paths.output_dir, paths.download_dir
    os.environ.update(paths.env())
    asyncio.run(main())
