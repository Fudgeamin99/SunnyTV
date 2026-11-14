#!/bin/sh
# Picks the newest working app code (an in-app update in /data/code, or the code in the image),
# refreshes yt-dlp, then starts the server.
CODE_DIR=$(cd /srv && python -c "from app import updater; print(updater.choose_code_dir())")
[ -z "$CODE_DIR" ] && CODE_DIR=/srv

# YouTube changes often, so refresh yt-dlp on every start (ignored if offline).
pip install --no-cache-dir -U yt-dlp >/dev/null 2>&1 || true

cd "$CODE_DIR"
echo "Starting Sunny TV from $CODE_DIR"
exec uvicorn --app-dir "$CODE_DIR" app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
