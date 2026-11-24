"""Prepare the existing OpenCLI service and an explicitly authorized Chrome."""
from http.client import HTTPConnection, HTTPException
import json
import subprocess
import time
from urllib.parse import urlencode


PORT = 19825
VERSION = "2.0.3"


class BrowserUnavailable(Exception):
    """A bounded, user-facing failure code and recovery action."""


RECOVERY = {
    "AUTHORIZED_CHROME_CONTEXT_REQUIRED": "请先完成产品的浏览器连接授权，再提交这条小红书链接。",
    "EXISTING_NODE_NOT_FOUND_NO_INSTALL": "当前安装缺少浏览器连接运行环境，请从产品的环境准备入口检查安装。",
    "XHS_BRIDGE_COMPONENT_MISSING": "当前安装缺少浏览器连接组件，请从产品的环境准备入口检查安装。",
    "XHS_BRIDGE_START_DENIED": "当前环境拒绝启动浏览器连接服务，请由宿主管理员检查应用启动权限。",
    "XHS_BRIDGE_START_FAILED": "浏览器连接服务未就绪。请使用产品的环境检查入口核对本机组件。",
    "XHS_BRIDGE_PORT_UNEXPECTED": "浏览器连接端口由其他服务占用。请让本机维护人员检查 19825 端口。",
    "XHS_BRIDGE_VERSION_MISMATCH": "运行中的浏览器连接服务与产品版本不一致，请维护人员检查版本。",
    "XHS_CHROME_MISSING": "找不到已配置的 Chrome，请从产品的环境准备入口检查安装。",
    "XHS_CHROME_START_DENIED": "当前环境拒绝打开已授权的 Chrome，请在该环境允许时打开已授权的浏览器。",
    "XHS_CHROME_START_FAILED": "已授权的 Chrome 未能启动，请在本机打开 Chrome 后重试。",
    "XHS_BRIDGE_EXTENSION_OFFLINE": "请确认已授权的 Chrome 已打开且 OpenCLI 扩展已启用，然后重试。",
    "XHS_BRIDGE_PROFILE_MISMATCH": "已授权的浏览器资料未连接。请打开原先授权的 Chrome 资料后重试。",
    "XHS_BRIDGE_DROPPED": "浏览器连接在读取过程中断开。请确认已授权的 Chrome 和扩展在线后重试。",
    "AUTH_REQUIRED_STOPPED": "请在已授权的 Chrome 中登录小红书后重试。",
    "ACCESS_RESTRICTED_STOPPED": "小红书要求验证或限制了访问。请先在平台页面处理验证再重试。",
}


def status(context_id):
    # A direct loopback check avoids inheriting HTTP proxy settings.
    conn = HTTPConnection("127.0.0.1", PORT, timeout=3)
    try:
        # Windows can take roughly two seconds to report loopback refusal.
        # Check TCP first so an occupied but unresponsive port stays an error.
        conn.connect()
        conn.sock.settimeout(1.5)
        conn.request("GET", "/status?" + urlencode({"contextId": context_id}),
                     headers={"X-OpenCLI": "1"})
        response = conn.getresponse()
        if response.status != 200:
            raise BrowserUnavailable("XHS_BRIDGE_PORT_UNEXPECTED")
        data = json.loads(response.read(65536))
    except ConnectionRefusedError:
        return None
    except (OSError, HTTPException, ValueError) as exc:
        raise BrowserUnavailable("XHS_BRIDGE_PORT_UNEXPECTED") from exc
    finally:
        conn.close()
    if (not isinstance(data, dict) or data.get("ok") is not True
            or not isinstance(data.get("extensionConnected"), bool)
            or not isinstance(data.get("profiles"), list) or data.get("port") != PORT):
        raise BrowserUnavailable("XHS_BRIDGE_PORT_UNEXPECTED")
    if data.get("daemonVersion") != VERSION:
        raise BrowserUnavailable("XHS_BRIDGE_VERSION_MISMATCH")
    return data


def launch(argv, service):
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL}
    if service:
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE
        options["startupinfo"] = startup
    operation = "XHS_BRIDGE" if service else "XHS_CHROME"
    try:
        return subprocess.Popen(argv, **options)
    except OSError as exc:
        denied = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 1260}
        raise BrowserUnavailable(operation + ("_START_DENIED" if denied else "_START_FAILED")) from exc


def wait_status(context_id, accept, seconds):
    deadline = time.monotonic() + seconds
    while True:
        data = status(context_id)
        if accept(data) or time.monotonic() >= deadline:
            return data
        time.sleep(0.25)


def is_ready(data, context_id):
    return (data and data["extensionConnected"] and not data.get("profileDisconnected")
            and data.get("contextId") == context_id)


def prepare(context_id, node, daemon, chrome, launch_default=False):
    data = status(context_id)
    if is_ready(data, context_id):
        return
    if data is None:
        if not daemon.is_file():
            raise BrowserUnavailable("XHS_BRIDGE_COMPONENT_MISSING")
        service = launch([str(node), str(daemon)], service=True)
        data = wait_status(context_id, lambda snapshot: snapshot is not None or service.poll() is not None, 6)
        if data is None:
            raise BrowserUnavailable("XHS_BRIDGE_START_FAILED")
    ready = lambda snapshot: is_ready(snapshot, context_id)
    data = wait_status(context_id, ready, 2)
    if ready(data):
        return
    if data is None:
        raise BrowserUnavailable("XHS_BRIDGE_DROPPED")
    if launch_default:
        if not chrome.is_file():
            raise BrowserUnavailable("XHS_CHROME_MISSING")
        launch([str(chrome), "--profile-directory=Default"], service=False)
        data = wait_status(context_id, ready, 12)
        if ready(data):
            return
        if data is None:
            raise BrowserUnavailable("XHS_BRIDGE_DROPPED")
    if data.get("profiles"):
        raise BrowserUnavailable("XHS_BRIDGE_PROFILE_MISMATCH")
    raise BrowserUnavailable("XHS_BRIDGE_EXTENSION_OFFLINE")
