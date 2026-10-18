#!/usr/bin/env python3
"""Aethron for an agent that is not on this computer.

    python3 tests/remote_battery.py [template-project] [--docker IMAGE]

With --docker the server is the CONTAINER (the image Render runs), its
data folder mounted from here — the proof is of what gets deployed.

The lead agent may run on Render and be driven from a phone, so the same
MCP tools are served over HTTP (`forge_mcp.py --http`), behind a token,
and finished previews are served from the same address. This starts that
server for real on a throwaway data folder and drives it the way a remote
agent would: the token is enforced, the agent picks its own pages (any,
or all, and changes its mind), and the preview it publishes renders in a
browser FROM THE SERVER. Offline; the template is a project already on
this machine (default projects/agero).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARGS = [a for a in sys.argv[1:]]
IMAGE = ARGS[ARGS.index("--docker") + 1] if "--docker" in ARGS else ""
if IMAGE:
    del ARGS[ARGS.index("--docker"):ARGS.index("--docker") + 2]
TEMPLATE = ARGS[0] if ARGS else "agero"
TOKEN = "t" * 12 + "remote-battery-secret"
checks = []


def ck(name, ok, detail=""):
    checks.append((name, bool(ok)))
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {str(detail)[:400]}"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(method, url, body=None, token=TOKEN, timeout=900):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def main():
    src = ROOT / "projects" / TEMPLATE
    if not (src / "copy_map.json").exists():
        print(f"SKIPPED — no prepared template at projects/{TEMPLATE} "
              "(UNVERIFIED, not passed)")
        return 0
    sys.path.insert(0, str(ROOT))
    import forge
    # a container runtime only sees folders under the home directory
    home = Path(tempfile.mkdtemp(prefix=".ae-remote-",
                                 dir=Path.home() if IMAGE else None))
    (home / "projects").mkdir()
    shutil.copytree(src, home / "projects" / "tpl",
                    ignore=shutil.ignore_patterns("site", ".history", "convert-*",
                                                  "export-*"),
                    copy_function=lambda a, b: os.link(a, b)
                    if "pristine" in Path(a).parts else shutil.copy2(a, b))
    port = free_port()
    env = dict(os.environ, AETHRON_HOME=str(home))
    env.pop("AETHRON_MCP_TOKEN", None)

    def server(with_token):
        if IMAGE:
            return ["docker", "run", "--rm", "--name", f"ae-remote-{port}",
                    "-p", f"127.0.0.1:{port}:10000", "-v", f"{home}:/data"] + (["--memory", os.environ["AE_DOCKER_MEMORY"]] if os.environ.get("AE_DOCKER_MEMORY") else []) + (["--cpus", os.environ["AE_DOCKER_CPUS"]] if os.environ.get("AE_DOCKER_CPUS") else []) + \
                (["-e", f"AETHRON_MCP_TOKEN={TOKEN}"] if with_token else []) + [IMAGE]
        return [sys.executable, str(ROOT / "forge_mcp.py"), "--http", str(port)]
    r = subprocess.run(server(False), env=env, capture_output=True, text=True,
                       timeout=60)
    ck("the server refuses to start without a secret token",
       r.returncode != 0 and "AETHRON_MCP_TOKEN" in (r.stdout + r.stderr), r.stderr[-300:])

    env["AETHRON_MCP_TOKEN"] = TOKEN
    srv = subprocess.Popen(server(True), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(300):
            try:
                if http("GET", base + "/health", token=None, timeout=2)[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
        n = [0]

        def call(tool, args, token=TOKEN):
            n[0] += 1
            code, _, body = http("POST", base + "/mcp", {
                "jsonrpc": "2.0", "id": n[0], "method": "tools/call",
                "params": {"name": tool, "arguments": args}}, token=token)
            if code != 200:
                return False, f"HTTP {code}: {body[:200]!r}"
            res = json.loads(body).get("result") or {}
            text = " ".join(c.get("text", "") for c in res.get("content", []))
            return not res.get("isError"), text

        code, hdr, _ = http("POST", base + "/mcp", {"jsonrpc": "2.0", "id": 1,
                                                   "method": "tools/list"}, token=None)
        ck("no token: refused (401)", code == 401, code)
        code, _, _ = http("POST", base + "/mcp", {"jsonrpc": "2.0", "id": 1,
                                                 "method": "tools/list"}, token="wrong" * 6)
        ck("wrong token: refused (401)", code == 401, code)
        code, _, body = http("POST", base + "/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"}})
        ck("initialize answers over HTTP", code == 200 and b'"aethron"' in body, body[:200])
        code, _, _ = http("POST", base + "/mcp", {"jsonrpc": "2.0",
                                                 "method": "notifications/initialized"})
        ck("a notification is accepted with no body (202)", code == 202, code)
        code, _, body = http("POST", base + "/mcp", {"jsonrpc": "2.0", "id": 2,
                                                    "method": "tools/list"})
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        ck("every tool is offered over HTTP, preview tools included",
           {"make_preview", "preview_pages", "add_block", "export_preview",
            "create_project", "set_content_bulk"} <= set(names), names)

        ok, out = call("preview_pages", {"project": "tpl"})
        have = re.search(r"pages this template has: (.*)", out)
        have = [p.strip() for p in have.group(1).split(",")] if have else []
        ck("the agent can see every page the template has", ok and len(have) > 3, out)

        ok, out = call("make_preview", {"template": "tpl", "name": "Lead Remote",
                                        "pages": ["all"]})
        # every page the template has (not agero's names: any template)
        ck("the agent may take ALL pages", ok and len(have) > 3 and all(
            ("index.html" if p == "home" else p + ".html") in out
            for p in have), out)
        others = [p for p in have if p not in ("home", "about", "contact")]
        pick = ["home", others[0]] if others else ["home"]
        ok, out = call("preview_pages", {"project": "lead-remote", "pages": pick})
        ck(f"…and change its mind to any set it likes ({', '.join(pick)})",
           ok and f"now builds: {', '.join(pick)}" in out, out)

        page = (home / "projects" / "lead-remote" / "pristine" / "index.html").read_text(
            errors="ignore")
        ok, out = call("get_content", {"project": "lead-remote", "limit": 200,
                                       "brand_notes": False})
        listed = json.loads(out[out.index("["):]) if "[" in out else []
        anchor = next((e["old"] for e in listed if 2 <= len(e["old"].split()) <= 6
                       and e["old"] in page), "")
        ok, out = call("add_block", {"project": "lead-remote", "kind": "map",
                                     "anchor": anchor, "page": "home",
                                     "data": {"query": "Lagos"}})
        ck("an extra is added on the home page", ok, out)
        ok, out = call("preview_pages", {"project": "lead-remote", "pages": [pick[-1]]})
        ck("dropping the page an extra sits on says so, and keeps it",
           ok and "extras not shown on these pages: b1" in out, out)
        ok, out = call("preview_pages", {"project": "lead-remote", "pages": pick})
        ck("…and bringing the page back brings the extra back",
           ok and "extras not shown" not in out, out)

        ok, out = call("export_preview", {"project": "lead-remote"})
        ck("the export's browser check PASSES", ok and ": PASS" in out, out)

        code, hdr, body = http("GET", base + "/preview/lead-remote", token=None)
        ck("the server serves the preview at /preview/<name> with no token",
           code == 200 and "text/html" in hdr.get("Content-Type", "")
           and b"__AE_PREVIEW__" in body, (code, hdr.get("Content-Type")))
        mjs = next((home / "previews" / "lead-remote" / "assets" / "chunks").glob("*.mjs"), None)
        if mjs:
            code, hdr, _ = http("GET", f"{base}/preview/lead-remote/assets/chunks/{mjs.name}",
                                token=None)
            ck("a module is served as JavaScript (or the page stays blank)",
               code == 200 and "javascript" in hdr.get("Content-Type", ""),
               hdr.get("Content-Type"))
        for bad in ("/preview/lead-remote/../../projects/tpl/forge.json",
                    "/preview/..%2fprojects%2ftpl/forge.json",
                    "/projects/tpl/forge.json", "/preview/lead-remote/%2e%2e/%2e%2e/forge.json"):
            code, _, _ = http("GET", base + bad, token=None)
            if code != 404:
                break
        ck("nothing outside the previews folder can be read", code == 404, (bad, code))

        # the real scenario: the page as a lead's browser gets it, from the server
        ssr = len(forge._visible_text(body.decode("utf-8", "ignore")))
        got = forge._render_page(forge._find_browser(), f"{base}/preview/lead-remote",
                                 budget_ms=9000) if forge._find_browser() else {}
        text = len(forge._visible_text(got.get("dom") or ""))
        if not forge._find_browser():
            print("  SKIP the served preview renders in a browser — no browser (UNVERIFIED)")
        else:
            ck("served from Aethron itself, the preview renders in a browser",
               text >= 0.4 * ssr and "__AE_PREVIEW__" in (got.get("dom") or ""),
               f"{text} of {ssr} chars; {got.get('error')}")
    finally:
        if IMAGE:
            subprocess.run(["docker", "rm", "-f", f"ae-remote-{port}"],
                           capture_output=True)
        srv.kill()
        shutil.rmtree(home, ignore_errors=True)
    bad = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(bad)}/{len(checks)} green")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
