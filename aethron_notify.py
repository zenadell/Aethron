#!/usr/bin/env python3
"""Tell the person what needs them, when Aethron is not in front of them.

WHY THIS EXISTS. Ember offers to restart for an update, to answer a
migration's question, to fix a page that broke — on a card beside it, in
the window. A card in a window nobody is looking at is an offer nobody
gets. So when the window is hidden or another app is in front, the same
offer also goes out as one system notification; clicking it brings
Aethron forward, where the card is waiting.

  macOS    the system's own notification centre (UserNotifications), as
           Aethron, with a click that brings the app forward. From source
           (no app bundle, so no notification identity) AppleScript's
           `display notification` stands in.
  Windows  a toast through PowerShell (Windows.UI.Notifications).
  Linux    notify-send, when it is installed.

Never more than PER_HOUR an hour, never the same thing twice in
REPEAT_S, words cut to what a notification shows, and control characters
taken out. What a notification says is passed as data, never pasted into
a script.

`python3 aethron_notify.py --selftest` checks the rules everywhere and,
on a Mac, that the notification centre's classes are there.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time

ON_CLICK = None        # set by desktop.py: bring Aethron forward
REPEAT_S = 1800        # the same thing is never said twice within this
PER_HOUR = 8
_last: dict = {}       # key -> when it was last sent
_sent: list = []       # when the recent ones were sent
_lock = threading.Lock()
_mac = {"center": None, "ok": None, "why": "", "delegate": None}
SENDER = None          # tests replace how a notification goes out


def _bundled_mac() -> bool:
    """Only an app bundle has the identity the notification centre needs;
    asking for it from a bare python raises inside the system."""
    return (sys.platform == "darwin" and getattr(sys, "frozen", False)
            and ".app/Contents/" in sys.executable)


def how() -> str:
    if sys.platform == "darwin":
        if _bundled_mac():
            try:
                import UserNotifications  # noqa: F401
                return "mac"
            except Exception:
                pass
        return "mac-script" if shutil.which("osascript") else ""
    if os.name == "nt":
        return "windows" if (shutil.which("powershell") or shutil.which("powershell.exe")) else ""
    return "linux" if shutil.which("notify-send") else ""


def available() -> dict:
    h = how()
    if h:
        return {"ok": True, "how": h}
    why = {"darwin": "osascript is missing", "nt": "PowerShell is missing"}.get(
        "nt" if os.name == "nt" else sys.platform,
        "notify-send is not installed (sudo apt install libnotify-bin)")
    return {"ok": False, "how": "", "why": why}


def clean(s, n: int) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def send(title: str, body: str = "", key: str = "") -> dict:
    """Say it once, if it is new and within the hour's allowance."""
    title, body = clean(title, 64) or "Aethron", clean(body, 200)
    key = clean(key, 120) or f"{title}|{body}"
    t = time.time()
    with _lock:
        if t - _last.get(key, -1e9) < REPEAT_S:
            return {"ok": False, "why": "already said recently", "skipped": True}
        while _sent and t - _sent[0] > 3600:
            _sent.pop(0)
        if len(_sent) >= PER_HOUR:
            return {"ok": False, "why": "enough notifications this hour", "skipped": True}
        _last[key] = t
        _sent.append(t)
    h = how()
    if SENDER:
        return SENDER(h, title, body, key)
    if not h:
        return dict(available(), skipped=False)
    threading.Thread(target=_deliver, args=(h, title, body, key), daemon=True).start()
    return {"ok": True, "how": h}


