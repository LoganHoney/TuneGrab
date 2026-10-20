#!/bin/sh
set -e

# YouTube breaks yt-dlp regularly; grabbing the newest release on every start fixes most failures.
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
  echo "[tunegrab] updating yt-dlp..."
  pip install --no-cache-dir -q -U "yt-dlp[default]" bgutil-ytdlp-pot-provider || echo "[tunegrab] yt-dlp update failed, using bundled version"
fi

if ! touch "$DATA_DIR/.write-test" 2>/dev/null; then
  echo "[tunegrab] ERROR: $DATA_DIR is not writable by uid $(id -u). Fix ownership of the ./data folder on the host."
fi
rm -f "$DATA_DIR/.write-test" 2>/dev/null || true

if ! touch "$MUSIC_DIR/.write-test" 2>/dev/null; then
  echo "[tunegrab] ERROR: $MUSIC_DIR is not writable by uid $(id -u). Check PUID/PGID and your music folder permissions."
fi
rm -f "$MUSIC_DIR/.write-test" 2>/dev/null || true

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
