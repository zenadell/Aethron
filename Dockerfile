# Aethron — zero-dependency Python; the image is just Python + the repo.
FROM python:3.12-slim

WORKDIR /app
COPY forge.py studio.py forge_mcp.py PLAYBOOK.md README.md ./

# fonttools+brotli are OPTIONAL (only the `logo` wordmark generator
# uses them) — included here so hosted instances have the full feature
# set. Everything else is stdlib.
RUN pip install --no-cache-dir fonttools brotli

# projects live on the container disk. On free tiers this disk is
# EPHEMERAL (resets on redeploy) — fine for a playground; attach a
# volume/disk for anything you want to keep.
VOLUME /app/projects

ENV PORT=8899
EXPOSE 8899
CMD ["python3", "studio.py"]
