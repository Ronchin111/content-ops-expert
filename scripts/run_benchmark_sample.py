"""One authorized Xiaohongshu search/detail sample; signed links stay in memory."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import os
import shutil
import sys
from urllib.parse import urlsplit, parse_qs

HERE = Path(__file__).resolve().parent
media_spec = importlib.util.spec_from_file_location("media_without_cookies", HERE / "media_without_cookies.py")
media_module = importlib.util.module_from_spec(media_spec)
media_spec.loader.exec_module(media_module)
fetch_selected_media = media_module.fetch
spec = importlib.util.spec_from_file_location("benchmark_normalize", HERE / "benchmark_normalize.py")
normalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalizer)
PACKAGE = HERE.parent / "vendor" / "opencli-2.0.3" / "node_modules" / "@jackwener" / "opencli"
NODE = Path(os.environ.get('CONTENT_OPS_NODE') or shutil.which('node') or 'node.exe')


def save(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def invoke(profile, operation, *args):
    env = dict(os.environ, CI="1")
    # Upstream startup replaces a mismatching compatibility link. Refuse that
    # path here so this integration never removes another installation.
    check = "const fs=require('fs'),p=require('path'),os=require('os');try{process.exit(fs.readlinkSync(p.join(os.homedir(),'.opencli','node_modules','@jackwener','opencli'))===process.argv[1]?0:1)}catch{process.exit(1)}"
    try:
        gate = subprocess.run([str(NODE), "-e", check, str(PACKAGE)], env=env, capture_output=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None, {"code": "RUNTIME_LINK_CHECK_FAILED", "outcome": "stopped_before_platform_request"}
    if gate.returncode:
        return None, {"code": "RUNTIME_LINK_MISMATCH", "outcome": "stopped_before_platform_request"}
    # Do not log argv/stderr: the selected URL can contain xsec_token.
    command = [str(NODE), str(PACKAGE / "dist" / "src" / "main.js"), "--profile", profile,
               "xiaohongshu", operation, *args, "-f", "json"]
    try:
        result = subprocess.run(command, env=env, capture_output=True, timeout=180)
        payload = json.loads(result.stdout.decode("utf-8"))
    except (subprocess.TimeoutExpired, OSError, UnicodeError, ValueError):
        return None, {"code": "LOCAL_EXECUTION_OR_OUTPUT_ERROR", "outcome": "unknown_no_auto_retry"}
    if result.returncode or not isinstance(payload, dict) or payload.get("ok") is not True:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        code = error.get("code", "UPSTREAM_ERROR") if isinstance(error, dict) else "UPSTREAM_ERROR"
        if not isinstance(code, str) or not code.replace("_", "").isalnum():
            code = "UPSTREAM_ERROR"
        message = error.get("message", "") if isinstance(error, dict) else ""
        message = re.sub(r"https?://\S+", "[URL omitted]", str(message))
        message = re.sub(r"(?i)(token|cookie|password|authorization|secret)\s*[:=]\s*\S+", "[sensitive value omitted]", message)
        message = re.sub(r"[A-Za-z0-9_+/=-]{24,}", "[identifier omitted]", message)[:700]
        return None, {"code": code, "message": message, "exit_code": result.returncode, "outcome": "stopped_no_auto_retry"}
    return payload, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rank", type=int, default=1)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--note-type", choices=("all", "video", "image"), default="all")
    parser.add_argument("--selected-note-id", help="Resume only this exact previously selected note")
    parser.add_argument("--media-only", action="store_true", help="Refresh access URL then fetch media; do not repeat detail")
    args = parser.parse_args()
    if not 1 <= args.rank <= 10 or not args.query.strip():
        parser.error("rank must be 1..10 and query must be nonempty")
    if args.media_only and (not args.selected_note_id or not args.download):
        parser.error("media-only requires selected-note-id and download")
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "platform": "xiaohongshu",
               "query": args.query, "selected_rank": args.rank, "search": "not_started",
               "detail": "not_started", "media": "not_started", "transcription": "not_started", "note_type": args.note_type}
    payload, error = invoke(args.profile, "search", args.query, "--limit", "10", "--note-type", args.note_type)
    if error:
        summary.update(search="failed", error=error)
        save(args.output / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 1
    try:
        rows = normalizer.unpack(payload)
        candidates = normalizer.normalize("xiaohongshu", "search", payload)
    except ValueError:
        summary.update(search="malformed_result", error={"code": "INVALID_SEARCH_RESULT"})
        save(args.output / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 1
    save(args.output / "candidates.json", candidates)
    summary["search"] = "completed"
    summary["candidate_count"] = len(rows)
    if args.selected_note_id:
        match=next((i for i,row in enumerate(rows) if urlsplit(row.get('url','')).path.rstrip('/').rsplit('/',1)[-1]==args.selected_note_id),None)
        if match is None:
            summary.update(status='partial',media='selected_note_not_in_results')
            save(args.output/'summary.json',summary)
            print(json.dumps(summary,ensure_ascii=False))
            return 2
        args.rank=match+1
        summary['selected_rank']=args.rank
    if len(rows) < args.rank:
        summary["detail"] = "selected_rank_unavailable"
    else:
        row = rows[args.rank - 1]
        target = row.get("url", "")
        parsed = urlsplit(target)
        if (not normalizer.public_url(target, "xiaohongshu")
                or not parse_qs(parsed.query).get("xsec_token")):
            summary["detail"] = "signed_url_unavailable"
        else:
            save(args.output / "selected.json", {"rank": args.rank, "public_url": normalizer.public_url(target, "xiaohongshu"),
                 "title": row.get("title"), "note": "Public display URL omits signed parameters; not a substitute for detail access."})
            if args.media_only:
                receipt=fetch_selected_media(args.profile,target,args.output/'media')
                summary.update(detail='not_requested_existing_result_retained',media=receipt,
                               status='completed' if receipt.get('ok') else 'partial')
                save(args.output/'summary.json',summary)
                print(json.dumps(summary,ensure_ascii=False))
                return 0 if receipt.get('ok') else 2
            detail, error = invoke(args.profile, "note", target)
            if error:
                summary.update(detail="failed", error=error)
            else:
                try:
                    normalized_detail = normalizer.normalize("xiaohongshu", "detail", detail)
                except ValueError:
                    summary.update(detail="malformed_result", error={"code": "INVALID_DETAIL_RESULT"})
                    save(args.output / "summary.json", summary)
                    print(json.dumps(summary, ensure_ascii=False))
                    return 1
                save(args.output / "detail.json", normalized_detail)
                summary["detail"] = "completed"
                if args.download:
                    receipt=fetch_selected_media(args.profile,target,args.output/'media')
                    summary['media_receipt']=receipt
                    summary["media"] = "downloaded_without_cookie_extraction" if receipt.get('ok') else receipt.get('code','MEDIA_FAILED')
    summary["status"] = ("completed" if summary["detail"] == "completed" and
                         (not args.download or summary["media"] == "downloaded_without_cookie_extraction") else "partial")
    save(args.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    if summary["detail"] != "completed":
        return 1
    return 0 if summary["status"] == "completed" else 2


def download_without_cookies(target, output):
    import yt_dlp
    class SilentLogger:
        def debug(self, msg): pass
        def warning(self, msg): pass
        def error(self, msg): pass
    output.mkdir(exist_ok=False)
    opts = {"outtmpl": str(output / "%(id)s.%(ext)s"), "noplaylist": True,
            "cookiefile": None, "cookiesfrombrowser": None, "quiet": True,
            "no_warnings": True, "logger": SilentLogger(), "retries": 0,
            "fragment_retries": 0, "socket_timeout": 25, "overwrites": False,
            "writeinfojson": False, "writethumbnail": False}
    try:
        with yt_dlp.YoutubeDL(opts) as downloader:
            downloader.extract_info(target, download=True)
        files = [p for p in output.iterdir() if p.is_file() and p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov") and p.stat().st_size]
        return "downloaded_without_cookie_extraction" if files else "no_verified_video"
    except Exception:
        return "download_failed_without_cookies_no_auto_retry"


if __name__ == "__main__":
    sys.exit(main())
