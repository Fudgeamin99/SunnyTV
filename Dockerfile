FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates tini fonts-dejavu-core \
 && if [ "$(dpkg --print-architecture)" = "amd64" ]; then \
      apt-get install -y --no-install-recommends intel-media-va-driver i965-va-driver mesa-va-drivers vainfo; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to talk to YouTube reliably.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Kept so the in-app updater can tell when an update needs a new image.
COPY Dockerfile requirements.txt docker-entrypoint.sh /srv/build-meta/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV DATA_DIR=/data MEDIA_DIR=/media PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