def _deliver(h, title, body, key):
    try:
        if h == "mac":
            _mac_send(title, body, key)
        elif h == "mac-script":
            subprocess.run(["osascript", "-e", "on run argv",
                            "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                            "-e", "end run", title, body],
                           capture_output=True, timeout=10)
        elif h == "windows":
            _win_send(title, body)
        elif h == "linux":
            subprocess.run(["notify-send", "-a", "Aethron", title, body],
                           capture_output=True, timeout=10)
    except Exception:
        pass                    # a notification that cannot go out is not an error


# ── macOS: the system's notification centre ──────────────────────────

def _mac_center():
    if _mac["center"] is not None:
        return _mac["center"]
    import objc
    import UserNotifications as UN
    from Foundation import NSObject
    center = UN.UNUserNotificationCenter.currentNotificationCenter()

    class AethronNotifyDelegate(NSObject, protocols=[objc.protocolNamed("UNUserNotificationCenterDelegate")]):
        def userNotificationCenter_willPresentNotification_withCompletionHandler_(self, c, note, done):
            done(UN.UNNotificationPresentationOptionBanner
                 | UN.UNNotificationPresentationOptionList
                 | UN.UNNotificationPresentationOptionSound)

        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(self, c, resp, done):
            try:
                info = resp.notification().request().content().userInfo() or {}
                if callable(ON_CLICK):
                    ON_CLICK(str(info.get("key") or ""))
            except Exception:
                pass
            done()

    d = AethronNotifyDelegate.alloc().init()
    center.setDelegate_(d)
    _mac.update(center=center, delegate=d)
    return center


def _mac_send(title, body, key):
    import UserNotifications as UN
    center = _mac_center()

    def post():
        content = UN.UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        content.setUserInfo_({"key": key})
        content.setSound_(UN.UNNotificationSound.defaultSound())
        req = UN.UNNotificationRequest.requestWithIdentifier_content_trigger_(
            "aethron-" + re.sub(r"[^\w.-]", "-", key)[:80], content, None)
        center.addNotificationRequest_withCompletionHandler_(
            req, lambda err: _mac.update(why=str(err)) if err else None)

    if _mac["ok"]:
        return post()
    if _mac["ok"] is False:
        return

    def granted(ok, err):
        _mac.update(ok=bool(ok), why=str(err) if err else ("" if ok else "turned off for Aethron"))
        if ok:
            post()
    # the system asks the person once; their answer stands from then on
    center.requestAuthorizationWithOptions_completionHandler_(
        UN.UNAuthorizationOptionAlert | UN.UNAuthorizationOptionSound, granted)


# ── Windows: a toast ─────────────────────────────────────────────────

# PowerShell's own app id is registered on every Windows 10/11 machine;
# an unregistered id is dropped without a word. The words travel in
# environment variables, never inside the script.
_WIN_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_WIN_PS = (
    "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
    "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$t=$x.GetElementsByTagName('text');"
    "[void]$t.Item(0).AppendChild($x.CreateTextNode($env:AE_TITLE));"
    "[void]$t.Item(1).AppendChild($x.CreateTextNode($env:AE_BODY));"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:AE_APP)"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")


def _win_send(title, body):
    env = dict(os.environ, AE_TITLE=title, AE_BODY=body, AE_APP=_WIN_AUMID)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                    "-Command", _WIN_PS], env=env, capture_output=True, timeout=20,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ── selftest ─────────────────────────────────────────────────────────

def _selftest() -> int:
    global SENDER
    ok = 0
    bad = 0

    def check(name, cond, extra=""):
        nonlocal ok, bad
        ok += bool(cond)
        bad += not cond
        print(("  ok   " if cond else "  FAIL ") + name + ("" if cond or not extra else f"   {extra}"))

    out = []
    SENDER = lambda h, t, b, k: (out.append((t, b, k)), {"ok": True, "how": h})[1]
    _last.clear(); _sent.clear()
    r = send("Aethron 1.1.200 is ready", "Restart to finish.", "restart:1.1.200")
    check("a notification goes out", r.get("ok") and out and out[0][0] == "Aethron 1.1.200 is ready")
    r2 = send("Aethron 1.1.200 is ready", "Restart to finish.", "restart:1.1.200")
    check("the same thing is not said twice", r2.get("skipped") and len(out) == 1)
    for i in range(20):
        send(f"t{i}", "b", f"k{i}")
    check("never more than PER_HOUR an hour", len(out) == PER_HOUR, str(len(out)))
    _last.clear(); _sent.clear(); out.clear()
    send("a\x00b\nc" + "x" * 200, "line\r\nbreak\t" + "y" * 400, "k")
    t, b, _ = out[0]
    check("control characters are taken out and the words cut to fit",
          "\x00" not in t and "\n" not in b and len(t) <= 64 and len(b) <= 200, repr((t[:20], b[:20])))
    check("the words are passed as data, never pasted into a script",
          "$env:AE_TITLE" in _WIN_PS and "AE_BODY" in _WIN_PS and "item 1 of argv" in
          open(__file__, encoding="utf-8").read())
    SENDER = None
    a = available()
    check("it says whether this computer can show one, and why not",
          a.get("ok") or a.get("why"), str(a))
    if sys.platform == "darwin":
        try:
            import objc
            import UserNotifications as UN  # noqa: F401
            proto = objc.protocolNamed("UNUserNotificationCenterDelegate")
            check("on a Mac the notification centre and its delegate protocol are there",
                  proto is not None and hasattr(UN, "UNMutableNotificationContent"))
        except Exception as e:
            check("on a Mac the notification centre and its delegate protocol are there", False, str(e))
    print(f"\n{ok}/{ok + bad} notification checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(send(" ".join(sys.argv[1:2]) or "Aethron", " ".join(sys.argv[2:])))
