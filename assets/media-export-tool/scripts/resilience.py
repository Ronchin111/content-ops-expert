# -*- coding: utf-8 -*-
"""下载崩溃容错公共层：浏览器会话 + 落盘收编 + xlsx 校验 + 导出收口。

由抖音/小红书共用，消除两处几乎重复的内联实现。
视频号因是 CDP 外接 Chrome（非 launch_persistent_context），继续沿用其 dl_utils。
"""
import asyncio
import os
import shutil
import time
import uuid
import importlib.util
from urllib.parse import urlsplit, unquote

# Support both script imports and the existing file-based offline smoke loader.
_paths_file = os.path.join(os.path.dirname(__file__), 'run_paths.py')
if not os.path.isfile(_paths_file):
    raise ImportError('Missing local export dependency: run_paths.py')
_paths_spec = importlib.util.spec_from_file_location(
    '_mediaexport_run_paths', _paths_file)
if _paths_spec is None or _paths_spec.loader is None:
    raise ImportError('Cannot load local export dependency: run_paths.py')
_paths_module = importlib.util.module_from_spec(_paths_spec)
_paths_spec.loader.exec_module(_paths_module)
copy_exclusive = _paths_module.copy_exclusive

_results_spec = importlib.util.spec_from_file_location(
    '_mediaexport_results', os.path.join(os.path.dirname(__file__), 'export_results.py'))
_results_module = importlib.util.module_from_spec(_results_spec)
_results_spec.loader.exec_module(_results_module)
NoDataAvailable = _results_module.NoDataAvailable


async def wait_for_file(dl_root, before, timeout=150, stable_rounds=4, poll=1.5, log=print):
    """轮询下载目录，等文件出现且大小稳定；浏览器崩溃时收编 .crdownload。"""
    deadline = time.time() + timeout
    last_size = {}
    stable = {}
    while time.time() < deadline:
        await asyncio.sleep(poll)
        try:
            current = set(os.listdir(dl_root))
        except FileNotFoundError:
            return None
        for f in sorted(current - before):
            path = os.path.join(dl_root, f)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if not f.endswith(".crdownload"):
                log(f"  ✓ 文件已落地: {f}（{size} bytes）")
                return path
            if size > 0 and last_size.get(f) == size:
                stable[f] = stable.get(f, 0) + 1
                if stable[f] >= stable_rounds - 1:
                    log(f"  ✓ 中途文件已写完（浏览器可能崩了）: {f}（{size} bytes）")
                    return path
            else:
                stable[f] = 0
            last_size[f] = size
    log(f"  ✗ 等待超时（{timeout}s），未发现下载文件")
    return None


async def visible_loaded_image(frame, selector):
    return await frame.locator(selector).evaluate_all("""images => images.some(img => {
        if (!img.complete || !img.naturalWidth || !img.naturalHeight) return false;
        const box = img.getBoundingClientRect();
        if (box.width < 80 || box.height < 80) return false;
        for (let node = img; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible' || style.opacity === '0') return false;
        }
        return true;
    })""")


async def wait_login_state(page, classify, *, timeout=12, wait_for_backend=False):
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        if page.is_closed():
            return 'closed'
        try:
            state = await asyncio.wait_for(classify(page), min(remaining, 2))
        except asyncio.TimeoutError:
            state = 'pending'
        except Exception:
            if page.is_closed():
                return 'closed'
            # A redirect can destroy the document while the selector is resolving.
            state = 'pending'
        if state != 'pending' and (state != 'qr' or not wait_for_backend):
            return state
        await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))
    return 'timeout'


async def set_window_state(page, state, log):
    cdp = None
    try:
        cdp = await page.context.new_cdp_session(page)
        window_id = (await cdp.send('Browser.getWindowForTarget'))['windowId']
        await cdp.send('Browser.setWindowBounds', {'windowId': window_id, 'bounds': {'windowState': state}})
        current = await cdp.send('Browser.getWindowBounds', {'windowId': window_id})
        if current['bounds']['windowState'] == state:
            return True
    except Exception as exc:
        log(f'[WINDOW] {state} 设置失败：{type(exc).__name__}')
        return False
    finally:
        if cdp is not None:
            try:
                await cdp.detach()
            except Exception:
                pass
    log(f'[WINDOW] {state} 未确认')
    return False


