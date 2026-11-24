# -*- coding: utf-8 -*-
"""换机依赖预检（P0 闭环，2026-11-18 GPT复审整改）。

在新机器上运行本脚本，逐项检查报告链路（多平台周期复盘）的全部前提，
输出 PASS/FAIL 清单；全部 PASS 才进入渲染。只检查、不安装任何东西；
安装方式见 references/换机依赖清单.md。

用法（任一 Python 3.10+）:
    python -I 依赖预检_GLM.py
    python -I 依赖预检_GLM.py --wheel-dir "<离线依赖包目录>"
"""
import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

CHECKS = []

def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco

@check("Python 版本 3.10+（导出工具链要求，报告脚本本身 3.9+ 可跑）")
def _py():
    if sys.version_info < (3, 10):
        return f"FAIL: {sys.version_info[:3]}，需要 3.10+"
    return "PASS: " + sys.version.split()[0]

@check("reportlab（PDF 渲染）")
def _reportlab():
    try:
        import reportlab
        return "PASS: reportlab " + reportlab.__version__
    except ImportError:
        return "FAIL: 未安装 reportlab；安装: pip install reportlab==4.4.9（离线: 离线依赖包/reportlab-4.4.9-py3-none-any.whl 及 pillow、charset-normalizer）"

@check("Pillow（reportlab 图像依赖）")
def _pillow():
    try:
        import PIL
        return "PASS: pillow " + PIL.__version__
    except ImportError:
        return "FAIL: 未安装 Pillow；安装: pip install pillow（离线包有 cp312/cp313 两版）"

@check("openpyxl（xlsx 读取）")
def _openpyxl():
    try:
        import openpyxl
        return "PASS: openpyxl " + openpyxl.__version__
    except ImportError:
        return "FAIL: 未安装 openpyxl；安装: pip install openpyxl==3.1.5（离线包同版本）"

@check("IANA 时区数据（zoneinfo: Asia/Shanghai）")
def _tz():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Shanghai")
        return "PASS: 系统自带或已装 tzdata"
    except Exception:
        return "FAIL: ZoneInfo 不可用；Windows 需 pip install tzdata（离线: 离线依赖包/tzdata-2026.6-py2.py3-none-any.whl）"

@check("中文字体（微软雅黑，Windows 系统自带）")
def _fonts():
    files = [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyhbd.ttc"),
             Path("C:/Windows/Fonts/seguiemj.ttf")]
    missing = [p.name for p in files if not p.is_file()]
    if missing:
        return "FAIL: 缺字体 " + ", ".join(missing) + "（Win10/11 标配；缺则从系统字体设置恢复，不随包分发）"
    return "PASS: msyh.ttc / msyhbd.ttc / seguiemj.ttf"

@check("离线依赖包 wheel 完整性（名字齐备）")
def _wheels():
    if not WHEEL_DIR:
        return "SKIP: 未指定 --wheel-dir（在线安装可跳过）"
    need = ["reportlab", "pillow", "charset_normalizer", "openpyxl", "tzdata"]
    have = [f.name for f in WHEEL_DIR.glob("*.whl")]
    miss = [n for n in need if not any(n in h for h in have)]
    if miss:
        return "FAIL: 离线包缺 " + ", ".join(miss) + " 的 wheel"
    return "PASS: 离线包含报告链路全部 wheel（" + str(len(have)) + " 个）"

@check("离线依赖对当前解释器可解析（pip dry-run，不安装）")
def _wheels_resolve():
    if not WHEEL_DIR:
        return "SKIP: 未指定 --wheel-dir（在线安装可跳过）"
    import subprocess
    cmd = [sys.executable, "-I", "-B", "-m", "pip", "install", "--dry-run",
           "--ignore-installed", "--no-cache-dir", "--no-index",
           "--only-binary=:all:", "--find-links", str(WHEEL_DIR),
           "reportlab==4.4.9", "openpyxl==3.1.5", "tzdata"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()
        return "FAIL: 离线包 wheel 与当前解释器(" + sys.version.split()[0] + ")不兼容或依赖不可解析: " + (tail[-1][:120] if tail else "pip 退出码 " + str(r.returncode))
    return "PASS: reportlab+openpyxl+tzdata 及传递依赖对 Python " + sys.version.split()[0] + " 离线可解析（已验证 3.12/3.13 win_amd64）"

def main():
    global WHEEL_DIR
    p = argparse.ArgumentParser()
    p.add_argument("--wheel-dir", type=Path, default=None, help="离线依赖包目录（可选）")
    WHEEL_DIR = p.parse_args().wheel_dir
    print("=" * 62)
    print("报告链路换机依赖预检（只查不装）")
    print("=" * 62)
    fails = 0
    for name, fn in CHECKS:
        try:
            r = fn()
        except Exception as e:
            r = "FAIL: " + type(e).__name__ + ": " + str(e)
        print(f"  [{r.split(':',1)[0].strip()}] {name}")
        print(f"        {r.split(':',1)[1].strip() if ':' in r else r}")
        if r.startswith("FAIL"):
            fails += 1
    print("=" * 62)
    if fails:
        print(f"结论: {fails} 项 FAIL——先补齐再渲染；安装命令见 references/换机依赖清单.md")
        sys.exit(1)
    print("结论: 全部 PASS——报告链路前提齐备")

if __name__ == "__main__":
    main()
