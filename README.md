# Sunny TV

A small self-hosted IPTV server. It turns a YouTube channel (or a folder of videos) into a
live TV channel that loops forever, optionally with advert breaks, and publishes an M3U
playlist and XMLTV guide for Plex, Jellyfin, Emby, VLC or Kodi.

## Run it on a NAS (UGREEN, Synology, etc.)

The image is built automatically by GitHub, so the NAS only has to pull it.

1. **Make a data folder** on the NAS, e.g. `/volume1/docker/sunnytv/data`, and know the path of the folder
   that holds your own videos and ad clips (it can be empty if you only use YouTube).
2. In the NAS's **Docker** app, create a new **project** from a compose file and paste in
   [`docker-compose.nas.yml`](docker-compose.nas.yml). Change the lines marked `CHANGE`
   (the NAS's IP address and the two folder paths), then start it.
3. Open `http://<nas-ip>:8484`.
4. First time only: GitHub makes new images private. In the repo go to **Packages > sunnytv > Package settings**
   and set visibility to **Public**, otherwise the NAS cannot pull it without logging in.

The compose file looks like this (edit the lines marked `CHANGE`):

```yaml
# For a NAS (UGREEN UGOS, Synology, etc.): paste into the NAS's Docker "project" / compose screen.
# Edit the three lines marked CHANGE, nothing else is needed.
services:
  sunny-tv:
    image: ghcr.io/fudgeamin99/sunnytv:latest
    container_name: sunny-tv
    restart: unless-stopped   # needed: the in-app Update button restarts the app by exiting it
    ports:
      - "8484:8000"           # CHANGE the left number if port 8484 is taken on your NAS
    environment:
      TZ: Europe/London
      PUBLIC_URL: http://192.168.1.50:8484   # CHANGE to your NAS IP and the port above
      RESOLUTION: 1280x720
      VIDEO_BITRATE: 3000k
      X264_PRESET: veryfast
      IDLE_STOP_SECONDS: 45
    volumes:
      - /volume1/docker/sunnytv/data:/data   # CHANGE to a folder on your NAS for settings and downloads
      - /volume1/Media:/media:ro             # CHANGE to the folder holding your own videos and ads
```

Videos are encoded on the NAS CPU. A 720p channel needs roughly one modern core, and channels only
encode while someone is watching.

## Run it with Docker Compose on any machine

```bash
git clone https://github.com/Fudgeamin99/SunnyTV && cd SunnyTV
cp .env.example .env        # set PUBLIC_URL to e.g. http://192.168.1.50:8484
docker compose up -d
```

Put your own videos and ad clips in `./media`. Settings and YouTube downloads are kept in `./data`.
Both folders sit next to the compose file by default. To use other folders, set `MEDIA_PATH` and `DATA_PATH` in `.env`
(for example `MEDIA_PATH=/mnt/tv/media`).

The `docker-compose.yml` it uses:

```yaml
# Standard setup: pulls the prebuilt image from GitHub, nothing to build.
# On a NAS, use docker-compose.nas.yml instead (absolute folder paths).
services:
  sunny-tv:
    image: ghcr.io/fudgeamin99/sunnytv:latest
    container_name: sunny-tv
    restart: unless-stopped   # needed: the in-app Update button restarts the app by exiting it
    ports:
      - "${PORT:-8484}:8000"
    environment:
      TZ: ${TZ:-Europe/London}
      # Address your TV/Plex/Jellyfin uses to reach this server (goes into the M3U and guide).
      PUBLIC_URL: ${PUBLIC_URL:-}
      RESOLUTION: ${RESOLUTION:-1280x720}
      VIDEO_BITRATE: ${VIDEO_BITRATE:-3000k}
      X264_PRESET: ${X264_PRESET:-veryfast}
      IDLE_STOP_SECONDS: ${IDLE_STOP_SECONDS:-45}
      # Only if the GitHub repo is private (for the Update button):
      # GITHUB_TOKEN: ghp_xxx
    volumes:
      # Settings, downloaded YouTube videos, logs, app updates. Optional: cookies.txt goes here.
      - ${DATA_PATH:-./data}:/data
      # Your own videos and advert clips (read only).
      - ${MEDIA_PATH:-./media}:/media:ro
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/info', timeout=4)"]
      interval: 30s
      timeout: 6s
      retries: 3
```

To build from source instead of pulling the image:
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`

## Using it

1. **Channels > New channel**, choose **YouTube channel**, paste the channel URL, keep the latest 15 videos.
2. The server downloads them with yt-dlp (progress shows in the channel's playlist panel), then the channel is live.
3. Copy the tuner links from the Channels page into your player.

- **VLC / Kodi / Jellyfin / Emby:** add the *M3U playlist (HLS)* and the *XMLTV guide* URLs.
- **Plex (Live TV & DVR):** Sunny TV acts as a network tuner (it speaks the HDHomeRun protocol), so no bridge is needed.
  In Plex go to *Settings > Live TV & DVR > Set up Plex DVR*, choose *Enter device address manually*, and type the
  tuner address shown on the Channels page (for example `192.168.1.50:8484`, no `http://`). Plex finds your channels.
  When it asks for a guide, choose *Use an XMLTV guide* and paste the XMLTV link **including `http://`**,
  then match each Plex channel to the same-named guide entry. Plex needs a plan that includes Live TV & DVR.
- **Jellyfin / Emby:** add a *HDHomeRun* tuner with the same address, or use the M3U and XMLTV links.

## Updating

- **Update button:** **Settings > Check for updates**, then **Update now**. It downloads the latest app code from
  this repo, checks it, and restarts the app (a few seconds). This needs `restart: unless-stopped`, which the
  compose files already set. If the new code ever fails to start, it falls back to the code in the image.
- **New image:** when a change touches the Dockerfile or requirements, the button tells you. Then pull the
  newest image (re-deploy the project on the NAS, or `docker compose pull && docker compose up -d`).
- If you rebuild or pull a newer image, it automatically wins over an older in-app update.
- If the repo is private, set `GITHUB_TOKEN` (a token with read access to the repo) in the compose environment.

## How it works

- Each channel is a looping timeline anchored at the moment you created it, so every viewer sees the same
  thing at the same time. The "now playing" position is worked out from the wall clock.
- A channel only encodes while someone is watching (stops 45s after the last viewer). Everything is
  re-encoded to one resolution and bitrate (`RESOLUTION`, `VIDEO_BITRATE`) so items join up cleanly.
- **YouTube channels** are re-checked on the refresh interval you set. New uploads join the loop and videos
  outside the latest N are deleted from disk.
- **Advert groups** are lists of clips from your media folder. Each channel sets how often a break plays
  (default every 15 minutes, plus after each video) and how long it lasts, rotating through the group. Apply a group to many channels from the Adverts page.

## Folders

| Path in the container | On the host (default, or the NAS path you chose) | What |
| --- | --- | --- |
| `/media` (read only) | `./media` / `MEDIA_PATH` | Your videos and ad clips. Sub folders work, and the app never changes anything here |
| `/data` | `./data` / `DATA_PATH` | Settings (`config.json`), YouTube downloads (`cache/`), logs (`logs/<channel>.log`), app updates (`code/`). Back this folder up to keep your channels and adverts |

## Good to know

- Only use YouTube content you own or have permission to broadcast. Downloading from YouTube is against its terms,
  and rebroadcasting other people's videos can infringe copyright.
- If yt-dlp reports YouTube wants you to sign in, export your browser cookies to `data/cookies.txt`
  (Netscape format). It is picked up automatically.
- Editing a channel's playlist changes the loop length, so what is "on now" can jump.
- The web UI has no login, and the Update button works for anyone who can open it. Keep it on your home network,
  or put it behind a reverse proxy with authentication.
- FFmpeg problems show up in `data/logs/<channel number>.log`.
