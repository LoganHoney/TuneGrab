# Deno is the JavaScript runtime yt-dlp now needs for YouTube
FROM denoland/deno:bin AS deno

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    MUSIC_DIR=/music \
    DATA_DIR=/data \
    HOME=/data \
    DENO_DIR=/data/deno \
    XDG_CACHE_HOME=/data/cache

RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
# venv is world-writable so yt-dlp can self-update even when running as PUID/PGID
RUN pip install --no-cache-dir -r /tmp/requirements.txt && chmod -R a+rwX /opt/venv

WORKDIR /srv
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data /music && chmod 777 /data

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
