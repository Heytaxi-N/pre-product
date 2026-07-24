"""
mitmproxy 抓包脚本 — 只记录 szwego.com 的请求
用法: mitmdump -s capture_szwego.py
然后打开微购相册操作一轮,看终端输出
"""
import json, os, time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from mitmproxy import http

OUTPUT = os.path.join(os.path.dirname(__file__), "captured_api.jsonl")
SENSITIVE_PARTS = ("auth", "cookie", "token", "secret", "password", "sign", "api-key", "api_key")


def _is_sensitive(name):
    key = str(name).lower()
    return any(part in key for part in SENSITIVE_PARTS)


def _redact_url(url):
    parts = urlsplit(url)
    query = [
        (key, "<redacted>" if _is_sensitive(key) else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _redact_headers(headers):
    return {
        key: "<redacted>" if _is_sensitive(key) else _redact_value(value)
        for key, value in dict(headers).items()
    }


def _redact_value(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _is_sensitive(key) else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str) and "://" in value:
        return _redact_url(value)
    return value


def _redact_json_body(text, limit=5000):
    if not text:
        return None
    try:
        return json.dumps(_redact_value(json.loads(text)), ensure_ascii=False)[:limit]
    except (TypeError, ValueError):
        return "<redacted non-JSON body>" if _is_sensitive(text) else text[:limit]

def response(flow: http.HTTPFlow):
    if "szwego.com" not in (flow.request.pretty_host or ""):
        return
    # 跳过纯静态资源
    ct = flow.response.headers.get("content-type", "")
    if any(t in ct for t in ("image/", "font/", "video/")):
        return

    entry = {
        "ts": time.strftime("%H:%M:%S"),
        "method": flow.request.method,
        "url": _redact_url(flow.request.pretty_url),
        "req_headers": _redact_headers(flow.request.headers),
        "req_body": _redact_json_body(flow.request.get_text(), 2000)
                    if flow.request.content else None,
        "status": flow.response.status_code,
        "resp_body": _redact_json_body(flow.response.get_text(), 5000)
                     if flow.response.content else None,
    }

    # 终端高亮打印关键信息
    print(f"\n{'='*60}")
    print(f"[{entry['ts']}] {entry['method']} {entry['url']}")
    print(f"  Status: {entry['status']}")

    # 持久化到文件
    with open(OUTPUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
