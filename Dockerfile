# Aethron, hosted — the MCP tools over HTTPS for an agent that is not on
# the owner's computer (a lead agent on Render, driven from a phone), and
# the finished previews served from the same address.
#
#   POST /mcp                 MCP "Streamable HTTP"; Authorization: Bearer
#                             <AETHRON_MCP_TOKEN> — nothing works without it
#   GET  /preview/<name>/…    finished previews (public: they are for leads)
#   GET  /health              for the host's health check
#
# Python is all Aethron needs; Chromium is here because every build is
# checked in a real browser before it is called done.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-liberation \
      fonts-noto-color-emoji ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Chrome inside a container: no user namespaces for its sandbox, and a
# 64MB /dev/shm that crashes big pages. One wrapper fixes both for every
# place Aethron opens a browser.
RUN printf '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox --disable-dev-shm-usage --disable-gpu --disable-field-trial-config "$@"\n' \
      > /usr/local/bin/aethron-chromium \
 && chmod +x /usr/local/bin/aethron-chromium

# fonttools+brotli: only the wordmark generator (generate_logo) uses them
RUN pip install --no-cache-dir fonttools brotli

WORKDIR /app
COPY *.py *.md ./

# Projects, templates and previews live on a DISK mounted here. Without
# one (Render's free plan) they are wiped on every restart and deploy.
ENV AETHRON_HOME=/data \
    AETHRON_BROWSER=/usr/local/bin/aethron-chromium \
    PYTHONUNBUFFERED=1 \
    PORT=10000
RUN mkdir -p /data
EXPOSE 10000

CMD ["python3", "forge_mcp.py", "--http"]
