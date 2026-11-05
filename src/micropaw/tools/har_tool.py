"""HAR tool: turn a recorded browser session into an endpoint map, an OpenAPI sketch and a client stub.

Cookies, Authorization, and anything that looks like a token never reach the output: header
values and query values are not printed at all, and token-like path parts are folded away.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from micropaw.tools.base import Tool, ToolContext
from micropaw.tools.path_security import resolve_path, validate_file_path

_MAX_HAR = 50 * 1024 * 1024
_MAX_OUT = 30_000
_ID = re.compile(r"\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,}")
_PLACEHOLDER = re.compile(r"\{((?:[A-Za-z_]+_)?(?:id|token)\d*)\}")  # only the names _template makes
_SECRET_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization", "x-api-key"}


def _token_like(s: str) -> bool:
    """JWTs and long mixed letter-digit strings, which are keys and sessions rather than words."""
    return s.startswith("eyJ") or (len(s) >= 20 and bool(re.search(r"\d", s)) and bool(re.search(r"[A-Za-z]", s))
                                   and re.fullmatch(r"[\w\-+/=.~]+", s) is not None)


def _template(path: str) -> str:
    """Fold ids and tokens into {name}, named after the segment before (users/42 becomes users/{user_id})."""
    parts, used = path.split("/"), set()
    for i, seg in enumerate(parts):
        if _ID.fullmatch(seg) or _token_like(seg):
            prev = parts[i - 1].rstrip("s") if i and re.fullmatch(r"[A-Za-z_]+", parts[i - 1]) else ""
            name = f"{prev}_" if prev else ""
            name += "id" if _ID.fullmatch(seg) else "token"
            while name in used:
                name += "2"
            used.add(name)
            parts[i] = "{" + name + "}"
    return "/".join(parts) or "/"


def summarize_har(text: str) -> str:
    entries = json.loads(text)["log"]["entries"]
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    hosts: Counter = Counter()
    auth: set[str] = set()
    for e in entries:
        req, resp = e.get("request", {}), e.get("response", {})
        url = urlsplit(req.get("url", ""))
        if not url.hostname or not re.fullmatch(r"[A-Za-z]{3,10}", str(req.get("method", "GET"))):
            continue
        hosts[url.hostname] += 1
        for h in req.get("headers", []):
            if h.get("name", "").lower() in _SECRET_HEADERS:
                auth.add(h["name"].title())
        g = groups.setdefault((req.get("method", "GET").upper(), url.hostname, _template(url.path)),
                              {"n": 0, "status": Counter(), "types": set(), "query": set(), "body": set()})
        g["n"] += 1
        g["status"][resp.get("status", 0)] += 1
        if mime := resp.get("content", {}).get("mimeType", "").split(";")[0].strip():
            g["types"].add(mime)
        g["query"].update("{param}" if _token_like(q.get("name", "")) else q.get("name", "")
                          for q in req.get("queryString", []))
        if mime := req.get("postData", {}).get("mimeType", "").split(";")[0].strip():
            g["body"].add(mime)
    if not groups:
        return "No requests found in this HAR file."
    ranked = sorted(groups.items(), key=lambda kv: -kv[1]["n"])
    lines = [f"HAR: {len(entries)} requests, {len(groups)} endpoints. Hosts: "
             + ", ".join(f"{h} ({n})" for h, n in hosts.most_common(8))]
    if auth:
        lines.append("Credentials seen in requests (values removed): " + ", ".join(sorted(auth)))
    for (method, host, path), g in ranked[:80]:
        extra = (f"  query: {', '.join(sorted(g['query']))}" if g["query"] else "") + (
            f"  body: {', '.join(sorted(g['body']))}" if g["body"] else "")
        status = ", ".join(f"{s}:{n}" for s, n in sorted(g["status"].items()))
        lines.append(f"{method} {host}{path}  x{g['n']}  status {status}  "
                     f"types: {', '.join(sorted(g['types'])) or '-'}{extra}")
    if len(ranked) > 80:
        lines.append(f"... {len(ranked) - 80} more endpoints not shown")
    main = hosts.most_common(1)[0][0]
    mine = [(m, p, g) for (m, h, p), g in ranked if h == main][:100]
    spec = {"openapi": "3.1.0", "info": {"title": f"{main} (from HAR)", "version": "0"},
            "servers": [{"url": f"https://{main}"}], "paths": {}}
    for method, path, g in mine:
        params = [{"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
                  for n in _PLACEHOLDER.findall(path)]
        params += [{"name": q, "in": "query", "schema": {"type": "string"}} for q in sorted(g["query"])]
        op: dict[str, Any] = {"parameters": params, "responses": {
            str(s): {"description": "seen in capture",
                     **({"content": {t: {} for t in sorted(g["types"])}} if g["types"] else {})}
            for s in sorted(g["status"]) if s}}
        if g["body"]:
            op["requestBody"] = {"content": {t: {} for t in sorted(g["body"])}}
        spec["paths"].setdefault(path, {})[method.lower()] = op
    # Everything from the capture goes into the stub through json.dumps, so it stays text, never code
    stub = ["import httpx", "", f"BASE = {json.dumps(f'https://{main}')}", "", "",
            "class Client:", "    def __init__(self, headers: dict[str, str] | None = None) -> None:",
            "        self.http = httpx.Client(base_url=BASE, headers=headers or {})"]
    names: Counter = Counter()
    for method, path, g in mine[:40]:
        words = re.sub(r"[^A-Za-z0-9]+", "_", _PLACEHOLDER.sub("", path)).strip("_")
        base = method.lower() + "_" + (words or "root")
        names[base] += 1
        fn = base if names[base] == 1 else f"{base}{names[base]}"
        args = "".join(f", {a}" for a in _PLACEHOLDER.findall(path))
        # other braces in the path are literal text: double them so the f-string cannot run them
        literal = "".join(
            part.replace("{", "{{").replace("}", "}}") if i % 2 == 0 else "{" + part + "}"
            for i, part in enumerate(_PLACEHOLDER.split(path))
        )
        stub += ["", f"    def {fn}(self{args}, **params) -> httpx.Response:",
                 f"        return self.http.request({json.dumps(method)}, f{json.dumps(literal)}, params=params)"]
    out = "\n".join(lines + ["", f"OpenAPI 3.1 sketch for {main}:", json.dumps(spec, indent=1), "",
                             "Python client stub (add your own auth headers):", *stub])
    return out if len(out) <= _MAX_OUT else out[:_MAX_OUT] + "\n... (output cut)"


class HarTool(Tool):
    name = "har"
    marks = True  # the capture holds other sites' data
    description = ("Read a HAR file (browser network capture) and list its API endpoints grouped by method, host "
                   "and path, with counts, status codes, content types and query parameter names, plus an "
                   "OpenAPI 3.1 sketch and a Python httpx client stub. Cookies, tokens and header values are removed.")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the .har file"}},
        "required": ["path"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        path = resolve_path(args["path"], context.workspace)
        if error := validate_file_path(str(path)):
            return f"Error: {error}"
        if not path.is_file():
            return f"Error: Not a file: {path}"
        if path.stat().st_size > _MAX_HAR:
            return f"Error: HAR file too large ({path.stat().st_size:,} bytes, max {_MAX_HAR:,})"
        try:
            return await asyncio.to_thread(lambda: summarize_har(path.read_text(encoding="utf-8", errors="replace")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            return f"Error: could not read this as a HAR file ({type(e).__name__})"
