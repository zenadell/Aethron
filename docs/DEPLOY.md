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

### Hosted Aethron for a remote agent (MCP over HTTPS)
The `Dockerfile` runs `forge_mcp.py --http`: the same tools an agent gets over
stdio on this computer, served over HTTP for an agent that runs somewhere else.
The image includes Chromium, because every export is checked in a browser.

```bash
docker build -t aethron .
docker run -p 10000:10000 -v aethron-data:/data \
  -e AETHRON_MCP_TOKEN="$(openssl rand -hex 24)" aethron
```

| Path | What it is | Needs the token |
|---|---|---|
| `POST /mcp` | MCP "Streamable HTTP" (JSON-RPC, answered with JSON) | yes |
| `GET /preview/<name>/…` | a finished preview (`<name>` is the export folder) | no — previews are for leads |
| `GET /health` | health check | no |

An agent built on the Vercel AI SDK connects with `@ai-sdk/mcp` using its
HTTP transport. Set the URL to `https://<host>/mcp` and send the header
`Authorization: Bearer <token>`.

**Serving previews on your own domain.** Proxy `/preview/:name/*` on your site
to `https://<host>/preview/:name/*`. The browser stays on your domain, and
every address in a preview already starts with `/preview/<name>`.

### Render
`render.yaml` sets up the hosted Aethron:
1. render.com → **New** → **Blueprint** → pick the repo.
2. Render generates **AETHRON_MCP_TOKEN**. Copy it into the agent's MCP settings.
3. Deploy. The agent's MCP address is `https://<service>.onrender.com/mcp`.

> **Plan.** Use Starter or above. The free plan sleeps after 15 idle minutes
> (the next call waits about a minute), and it has no disk, so prepared
> templates and previews vanish on every restart. Measured on an 11-page Framer
> template: a full preview (build plus browser check) peaks at about 440 MB, and
> it passes inside a 512 MB limit (Starter). That margin is small. If builds of
> bigger templates are killed, move up a plan.

### Studio in a container
The same image runs the Studio: `docker run -p 8899:8899 -e PORT=8899 -e
STUDIO_PASSWORD=choose-a-password aethron python3 studio.py`. Setting
`STUDIO_PASSWORD` turns on HTTP Basic auth (username `aethron`).

### Security checklist before exposing an instance
- [ ] Hosted MCP: `AETHRON_MCP_TOKEN` is long and secret (the server refuses to start without one).
- [ ] Hosted Studio: `STUDIO_PASSWORD` is set (the server warns on public bind without it).
- [ ] You understand each instance is single-tenant — don't share credentials.
- [ ] Sensitive migrations? Prefer local or a private-network deploy.
