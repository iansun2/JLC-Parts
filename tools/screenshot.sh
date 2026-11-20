#!/bin/sh
# Usage: tools/screenshot.sh OUT.png [CATEGORY] [CLICK_TEXT1,CLICK_TEXT2]
# Takes a screenshot of the running app (port 8385) with headless Chromium in Docker.
OUT_DIR=$(cd "$(dirname "$1")" && pwd)
docker run --rm --network host -v "$(cd "$(dirname "$0")" && pwd)":/tools:ro -v "$OUT_DIR":/out \
  mcr.microsoft.com/playwright/python:v1.49.0-noble \
  sh -c "pip install -q playwright==1.49.0 >/dev/null 2>&1; python /tools/screenshot.py http://127.0.0.1:8385/ /out/$(basename "$1") \"$2\" \"$3\""
