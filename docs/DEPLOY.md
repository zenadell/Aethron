# Deploy

Two different things get deployed: **the migrated site** (a static folder anyone can
host) and **the Studio itself** (for letting others try Aethron).

---

## Part 1 — Deploy a migrated site

Every `site/` build is **fully static**. There is no server logic to run: the CMS
byte-range protocol is handled client-side, and icons ship with real `.js` names.
Download **site.zip** from the Studio (or use the `site/` folder from a CLI build).

Each build already includes `_redirects`, `404.html`, `vercel.json`, and a `DEPLOY.md`
with the specifics — hosts ignore the files they don't use.

### Cloudflare Pages (recommended — free, custom domains, fast)
- Dashboard → **Workers & Pages** → **Create** → **Pages** → **Upload assets** →
  drop the folder.
- Or: `npx wrangler pages deploy .`

### Netlify
- Drag the folder onto **app.netlify.com/drop**. Done.
- `_redirects` handles client-side routes automatically.

### Vercel
- `npx vercel .` in the folder, or import it in the dashboard.
- `vercel.json` provides the extension-less rewrite.

### GitHub Pages
- Push the folder's **contents** to a `username.github.io` repo (or a custom-domain
  site). `404.html` (a copy of the app shell) covers client routes.
- ⚠️ Project pages served under `/repo-name/` won't work — asset paths are
  root-absolute. Use a **user site** or a **custom domain**.

### Any static host / S3 / nginx
- Upload as-is. Optionally route unknown extension-less paths to `/index.html` so
  deep links resolve (real files must win first).

### Local
```bash
cd site && python3 serve.py     # → http://127.0.0.1:8000/
```

---

## Part 2 — Host the Studio for others to test

**Important:** the Studio is a **single-user tool**. It writes to disk and runs
subprocesses on behalf of whoever can reach it. One shared public instance means
anyone can read, edit, or delete anyone's projects. So the model is **one private
instance per person**, always password-protected.

### Local (the simplest "demo")
Anyone with Python can run the real thing:
```bash
git clone https://github.com/zenadell/Aethron.git
cd Aethron && python3 studio.py     # → http://127.0.0.1:8899
```
This is the recommended way to evaluate Aethron — full features, your own machine,
nothing exposed.

### Docker
```bash
docker build -t aethron .
docker run -p 8899:8899 -e STUDIO_PASSWORD=choose-a-password aethron
```
Setting `STUDIO_PASSWORD` turns on HTTP Basic auth (username `aethron`). Setting a
`PORT` env var makes the server bind `0.0.0.0` so a platform router can reach it.

### Render (free tier, one-click)
The repo includes `render.yaml`:
1. Fork/clone the repo to your GitHub.
2. render.com → **New** → **Blueprint** → pick the repo.
3. Set **STUDIO_PASSWORD** when prompted.
4. Deploy. You get a private URL only you (with the password) can use.

> **Free-tier caveats:** the disk is **ephemeral** — projects reset on redeploy, and
> the instance sleeps when idle (first request after sleep is slow). Attach a paid
> disk mounted at `/app/projects` if you want migrations to persist. Fine as-is for a
> playground.

### Railway / Fly.io / any container host
The `Dockerfile` is standard. Provide `PORT` (most platforms inject it) and always
set `STUDIO_PASSWORD` before the instance is reachable from the internet.

### Security checklist before exposing an instance
- [ ] `STUDIO_PASSWORD` is set (the server warns on public bind without it).
- [ ] You understand each instance is single-tenant — don't share credentials.
- [ ] Sensitive migrations? Prefer local or a private-network deploy.
