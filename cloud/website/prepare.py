"""Fits the built site's _redirects (written for Netlify) to Cloudflare's
static assets, which already serve /about from about.html:

  * "/about /about.html 200" is what Cloudflare does anyway, and with the
    rule it loops (Cloudflare strips .html, the rule adds it back): dropped.
  * other rewrites to a .html file point at the clean address instead.
  * "/* /index.html 200" is the single-page fallback, which wrangler.toml
    sets as not_found_handling: dropped.
Also removes the notes meant for people running the site themselves."""
import pathlib, sys

site = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "../../projects/aethron-web/site")
for n in ("serve.py", "README.txt", "DEPLOY.md", "vercel.json"):
    (site / n).unlink(missing_ok=True)
f = site / "_redirects"
if f.exists():
    out = []
    for line in f.read_text().splitlines():
        p = line.split()
        if len(p) >= 3 and p[2] == "200":
            src, dst = p[0], p[1]
            if src == "/*":
                continue
            if dst.endswith(".html") and "*" not in src and ":" not in dst:
                clean = dst[:-5]
                if clean.endswith("/index"):
                    clean = clean[:-5]
                if clean == src.rstrip("/") or clean == src:
                    continue
                line = f"{src} {clean} 200"
        out.append(line)
    # specific rules before splats, as Cloudflare prefers
    out.sort(key=lambda l: ("*" in l.split()[0] if l.split() else False))
    f.write_text("\n".join(out) + "\n")
    print(f"_redirects: {len(out)} rules")
