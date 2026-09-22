#!/usr/bin/env python3
"""Aethron's in-app approval bridge for Claude Code.

Claude Code runs in print/stream-json mode inside Aethron, so its normal
terminal permission prompt cannot be shown.  The CLI supports routing those
questions to an MCP tool instead.  This tiny stdio MCP server is that tool:
it forwards one request to Aethron's loopback server and waits for the person
to choose Approve or Deny in the Aethron chat.

It deliberately has exactly one tool and no file or shell access.  The random
session token is created by ``aethron_code.CodeSession`` and is required by
the loopback endpoint, so another local process cannot manufacture approvals.
"""
import json
import os
import sys
import urllib.error
import urllib.request


TOOL_NAME = "request"
TOOL_DEF = {
    "name": TOOL_NAME,
    "description": "Ask the Aethron user to approve or deny a Claude Code tool call.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
        },
        "required": ["tool_name", "input"],
    },
}


def send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def deny(message):
    return {"behavior": "deny", "message": str(message)}


def ask_aethron(tool_name, tool_input):
    """Ask the local Studio process and return Claude's required payload."""
    url = os.environ.get("AETHRON_PERMISSION_URL", "")
    token = os.environ.get("AETHRON_PERMISSION_TOKEN", "")
    session_id = os.environ.get("AETHRON_PERMISSION_SESSION", "")
    # This helper is intentionally loopback-only.  It receives the tool input,
    # which may contain local file names or private project text; it must never
    # be forwarded to an arbitrary endpoint supplied through an environment
    # variable or project config.
    if not url.startswith("http://127.0.0.1:"):
        return deny("Aethron's approval channel is unavailable for this session.")
    if not token or not session_id:
        return deny("Aethron's approval channel is not configured for this session.")
    payload = json.dumps({
        "session_id": session_id,
        "token": token,
        "tool_name": str(tool_name or ""),
        "input": tool_input if isinstance(tool_input, dict) else {},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        # The Studio waits up to ten minutes for a person.  Leave a little
        # transport margin so a decision never turns into a false timeout.
        with urllib.request.urlopen(req, timeout=620) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return deny(f"Aethron could not request approval ({e.code}).")
    except Exception:
        return deny("Aethron could not reach its approval window.")
    if answer.get("behavior") == "allow":
        return {"behavior": "allow",
                "updatedInput": (answer.get("updatedInput")
                                 if isinstance(answer.get("updatedInput"), dict)
                                 else (tool_input if isinstance(tool_input, dict) else {}))}
    return deny(answer.get("message") or "Permission was denied in Aethron.")


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except Exception:
            continue
        mid, method = msg.get("id"), msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "aethron_permissions", "version": "1.0"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"tools": [TOOL_DEF]}})
        elif method == "tools/call":
            if params.get("name") != TOOL_NAME:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(
                        deny("unknown Aethron permission tool"))}],
                    "isError": True,
                }})
                continue
            a = params.get("arguments") or {}
            result = ask_aethron(a.get("tool_name"), a.get("input"))
            # Claude Code's permission-prompt tool contract requires the
            # decision object to be JSON-stringified inside an MCP text block.
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(result)}],
            }})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
