#!/usr/bin/env python3
"""Run this site:  python3 serve.py [port]   (default 8000)

The build is fully static — see DEPLOY.md for one-step hosting
(Cloudflare Pages, Netlify, Vercel, GitHub Pages, any CDN). This
runner is the nicest local dev server: it also implements the exact
Framer protocols (CMS ?range= byte slices, .js MIME, SPA route
fallback), which older Aethron builds require.
"""
import re, sys, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLATFORM = "framer"  # set by build (framer|webflow|static)


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def guess_type(self, path):
        if ".js@" in path or path.endswith((".js", ".mjs")):
            return "text/javascript"
        return super().guess_type(path)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith(".framercms") and "range" in q:
            f = Path(self.translate_path(u.path))
            if not f.is_file():
                return self.send_error(404)
            data = f.read_bytes()
            pieces = []
            for part in q["range"][0].split(","):
                m = re.fullmatch(r"(\d+)-(\d+)?", part.strip())
                if not m:
                    return self.send_error(400)
                s = int(m.group(1))
                e = int(m.group(2)) + 1 if m.group(2) else len(data)
                pieces.append(data[s:e])
            body = b"".join(pieces)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # platform-aware not-found: Framer = SPA (client routes ->
        # index.html); Webflow/static = multi-page (unmatched path is a
        # real 404 -> styled 404.html, never the wrong home page).
        f = Path(self.translate_path(u.path))
        if not f.exists():
            extensionless = "." not in Path(u.path).name
            if PLATFORM == "framer" and extensionless \
                    and (ROOT / "index.html").exists():
                self.path = "/index.html"
            else:
                fb = ROOT / "404.html"
                if fb.exists():
                    body = fb.read_bytes()
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
        return super().do_GET()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"serving at http://127.0.0.1:{port}/  (Ctrl-C stops)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
