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


def _clean_vtt(vtt_path, srt_path):
    """WebVTT -> SRT. YouTube's automatic captions repeat each line as they roll up the screen
    and carry timing tags, so strip tags and drop lines already shown by the previous cue."""
    ts = re.compile(r"(\d+:)?(\d\d):(\d\d)[.,](\d{3})\s+-->\s+(\d+:)?(\d\d):(\d\d)[.,](\d{3})")
    with open(vtt_path, encoding="utf-8", errors="replace") as f:
        blocks = re.split(r"\n\s*\n", f.read().replace("\r", ""))
    cues, prev = [], []
    for b in blocks:
        lines = b.strip().split("\n")
        i = next((k for k, l in enumerate(lines) if ts.search(l)), None)
        if i is None:
            continue
        m = ts.search(lines[i])
        a = (int(m[1][:-1]) if m[1] else 0) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000
        z = (int(m[5][:-1]) if m[5] else 0) * 3600 + int(m[6]) * 60 + int(m[7]) + int(m[8]) / 1000
        text = []
        for l in lines[i + 1:]:
            l = re.sub(r"<[^>]+>", "", l).replace("&nbsp;", " ").replace("&amp;", "&") \
                .replace("&lt;", "<").replace("&gt;", ">").strip()
            if l:
                text.append(l)
        new = [l for l in text if l not in prev]
        prev = text or prev
        if not new or z - a < 0.05:
            continue
        cues.append((a, z, new))

    def stamp(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    with open(srt_path, "w", encoding="utf-8") as f:
        for n, (a, z, text) in enumerate(cues, 1):
            f.write(f"{n}\n{stamp(a)} --> {stamp(z)}\n" + "\n".join(text) + "\n\n")
    return len(cues)


NOCC_DAYS = 3


def _nocc_path(dest, video_id, lang):
    return os.path.join(dest, f"{video_id}.{lang}.nocc")


def has_cached_result(dest, video_id, lang):
    """True if we already know the answer for this video (captions saved, or confirmed none lately).
    Markers left by older versions (.nosubs) are not trusted: they were sometimes written by mistake."""
    legacy = os.path.join(dest, f"{video_id}.{lang}.nosubs")
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
        except OSError:
            pass
    if os.path.exists(os.path.join(dest, f"{video_id}.{lang}.srt")):
        return True
    m = _nocc_path(dest, video_id, lang)
    return os.path.exists(m) and time.time() - os.path.getmtime(m) < NOCC_DAYS * 86400


def fetch_subs(video_id, dest, lang):
    """Download captions for one video and leave <id>.srt next to it. Returns the path, or None
    if the video has no captions in that language (remembered, so it is not retried every sync)."""
    srt = os.path.join(dest, f"{video_id}.{lang}.srt")
    none = _nocc_path(dest, video_id, lang)
    if os.path.exists(srt):
        return srt
    if has_cached_result(dest, video_id, lang):
        return None
    cmd = [c for c in _base() if c != "--no-warnings"] + [
        "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", f"{lang},{lang}-orig",
        "--sub-format", "vtt", "--no-playlist", "--sleep-subtitles", "2", "--retries", "5",
        "-o", os.path.join(dest, "%(id)s.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    log = (p.stdout + "\n" + p.stderr)
    if p.returncode != 0 or "Unable to download" in log or "HTTP Error" in log:
        # a real failure (rate limit, network): report it and try again next sync, do not remember "no captions"
        lines = [l for l in log.splitlines() if "rror" in l or "Unable" in l]
        raise RuntimeError((lines[-1] if lines else "caption download failed")[:240])
    vtts = sorted((f for f in os.listdir(dest) if f.startswith(video_id + ".") and f.endswith(".vtt")), key=len)
    ok = False
    if vtts:
        try:
            ok = _clean_vtt(os.path.join(dest, vtts[0]), srt) > 0
        except Exception as ex:
            print(f"[subs] {video_id}: {ex}", flush=True)
        for f in vtts:
            try:
                os.remove(os.path.join(dest, f))
            except OSError:
                pass
    if ok:
        return srt
    try:
        open(none, "w").close()
        if os.path.exists(srt):
            os.remove(srt)
    except OSError:
        pass
    return None


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
    want_subs = bool(src.get("subtitles"))
    lang = (src.get("sub_lang") or "en").strip() or "en"

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
                item = {"id": e["id"], "title": e["title"], "path": path, "duration": d}
                srt = os.path.join(dest, f"{e['id']}.{lang}.srt")
                if want_subs and os.path.exists(srt):
                    item["subs"] = srt
                items.append(item)
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

    base = f"ok, {len(items)} videos" + (f" ({errors} failed)" if errors else "")
    # The videos are live now. Captions are fetched slowly afterwards, so they never hold up a video.
    store.update_channel(ch_id, yt_items=items, last_sync=time.time(), sync_status=base)
    sub_fail, sub_err, sub_ok = 0, "", 0
    if want_subs:
        sub_ok, sub_fail, sub_err = _subtitle_pass(ch_id, items, dest, lang, base)
    status = base + (f", subtitles on {sub_ok}" if want_subs else "")
    if sub_fail:
        status += f" ({sub_fail} not yet: {sub_err or 'rate limited'}; trying again soon)"
    store.update_channel(ch_id, yt_items=items, last_sync=time.time(),
                         next_sync=time.time() + (900 if sub_fail else hours * 3600), sync_status=status)


def _is_429(msg):
    return "429" in msg or "Too Many" in msg


def _subtitle_pass(ch_id, items, dest, lang, base):
    """Download captions one video at a time, slowly. YouTube refuses bursts of caption requests
    (HTTP 429), so pause between videos, back off when refused, and give up for now if it keeps refusing
    (the next sync picks up where this one stopped)."""
    todo = [it for it in items if not it.get("subs") and not has_cached_result(dest, it["id"], lang)]
    ok = sum(1 for it in items if it.get("subs"))
    err = ""
    for n, it in enumerate(todo, 1):
        store.update_channel(ch_id, sync_status=f"{base}, subtitles {ok}/{len(items)} (fetching {n}/{len(todo)})")
        wait = 60
        for attempt in range(3):
            try:
                p = fetch_subs(it["id"], dest, lang)
                if p:
                    it["subs"] = p
                    ok += 1
                    store.update_channel(ch_id, yt_items=items)      # this video shows CC straight away
                break
            except Exception as ex:
                err = str(ex)
                print(f"[{ch_id}] subtitles failed for {it['id']}: {ex}", flush=True)
                if not _is_429(err):
                    break                                           # a different problem: skip this video
                if attempt == 2:
                    return ok, len(todo) - n + 1, err               # still refused: stop for now
                time.sleep(wait)
                wait *= 2
        time.sleep(15)
    return ok, 0, err


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