async def authenticate_creator(session, url, classify, platform):
    async def visit():
        try:
            response = await session.page.goto(url, wait_until='domcontentloaded', timeout=30000)
        except Exception as exc:
            session.log(f'[LOGIN] {platform} 页面加载失败：{type(exc).__name__}')
            return 'failed'
        if response is None or response.ok is not True:
            session.log(f'[LOGIN] {platform} 后台未返回成功响应')
            return 'failed'
        return await wait_login_state(session.page, classify)

    state = await visit()
    if state == 'ready':
        session.log(f'[LOGIN] {platform} 后台已就绪，继续导出')
        return True
    if state != 'qr':
        reason = '该登录页提供短信验证，未见已确认的扫码入口' if state == 'sms' else '未确认后台可用或扫码二维码'
        print(f'[NOTIFY] LOGIN_BLOCKED: {platform} {reason}', flush=True)
        return False
    if session.scan_opened:
        print(f'[NOTIFY] LOGIN_BLOCKED: {platform}本轮扫码后登录又失效', flush=True)
        return False
    # Release the browser profile before opening the only interactive window.
    await session.context.close()
    session.context = session.page = None
    keep_window = False
    try:
        session.scan_opened = True
        await session.start(interactive=True)
        state = await visit()
        if state == 'qr':
            print(f'[NOTIFY] QR_LOGIN_REQUIRED: {platform}需要扫码，请在浏览器窗口操作', flush=True)
            state = await wait_login_state(session.page, classify, timeout=180, wait_for_backend=True)
        if state == 'ready' and await set_window_state(session.page, 'minimized', session.log):
            keep_window = True
            session.log(f'[LOGIN] {platform} 后台已就绪，继续导出')
        else:
            session.log(f'[LOGIN] {platform} 未确认登录和窗口收回，结束本平台')
        return keep_window
    finally:
        if not keep_window:
            await session.stop()


def validate_xlsx(path):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = ws.max_row or 0
        wb.close()
        return rows
    except Exception:
        return None



def is_expected_backend_route(url, home_url):
    """Confirm only the expected HTTPS backend route, never account or login identity.

    The home URL comes from a reviewed worker constant, not user input. Do not log
    the supplied URL: it may contain authentication or redirect parameters.
    """
    routes = {'creator.douyin.com': '/creator-micro/home',
              'creator.xiaohongshu.com': '/new/home'}
    if not all(isinstance(value, str) and 0 < len(value) <= 8192
               and not any(ord(char) < 32 or ord(char) == 127 or char == '\\' for char in value)
               for value in (url, home_url)):
        return False
    try:
        current, expected = urlsplit(url), urlsplit(home_url)
        prefix = routes.get(expected.hostname)
        if (not prefix or expected.scheme != 'https' or expected.username is not None
                or expected.password is not None or expected.port not in (None, 443)
                or expected.path != prefix or expected.query or expected.fragment):
            return False
        if (current.scheme != 'https' or current.hostname != expected.hostname
                or current.username is not None or current.password is not None
                or current.port not in (None, 443) or current.query or current.fragment):
            return False
        path = unquote(current.path, encoding='utf-8', errors='strict')
        if ('%' in path or '\\' in path or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or any(part in ('.', '..') for part in path.split('/'))):
            return False
        segments = path.casefold().split('/')
        authentication = ('login', 'passport', 'signin', 'sign-in', 'sign', 'auth', 'oauth')
        return path in (prefix, prefix + "/") and not any(
            part.startswith(authentication) for part in segments if part)
    except (ValueError, UnicodeError):
        return False


class Session:
    """持久化上下文浏览器会话：CDP 指定下载目录 + 崩溃自动重建。"""

    def __init__(self, pw, profile_dir, chrome_path, download_root, log=print, check_login=None):
        self.pw = pw
        self.profile_dir = profile_dir
        self.chrome_path = chrome_path
        self.download_root = download_root
        self.log = log
        self.check_login = check_login
        self.context = None
        self.page = None
        self.scan_opened = False

    async def start(self, *, interactive=False):
        mode = '扫码窗口' if interactive else '后台'
        self.log(f'[WINDOW] 启动浏览器（{mode}）')
        self.context = await self.pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            executable_path=self.chrome_path,
            headless=not interactive,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1920, "height": 1080},
            accept_downloads=False,
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(30000)
        try:
            cdp = await self.context.new_cdp_session(self.page)
            await cdp.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": self.download_root,
                "eventsEnabled": False,
            })
        except Exception as e:
            self.log(f"⚠ 设置下载目录失败（改用默认路径）: {e}")
        return self.page

    def alive(self):
        return self.page is not None and not self.page.is_closed()

    async def stop(self):
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        self.context = None
        self.page = None

    async def ensure(self):
        if self.alive():
            return self.page
        if self.context or self.page:
            self.log("⚠ 浏览器已崩溃/关闭，重新启动浏览器...")
            await self.stop()
            await asyncio.sleep(3)
        await self.start()
        if self.check_login:
            try:
                ready = await self.check_login(self)
            except Exception:
                ready = False
            if ready is not True:
                await self.stop()
                raise RuntimeError("login_state_unconfirmed") from None
        return self.page


