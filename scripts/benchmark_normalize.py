"""Offline normalization of the pinned OpenCLI result contract; no network calls."""
import argparse
import json
import re
import sys
from urllib.parse import urlsplit, urlunsplit

REVISION = "9bc5f5ca132ec4d4e48d7ee517d4303bf159e8e8"
MAX_BYTES = 2 * 1024 * 1024


def text(value):
    return value if isinstance(value, str) and value else None


def public_url(value, platform):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        hosts = {"douyin": {"www.douyin.com", "douyin.com"},
                 "xiaohongshu": {"www.xiaohongshu.com", "xiaohongshu.com"}}
        if (url.scheme != "https" or url.hostname not in hosts[platform]
                or url.username or url.password or url.port):
            return None
        return urlunsplit(("https", url.hostname, url.path, "", ""))
    except ValueError:
        return None


def metric(value, provenance, zero_ambiguous=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return {"value": None, "source": provenance, "confidence": "unknown"}
    raw = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:万|亿)?", raw):
        return {"value": None, "source": provenance, "confidence": "unknown"}
    if zero_ambiguous and re.fullmatch(r"0+(?:\.0+)?(?:万|亿)?", raw):
        return {"value": None, "source": provenance, "confidence": "unknown_zero_or_fallback"}
    return {"value": raw, "source": provenance, "confidence": "adapter_reported_unverified"}


def unpack(payload):
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError("OpenCLI 返回失败或格式不符")
    rows = payload.get("data")
    if not isinstance(rows, list) or not all(isinstance(x, dict) for x in rows):
        raise ValueError("OpenCLI 数据不是预期列表")
    return rows


def normalize(platform, operation, payload):
    rows = unpack(payload)
    upstream_operation = "note" if platform == "xiaohongshu" and operation == "detail" else operation
    source = f"OpenCLI contract {REVISION}:{platform}/{upstream_operation}"
    result = {"source": source, "status": "adapter_output_unverified", "items": []}
    if operation == "search":
        if len(rows) > 10:
            raise ValueError("候选超过 10 条，拒绝处理")
        for row in rows:
            item = {"rank": row.get("rank"), "title": text(row.get("desc" if platform == "douyin" else "title")),
                    "author": text(row.get("author")), "public_url": public_url(row.get("url"), platform),
                    "likes": metric(row.get("likes"), source, True)}
            if platform == "douyin":
                for key in ("plays", "comments", "shares"):
                    item[key] = {"value": None, "source": source, "confidence": "upstream_placeholder"}
            else:
                item["published_at"] = {"value": None, "source": source, "confidence": "unknown"}
                item["note_id_inferred_date"] = {"value": text(row.get("published_at")), "source": source,
                                                  "confidence": "inferred_not_official"}
            result["items"].append(item)
    elif platform == "douyin":
        if len(rows) != 1:
            raise ValueError("抖音详情应有且仅有一条")
        row = rows[0]
        item = {"id": text(row.get("aweme_id")), "title": text(row.get("desc")), "author": text(row.get("author")),
                "created_utc": text(row.get("created")), "plays": {"value": None, "source": source, "confidence": "unknown"}}
        for output, original in (("likes", "digg"), ("comments", "comment"), ("collects", "collect"), ("shares", "share")):
            item[output] = metric(row.get(original), source, True)
        result["items"].append(item)
    else:
        fields = {}
        for row in rows:
            key = row.get("field")
            if not isinstance(key, str) or key in fields:
                raise ValueError("小红书详情字段重复或无效")
            fields[key] = row.get("value")
        if not {"title", "author", "content", "likes", "collects", "comments"}.issubset(fields):
            raise ValueError("小红书详情字段不完整")
        item = {key: text(fields.get(key)) for key in ("title", "author", "content", "tags")}
        for key in ("likes", "collects", "comments"):
            item[key] = metric(fields.get(key), source)
        result["items"].append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("douyin", "xiaohongshu"))
    parser.add_argument("operation", choices=("search", "detail"))
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("输入超出大小限制")
        result = normalize(args.platform, args.operation, json.loads(raw.decode("utf-8")))
        sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        sys.stderr.buffer.write("无法处理输入：结果失败、格式不符或超过限制。\n".encode("utf-8"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
