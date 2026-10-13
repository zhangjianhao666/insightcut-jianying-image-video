"""No-billing-by-default endpoint probe for an Alibaba Cloud workspace.

Usage:
 python scripts/probe_bailian.py --base-url https://WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
 python scripts/probe_bailian.py --base-url https://WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 --permissions
 python scripts/probe_bailian.py --base-url https://WORKSPACE.cn-beijing.maas.aliyuncs.com/compatible-mode/v1 --chat --model qwen3.8-flash

Uses Python standard library, requires no configuration files, and never prints
the API key or server response body. By default it only performs GET requests.
"""
import argparse
import getpass
import json
import os
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Unexpected HTTP redirect")


def endpoints(base):
    parsed = urlsplit(base.rstrip("/"))
    if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
        raise ValueError("Only official HTTPS port 443 URLs are allowed")
    if not parsed.hostname.endswith(".maas.aliyuncs.com"):
        raise ValueError("Host must be the official *.maas.aliyuncs.com domain")
    if parsed.path != "/compatible-mode/v1" or parsed.query or parsed.fragment:
        raise ValueError("Pass the /compatible-mode/v1 base URL without an extra path")
    return f"https://{parsed.hostname}/api/v1", f"https://{parsed.hostname}/compatible-mode/v1"


def request_json(method, url, key, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "InsightCut-Endpoint-Probe/1.0",
    })
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    try:
        with opener.open(request, timeout=16) as response:
            return response.status, json.loads(response.read(128_000))
    except HTTPError as exc:
        # Never print the body: a provider might echo sensitive information.
        return exc.code, {}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return None, {"network_error": type(exc).__name__}


def describe_models(payload):
    output = payload.get("output") or {}
    models = output.get("models") or payload.get("data") or []
    safe_ids = []
    for item in models[:20]:
        model_id = item.get("model") or item.get("id")
        if isinstance(model_id, str) and re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,100}", model_id):
            safe_ids.append(model_id)
    return safe_ids, output.get("total")


def main(argv=None):
    parser = argparse.ArgumentParser(description="InsightCut / Alibaba Model Studio permission probe")
    parser.add_argument("--base-url", default=os.getenv("LLM_BASE_URL", ""))
    parser.add_argument("--model", default=os.getenv("LLM_MODEL", "qwen3.8-flash"))
    parser.add_argument("--permissions", action="store_true", help="GET Wan model permission metadata (no generation)")
    parser.add_argument("--chat", action="store_true", help="EXPLICIT one possibly billable 24-token chat completion")
    args = parser.parse_args(argv)

    if not args.base_url:
        print("Specify --base-url (your own business workspace endpoint)")
        return 2
    try:
        api, chat = endpoints(args.base_url)
    except ValueError as exc:
        print("Invalid base URL:", exc)
        return 2

    key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("LLM_API_KEY")
    if not key:
        key = getpass.getpass("百炼 API Key（输入隐藏，不保存）：")
    if not key:
        print("API Key is missing")
        return 2

    print("Checking model catalog (GET, no token generation) ...")
    code, data = request_json("GET", f"{api}/models?{urlencode({'capabilities':'TG','page_size':20})}", key)
    catalog_code = code
    print("Catalog HTTP status:", code or "network unavailable")
    if code == 200:
        models, total = describe_models(data)
        print("Models:", ", ".join(models) or "none returned")
        print("Total:", total if total is not None else "unknown")
    else:
        print("Catalog unavailable; check workspace, token scope, and network.")

    if args.permissions:
        print("Checking Wan model permissions (GET; not a generation request) ...")
        code, data = request_json("GET", f"{api}/models/permissions?model=wan2.6-i2v-flash", key)
        print("Permissions HTTP status:", code or "network unavailable")
        if code == 200:
            print("Permission metadata received; verify individual grant details in the console.")
        else:
            print("Permission metadata unavailable; authorization is not proven.")

    if not args.chat:
        print("No generation request submitted. This script consumed 0 generated tokens.")
        return 0 if catalog_code == 200 else 1

    print(f"Performing ONE short chat request: model={args.model}, max_tokens=24")
    code, data = request_json("POST", f"{chat}/chat/completions", key, {
        "model": args.model,
        "messages": [{"role": "user", "content": "只回复：OK"}],
        "max_tokens": 24,
        "stream": False,
    })
    print("Chat HTTP status:", code or "network unavailable")
    if code == 200:
        choices = data.get("choices") or []
        result = (choices[0].get("message") or {}).get("content") if choices else ""
        print("Non-empty text:", bool(result))
        print("Reported total tokens:", (data.get("usage") or {}).get("total_tokens", "unavailable"))
        return 0
    print("Chat request failed; no automatic retry will be attempted.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