async def export_table(session, prepare, final_name, download_root, output_dir,
                       expect_rows=1, contract_key=None, validate_contract=None, log=print):
    """prepare(page) 负责导航并返回「导出数据」按钮 locator；点击后落盘收编 → 校验 → 复制。"""
    last_failed, last_contract = None, None
    for attempt in (1, 2):
        page = await session.ensure()
        try:
            button = await prepare(page)
        except NoDataAvailable as empty:
            log(f"  [NO_DATA] {empty.reason}；已跳过下载")
            return None, {'ok': False, 'status': 'NO_DATA', 'reason': empty.reason,
                          'period': empty.period, 'evidence': empty.evidence}
        except Exception as e:
            log(f"  ✗ 页面准备失败: {e}")
            last_contract = {'ok': False, 'reason': f'页面准备失败: {e}'}
            await asyncio.sleep(3)
            continue
        before = set(os.listdir(download_root))
        try:
            await button.click()
            log("  → 已点击「导出数据」，等待文件落盘...")
        except Exception as e:
            log(f"  ⚠ 点击异常（可能浏览器已崩）: {e}")
        path = await wait_for_file(download_root, before, log=log)
        if path and not session.alive():
            log("  ⚠ 浏览器在下载期间崩溃了，已用落盘文件收编")
        if path:
            dest = os.path.join(output_dir, final_name)
            try:
                # Stage under a unique name with the expected extension so
                # completed .crdownload files can still be read by openpyxl.
                attempt_dir = os.path.join(output_dir, '_attempts', uuid.uuid4().hex)
                os.makedirs(attempt_dir, exist_ok=False)
                candidate = os.path.join(attempt_dir, final_name)
                copy_exclusive(path, candidate)
            except Exception as e:
                log(f"  ✗ 复制文件失败: {e}")
                continue
            rows = validate_xlsx(candidate)
            if rows is None or rows < expect_rows:
                last_failed = candidate
                last_contract = {'ok': False, 'reason': '文件不完整', 'rows': rows}
                # A caller's confirmed terminal failure also applies to unreadable
                # originals. Other callers retain this branch's existing retry.
                contract = validate_contract(candidate, contract_key) if contract_key and validate_contract else None
                if contract and not contract.get('ok') and contract.get('retryable') is False:
                    log(f"  ✗ 契约校验未通过: {contract.get('reason')}")
                    log(f"  → 失败原件保留: {candidate}；当前失败不可通过重复下载恢复，停止重试")
                    return candidate, contract
                log(f"  ⚠ 文件不完整，重试（第 {attempt} 次）")
                continue
            contract = None
            if contract_key and validate_contract:
                contract = validate_contract(candidate, contract_key)
                if contract.get("ok"):
                    log(f"  ✓ 契约校验通过: {os.path.basename(dest)}")
                else:
                    last_failed, last_contract = candidate, contract
                    log(f"  ✗ 契约校验未通过: {contract.get('reason')}")
                    if contract.get('retryable') is False:
                        log(f"  → 失败原件保留: {candidate}；当前失败不可通过重复下载恢复，停止重试")
                        return candidate, contract
                    log(f"  → 失败原件保留: {candidate}；准备重试")
                    continue
            try:
                copy_exclusive(candidate, dest)
            except FileExistsError:
                log(f"  ✗ 目标已存在，拒绝覆盖: {dest}")
                return None, {'ok': False, 'reason': '目标已存在，拒绝覆盖'}
            except OSError as exc:
                log(f"  ✗ 发布失败，候选文件及中断写入保留: {exc}")
                return candidate, {'ok': False, 'reason': f'文件发布失败: {exc}', 'rows': rows}
            log(f"  ✓ 校验通过: {os.path.basename(dest)}（{rows} 行）")
            log(f"  → 保存路径: {dest}")
            return dest, contract
        else:
            last_contract = {'ok': False, 'reason': '未取得下载文件（等待超时或点击失败），详见运行日志'}
            log(f"  ⚠ 未拿到文件，重试（第 {attempt} 次）")
        await asyncio.sleep(3)
    return last_failed, last_contract
