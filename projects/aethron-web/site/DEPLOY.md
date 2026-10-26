# Deploy this site

This folder is **fully static** — no server logic needed. The build
already handles the Framer runtime's quirks (CMS byte ranges are
sliced client-side; icon modules ship with real `.js` names). The
files `_redirects`, `404.html` and `vercel.json` cover deep-link
routing per host; hosts ignore the ones they don't use.

## Cloudflare Pages (recommended — free, custom domains)
Dashboard -> Workers & Pages -> Create -> Pages -> Upload assets ->
drop THIS folder. Or: `npx wrangler pages deploy .`

## Netlify
Drag THIS folder onto https://app.netlify.com/drop — done.

## Vercel
`npx vercel .` in this folder (or import in the dashboard).

## GitHub Pages
Push this folder's CONTENTS to your `username.github.io` repo (or a
custom-domain site). NOTE: project pages served under `/repo-name/`
won't work — asset paths are root-absolute; use a user site or a
custom domain.

## Any other static host / CDN / S3 / nginx
Upload as-is. Optional: route unknown extension-less paths to
/index.html so deep links work (real pages and assets must win).

## Local
`python3 serve.py` -> http://127.0.0.1:8000/
(platform: framer; migrated with Aethron)
