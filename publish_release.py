#!/usr/bin/env python3
"""Publish a release the app can actually reach.

WHY THIS EXISTS

zenadell/Aethron is PRIVATE. An installed copy checks for updates
anonymously, and api.github.com answers 404 for a private repository —
so a GitHub release, however carefully published, is invisible to every
user. The owner is back where they started: downloading a zip, dragging
it to /Applications, deleting the old copy by hand. That is the exact
failure the updater exists to prevent, and no amount of correctness in
aethron_update.py fixes it, because the app can never see the release.

So the release also goes somewhere the app CAN read without a key: a
public Supabase Storage bucket, which the owner already pays nothing
for and already has in aethron_config.json.

    <supabase>/storage/v1/object/public/releases/mac.json   the feed
    <supabase>/storage/v1/object/public/releases/Aethron-<ver>-mac.zip

The source stays private. The builds do not — they were always meant
to be downloadable.

    export SUPABASE_SERVICE_KEY=…            (Supabase → Settings → API)
    python3 publish_release.py 1.0.1 dist/Aethron-mac.zip "what changed"

The service key is a WRITE credential: keep it in the shell that cuts
releases, never in the repo and never inside the app bundle. The app
only ever reads, and reads need no key at all.
"""
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


KEY_FILE = Path.home() / ".aethron-release-key"


def _cfg() -> dict:
    try:
        return json.loads((ROOT / "aethron_config.json").read_text())
    except Exception:
        return {}


def _service_key() -> str:
    """The WRITE credential, from the env or a file outside the repo.

    A key pasted into a chat, a commit or an issue is a key that has to
    be rotated. This one is saved once, to a file only the owner can
    read, and every later release just works — which is the difference
    between an update system that gets used and one that does not.

    It never enters the app bundle: installed copies only ever READ the
    feed, and reads need no key at all.
    """
    k = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if k:
        return k
    try:
        if KEY_FILE.is_file():
            mode = KEY_FILE.stat().st_mode & 0o077
            if mode:                      # readable by anyone else
                KEY_FILE.chmod(0o600)
                print(f"  tightened permissions on {KEY_FILE}")
            return KEY_FILE.read_text().strip()
    except Exception:
        pass
    return ""


def _req(method, url, key, data=None, ctype=None, extra=None):
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    if ctype:
        h["Content-Type"] = ctype
    h.update(extra or {})
    r = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=600) as resp:
            return resp.status, resp.read().decode("utf8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf8", "replace")


def ensure_bucket(base, key, bucket="releases") -> str:
    """Public bucket, created once. Returns '' or a reason it failed."""
    code, body = _req("POST", f"{base}/storage/v1/bucket", key,
                      json.dumps({"id": bucket, "name": bucket,
                                  "public": True}).encode(),
                      "application/json")
    if code in (200, 201):
        return ""
    if code == 409 or "already exists" in body.lower():
        # exists — but it MUST be public or every user's update check
        # 400s with a message about a missing token, which reads like a
        # broken app rather than a bucket setting.
        code, body = _req("PUT", f"{base}/storage/v1/bucket/{bucket}", key,
                          json.dumps({"public": True}).encode(),
                          "application/json")
        return "" if code in (200, 201) else f"bucket not public ({code}) {body[:200]}"
    return f"could not create bucket ({code}) {body[:200]}"


def upload(base, key, bucket, name, blob: bytes, ctype=None) -> str:
    """Overwrite-safe upload. Returns the public URL, raises on failure."""
    ctype = ctype or (mimetypes.guess_type(name)[0]
                      or "application/octet-stream")
    code, body = _req("POST", f"{base}/storage/v1/object/{bucket}/{name}",
                      key, blob, ctype, {"x-upsert": "true",
                                         "Cache-Control": "max-age=60"})
    if code not in (200, 201):
        raise SystemExit(f"upload of {name} failed ({code}): {body[:300]}")
    return f"{base}/storage/v1/object/public/{bucket}/{name}"


def publish(version, zip_path: Path, notes="", platform="mac") -> dict:
    cfg = _cfg()
    base = (os.environ.get("AETHRON_SUPABASE_URL")
            or cfg.get("supabase_url") or "").rstrip("/")
    key = _service_key()
    if not base:
        raise SystemExit("no supabase_url in aethron_config.json")
    if not key:
        raise SystemExit(
            "No Supabase service key.\n"
            "  Get it: dashboard → Project Settings → API keys → "
            "service_role (Reveal)\n"
            f"  Save it once:  echo 'PASTE_KEY' > {KEY_FILE} && "
            f"chmod 600 {KEY_FILE}\n"
            "  (or export SUPABASE_SERVICE_KEY for this shell only)")
    if not zip_path.is_file():
        raise SystemExit(f"no such file: {zip_path}")

    version = version.lstrip("v")
    why = ensure_bucket(base, key)
    if why:
        raise SystemExit(why)

    blob = zip_path.read_bytes()
    asset = f"Aethron-{version}-{platform}.zip"
    print(f"  uploading {asset} ({len(blob)/1e6:.1f} MB)…")
    url = upload(base, key, "releases", asset, blob, "application/zip")

    # THE FEED IS WRITTEN LAST, ON PURPOSE. It is the switch that turns
    # the release on for every installed copy; if the zip upload failed
    # we must not have already pointed users at it.
    feed = {"version": version, "url": url,
            "notes": notes or "Maintenance release."}
    feed_url = upload(base, key, "releases", f"{platform}.json",
                      json.dumps(feed, indent=1).encode(), "application/json")

    # Prove it the way the app will: anonymous GET, no key anywhere.
    with urllib.request.urlopen(feed_url, timeout=30) as r:
        back = json.loads(r.read().decode())
    if back.get("version") != version:
        raise SystemExit(f"feed read back wrong: {back}")
    head = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(head, timeout=30) as r:
        size = int(r.headers.get("Content-Length") or 0)
    if size != len(blob):
        raise SystemExit(f"asset read back {size} bytes, expected {len(blob)}")

    return {"version": version, "feed": feed_url, "asset": url, "size": size}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__.strip().splitlines()[-6].strip())
    out = publish(sys.argv[1], Path(sys.argv[2]),
                  sys.argv[3] if len(sys.argv) > 3 else "")
    print(f"  feed  {out['feed']}")
    print(f"  asset {out['asset']}  ({out['size']/1e6:.1f} MB)")
    print(f"\n✓ v{out['version']} is live — verified by anonymous read, "
          "the same way an installed app sees it.")
