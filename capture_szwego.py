"""
mitmproxy 抓包脚本 — 只记录 szwego.com 的请求
用法: mitmdump -s capture_szwego.py
然后打开微购相册操作一轮,看终端输出
"""
import json, os, time
from mitmproxy import http

OUTPUT = os.path.join(os.path.dirname(__file__), "captured_api.jsonl")

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
        "url": flow.request.pretty_url,
        "req_headers": dict(flow.request.headers),
        "req_body": flow.request.get_text()[:2000] if flow.request.content else None,
        "status": flow.response.status_code,
        "resp_body": flow.response.get_text()[:5000] if flow.response.content else None,
    }

    # 终端高亮打印关键信息
    print(f"\n{'='*60}")
    print(f"[{entry['ts']}] {entry['method']} {entry['url']}")
    print(f"  Status: {entry['status']}")
    if "authorization" in entry["req_headers"]:
        print(f"  Auth: {entry['req_headers']['authorization'][:80]}...")
    if "token" in (entry.get("req_body") or "").lower():
        print(f"  Body(token): {entry['req_body'][:200]}")

    # 持久化到文件
    with open(OUTPUT, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
