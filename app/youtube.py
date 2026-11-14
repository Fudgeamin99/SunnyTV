"""yt-dlp integration: keep the latest N videos of a YouTube channel downloaded."""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from . import media, store

COOKIES = os.environ.get("YTDLP_COOKIES", os.path.join(store.DATA_DIR, "cookies.txt"))
EXTS = (".mp4", ".mkv", ".webm")

_q = queue.Queue()
_pending = set()
_plock = threading.Lock()


def _base():
    cmd = ["yt-dlp", "--ignore-config", "--no-warnings"]
    if os.path.exists(COOKIES):
        cmd += ["--cookies", COOKIES]
    return cmd


def normalize(url):
    u = url.strip().rstrip("/")
    if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)$", u):
        u += "/videos"
    return u


def _run(cmd, timeout):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        lines = [l for l in p.stderr.strip().splitlines() if l.strip()]
        raise RuntimeError((lines[-1] if lines else "yt-dlp failed")[:240])
    return p.stdout


def list_latest(url, n):
    out = _run(_base() + ["--flat-playlist", "--playlist-end", str(n), "-J", normalize(url)], 180)
    data = json.loads(out)
    entries = []
    for e in data.get("entries") or []:
        if not e or not e.get("id"):
            continue
        if e.get("live_status") in ("is_live", "is_upcoming"):
            continue
        entries.append({"id": e["id"], "title": e.get("title") or e["id"]})
    return entries[:n]


def _find_file(dest, vid):
    for ext in EXTS:
        p = os.path.join(dest, vid + ext)
        if os.path.exists(p):
            return p
    return None


def download(video_id, dest, quality):
    os.makedirs(dest, exist_ok=True)
    fmt = (f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/bv*[height<={quality}]+ba/"
           f"b[height<={quality}]/b")
    _run(_base() + [
        "-f", fmt, "--merge-output-format", "mp4", "--no-playlist",
        "--socket-timeout", "30", "--retries", "5",
        "-o", os.path.join(dest, "%(id)s.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ], 3600)
    return _find_file(dest, video_id)


def sync_channel(ch_id):
    cfg = store.load()
    ch = store.find_channel(cfg, ch_id)
    if not ch or ch["source"].get("type") != "youtube":
        return
    src = ch["source"]
    url = (src.get("url") or "").strip()
    if not url:
        raise RuntimeError("no channel URL set")
    keep = int(src.get("keep", 15))
    quality = int(src.get("quality", 1080))
    hours = float(src.get("refresh_hours", 6))

    store.update_channel(ch_id, sync_status="checking for new videos")
    entries = list_latest(url, keep)
    if not entries:
        raise RuntimeError("no videos found at that URL")

    dest = os.path.join(media.CACHE_DIR, ch_id)
    old = {it["id"]: it for it in ch.get("yt_items", [])}
    items, errors = [], 0

    def merged(i):
        later = [old[e["id"]] for e in entries[i:] if e["id"] in old and os.path.exists(old[e["id"]]["path"])]
        return items + later

    for i, e in enumerate(entries, 1):
        path = _find_file(dest, e["id"])
        if not path:
            store.update_channel(ch_id, sync_status=f"downloading {i}/{len(entries)}")
            try:
                path = download(e["id"], dest, quality)
            except Exception as ex:
                errors += 1
                print(f"[{ch_id}] download failed for {e['id']}: {ex}", flush=True)
        if path:
            d = media.get_duration(path, probe=True)
            if d:
                items.append({"id": e["id"], "title": e["title"], "path": path, "duration": d})
        store.update_channel(ch_id, yt_items=merged(i))
    media.flush()

    if not items:
        raise RuntimeError("downloads failed (see container logs; YouTube may want cookies)")

    # Drop files for videos that fell out of the latest N.
    keep_ids = {e["id"] for e in entries}
    if os.path.isdir(dest):
        for f in os.listdir(dest):
            if f.split(".")[0] not in keep_ids:
                try:
                    os.remove(os.path.join(dest, f))
                except OSError:
                    pass

    status = f"ok, {len(items)} videos" + (f" ({errors} failed)" if errors else "")
    store.update_channel(ch_id, yt_items=items, last_sync=time.time(),
                         next_sync=time.time() + hours * 3600, sync_status=status)


def remove_cache(ch_id):
    shutil.rmtree(os.path.join(media.CACHE_DIR, ch_id), ignore_errors=True)


def enqueue(ch_id):
    with _plock:
        if ch_id in _pending:
            return
        _pending.add(ch_id)
    store.update_channel(ch_id, sync_status="queued")
    _q.put(ch_id)


def worker_loop():
    while True:
        ch_id = _q.get()
        try:
            sync_channel(ch_id)
        except Exception as e:
            print(f"[{ch_id}] sync error: {e}", flush=True)
            store.update_channel(ch_id, sync_status=f"error: {e}", next_sync=time.time() + 900)
        finally:
            with _plock:
                _pending.discard(ch_id)


def scheduler_loop():
    """Every 30s, queue any YouTube channel that is due for a refresh."""
    while True:
        try:
            now = time.time()
            for ch in store.load()["channels"]:
                if ch["source"].get("type") == "youtube" and ch.get("next_sync", 0) <= now:
                    enqueue(ch["id"])
        except Exception as e:
            print("sync scheduler error:", e, flush=True)
        time.sleep(30)
