# -*- coding: utf-8 -*-
"""视频号导出 - 下载容错公共模块

背景：本机 Chrome 154 在下载完成瞬间约半数概率进程崩溃（退出码 0xC0000005），
文件数据其实已写完，只是留成 *.crdownload。旧写法只认「新出现的 .csv」，
浏览器一崩就什么都拿不到，后面的任务还会因为 CDP 断开全部连带失败。

本模块提供：
  snapshot_dirs()        给下载目录拍快照（点击下载前调用）
  wait_for_new_file()    轮询多个目录，等新文件出现且大小稳定（含 .crdownload 收编）
  artifact_dirs()        兼容旧调用；不再扫描共享系统临时目录
  verify_csv()           粗校验 CSV 内容是否可用
"""
import asyncio
import glob
import os
import tempfile
import time
import csv
import re
from adaptive import csv_data_rows, csv_publication_date
from run_paths import exclusive_output


def load_wc_selectors():
    """加载视频号选择器配置（L1 外置）。"""
    import json
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "wechat_selectors.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def artifact_dirs():
    """Compatibility API: do not claim downloads from shared system folders."""
    return []


def _scan(dirs):
    found = {}
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            for fn in os.listdir(d):
                p = os.path.join(d, fn)
                if os.path.isfile(p):
                    try:
                        found[os.path.abspath(p)] = os.path.getsize(p)
                    except OSError:
                        pass
        except OSError:
            continue
    return found


def snapshot_dirs(dirs):
    return set(_scan(dirs).keys())


async def wait_for_new_file(dirs, before, timeout=45, stable_rounds=4, poll=0.5, log=print):
    """等新文件并确认写完。

    返回 (文件路径, 是否收编自未完成文件)；拿不到返回 (None, False)。
    判定：新出现 且 大小连续 stable_rounds 次不变（浏览器崩溃时也能拿到）。
    """
    deadline = time.time() + timeout
    sizes = {}
    stable = {}
    while time.time() < deadline:
        await asyncio.sleep(poll)
        now = _scan(dirs)
        for path, size in now.items():
            if path in before:
                continue
            if size <= 0:
                continue
            if sizes.get(path) == size:
                stable[path] = stable.get(path, 0) + 1
                if stable[path] >= stable_rounds:
                    partial = path.endswith(".crdownload")
                    tag = "（浏览器可能已崩，已收编中途文件）" if partial else ""
                    log(f"  ✓ 文件已就绪{tag}: {os.path.basename(path)}（{size} bytes）")
                    return path, partial
            else:
                stable[path] = 0
            sizes[path] = size
        if not now:
            await asyncio.sleep(0)
    log(f"  ✗ 等待下载超时（{timeout}s）")
    return None, False


def verify_csv(path, min_lines=2):
    """CSV 粗校验：能按 utf-8-sig 读出、至少 min_lines 行非空"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            lines = [l for l in f.read().split("\n") if l.strip()]
        if len(lines) < min_lines:
            return False, len(lines)
        return True, len(lines)
    except Exception:
        return False, 0


def save_csv_with_header(source, destination, required_columns, min_rows=2):
    """Skip only pre-header notes; preserve the actual header and data rows."""
    with open(source, 'r', encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.reader(handle))
    for index, row in enumerate(rows):
        if set(required_columns).issubset({cell.strip() for cell in row}):
            table = rows[index:]
            break
    else:
        raise ValueError('未找到完整表头，下载原件保留，不能当作有效报表')
    data = csv_data_rows(table[0], table[1:])
    if len(data) < min_rows:
        raise ValueError('数据行不足，下载原件保留，不能当作有效报表')
    with exclusive_output(destination, mode='x', encoding='utf-8-sig', newline='') as handle:
        csv.writer(handle).writerows(table)
    dates = []
    if '发布时间' in [cell.strip() for cell in table[0]]:
        column = [cell.strip() for cell in table[0]].index('发布时间')
        dates = sorted((csv_publication_date(row[column]) for row in data),
                       key=lambda date: date.replace('/', '-'))
    return len(data), dates[0] if dates else '', dates[-1] if dates else ''
