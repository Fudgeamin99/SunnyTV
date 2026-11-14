"""Sunny TV: web UI, JSON API, IPTV outputs (M3U + XMLTV) and live streams."""
import asyncio
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Literal, Optional
from xml.sax.saxutils import escape as xesc

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import media, schedule, store, streamer, updater, youtube

PORT = int(os.environ.get("PORT", "8000"))
HDHR_TUNERS = int(os.environ.get("HDHR_TUNERS", "4"))   # simultaneous streams Plex may open
HDHR_ID = "A1B2C3D4"
STATIC = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app):
    os.makedirs(store.DATA_DIR, exist_ok=True)
    for fn in (media.prober_loop, youtube.worker_loop, youtube.scheduler_loop, streamer.keepalive_loop):
        threading.Thread(target=fn, daemon=True).start()
    yield


app = FastAPI(title="Sunny TV", lifespan=lifespan)


# ---------- models ----------

class Source(BaseModel):
    type: Literal["folder", "youtube"] = "folder"
    folder: str = ""
    url: str = ""
    keep: int = Field(15, ge=1, le=100)
    refresh_hours: float = Field(6, ge=0.25, le=168)
    quality: Literal[480, 720, 1080, 1440, 2160] = 1080


class ChannelIn(BaseModel):
    number: int = Field(ge=1, le=9999)
    name: str = Field(min_length=1, max_length=60)
    source: Source = Source()
    order: Literal["sequential", "shuffle"] = "sequential"
    ad_group_id: Optional[str] = None
    ad_break_seconds: int = Field(60, ge=0, le=600)
    ad_break_every: int = Field(15, ge=0, le=120)
    enabled: Optional[bool] = None       # None = leave as it is
    always_on: Optional[bool] = None


class FlagsIn(BaseModel):
    enabled: Optional[bool] = None
    always_on: Optional[bool] = None


class AdGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    clips: List[str] = []


class ApplyIn(BaseModel):
    channel_ids: List[str] = []


# ---------- helpers ----------

def base_url(request: Request):
    return (os.environ.get("PUBLIC_URL") or str(request.base_url)).rstrip("/")


def public(cfg, ch):
    tl = schedule.build_timeline(cfg, ch)
    pos = schedule.position(tl, ch)
    now = None
    if pos:
        s = pos["segment"]
        now = {"title": s["title"], "kind": s["kind"], "offset": pos["offset"], "remaining": pos["remaining"]}
    vids = [s for s in tl["segments"] if s["kind"] == "video" and s.get("part", 0) == 0]
    out = {k: ch.get(k) for k in ("id", "number", "name", "source", "order", "ad_group_id",
                                  "ad_break_seconds", "sync_status", "last_sync")}
    out.update({"enabled": ch.get("enabled", True), "always_on": ch.get("always_on", False),
                "ad_break_every": ch.get("ad_break_every", 15), "videos": len(vids), "now": now, "live": streamer.is_live(ch["id"])})
    return out


def _enabled(cfg):
    return [c for c in cfg["channels"] if c.get("enabled", True)]


def get_channel_or_404(cfg, ch_id):
    ch = store.find_channel(cfg, ch_id)
    if not ch:
        raise HTTPException(404, "channel not found")
    return ch


# ---------- UI ----------

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/info")
def info(request: Request):
    b = base_url(request)
    return {"base": b, "m3u": f"{b}/iptv/channels.m3u", "m3u_ts": f"{b}/iptv/channels.m3u?format=ts",
            "xmltv": f"{b}/iptv/xmltv.xml", "hdhr": b.split("://", 1)[-1],
            "encoder": streamer.encoder_info()}


# ---------- updates ----------

@app.get("/api/update/status")
def update_status():
    return {"repo": updater.REPO, "branch": updater.BRANCH,
            "current": updater.hash_tree(updater.RUNNING_DIR)[:8], "token": bool(updater.TOKEN)}


@app.post("/api/update/check")
def update_check():
    try:
        return updater.check()
    except updater.UpdateError as e:
        raise HTTPException(502, str(e))


@app.post("/api/update/apply")
def update_apply():
    try:
        result = updater.apply()
    except updater.UpdateError as e:
        raise HTTPException(502, str(e))
    # Exit so the container's restart policy brings the app back on the new code.
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return result


# ---------- channels ----------

@app.get("/api/channels")
def list_channels():
    cfg = store.load()
    return [public(cfg, c) for c in sorted(cfg["channels"], key=lambda c: c["number"])]


@app.post("/api/channels")
def create_channel(body: ChannelIn):
    with store.edit() as cfg:
        if store.find_channel_by_number(cfg, body.number):
            raise HTTPException(409, "That channel number is already in use")
        ch = {"id": store.new_id(), **body.model_dump(), "anchor": time.time(),
              "yt_items": [], "next_sync": 0, "sync_status": "never", "last_sync": None}
        ch["enabled"] = True if body.enabled is None else body.enabled
        ch["always_on"] = bool(body.always_on)
        cfg["channels"].append(ch)
    if ch["source"]["type"] == "youtube":
        youtube.enqueue(ch["id"])
    media.kick()
    return {"id": ch["id"]}


@app.put("/api/channels/{ch_id}")
def update_channel(ch_id: str, body: ChannelIn):
    resync = False
    with store.edit() as cfg:
        ch = get_channel_or_404(cfg, ch_id)
        other = store.find_channel_by_number(cfg, body.number)
        if other and other["id"] != ch_id:
            raise HTTPException(409, "That channel number is already in use")
        old = ch["source"]
        new = body.source.model_dump()
        if new["type"] == "youtube":
            resync = old.get("type") != "youtube" or any(old.get(k) != new[k] for k in ("url", "keep", "quality"))
        ch.update(number=body.number, name=body.name, source=new, order=body.order,
                  ad_group_id=body.ad_group_id, ad_break_seconds=body.ad_break_seconds,
                  ad_break_every=body.ad_break_every)
        if body.enabled is not None:
            ch["enabled"] = body.enabled
        if body.always_on is not None:
            ch["always_on"] = body.always_on
    if resync:
        youtube.enqueue(ch_id)
    media.kick()
    return {"ok": True}


@app.put("/api/channels/{ch_id}/flags")
def set_flags(ch_id: str, body: FlagsIn):
    """The two switches on the Channels page: on/off, and keep running 24/7."""
    with store.edit() as cfg:
        ch = get_channel_or_404(cfg, ch_id)
        if body.enabled is not None:
            ch["enabled"] = body.enabled
        if body.always_on is not None:
            ch["always_on"] = body.always_on
        enabled, always = ch.get("enabled", True), ch.get("always_on", False)
    if not enabled:
        streamer.stop(ch_id)
    elif always:
        streamer.ensure(ch_id, always=True)
    else:
        streamer.release(ch_id)
    return {"ok": True, "enabled": enabled, "always_on": always}


@app.delete("/api/channels/{ch_id}")
def delete_channel(ch_id: str):
    with store.edit() as cfg:
        ch = get_channel_or_404(cfg, ch_id)
        cfg["channels"].remove(ch)
    streamer.stop(ch_id)
    youtube.remove_cache(ch_id)
    return {"ok": True}


@app.post("/api/channels/{ch_id}/sync")
def sync_channel(ch_id: str):
    cfg = store.load()
    ch = get_channel_or_404(cfg, ch_id)
    if ch["source"]["type"] != "youtube":
        raise HTTPException(400, "Only YouTube channels sync")
    youtube.enqueue(ch_id)
    return {"ok": True}


@app.get("/api/channels/{ch_id}/items")
def channel_items(ch_id: str):
    cfg = store.load()
    ch = get_channel_or_404(cfg, ch_id)
    tl = schedule.build_timeline(cfg, ch)
    vids = [{"title": s["title"], "duration": s["duration"]} for s in tl["segments"] if s["kind"] == "video" and s.get("part", 0) == 0]
    return {"items": vids, "sync_status": ch.get("sync_status"), "last_sync": ch.get("last_sync"),
            "next_sync": ch.get("next_sync")}


# ---------- library ----------

@app.get("/api/library")
def library(path: str = ""):
    try:
        return media.browse(path)
    except ValueError:
        raise HTTPException(400, "bad path")


@app.get("/api/library/files")
def library_files():
    out = []
    for rel in media.list_files("", ttl=10)[:5000]:
        out.append({"rel": rel, "duration": media.get_duration(media.abs_path(rel))})
    return out


@app.get("/api/library/dirs")
def library_dirs():
    return media.all_dirs()


# ---------- advert groups ----------

@app.get("/api/ad-groups")
def list_groups():
    return store.load()["ad_groups"]


@app.post("/api/ad-groups")
def create_group(body: AdGroupIn):
    with store.edit() as cfg:
        g = {"id": store.new_id(), "name": body.name, "clips": body.clips}
        cfg["ad_groups"].append(g)
    media.kick()
    return g


@app.put("/api/ad-groups/{gid}")
def update_group(gid: str, body: AdGroupIn):
    with store.edit() as cfg:
        g = store.find_group(cfg, gid)
        if not g:
            raise HTTPException(404, "group not found")
        g.update(name=body.name, clips=body.clips)
    media.kick()
    return g


@app.delete("/api/ad-groups/{gid}")
def delete_group(gid: str):
    with store.edit() as cfg:
        g = store.find_group(cfg, gid)
        if not g:
            raise HTTPException(404, "group not found")
        cfg["ad_groups"].remove(g)
        for c in cfg["channels"]:
            if c.get("ad_group_id") == gid:
                c["ad_group_id"] = None
    return {"ok": True}


@app.put("/api/ad-groups/{gid}/channels")
def apply_group(gid: str, body: ApplyIn):
    """Use this group on exactly the listed channels."""
    with store.edit() as cfg:
        if not store.find_group(cfg, gid):
            raise HTTPException(404, "group not found")
        for c in cfg["channels"]:
            if c["id"] in body.channel_ids:
                c["ad_group_id"] = gid
            elif c.get("ad_group_id") == gid:
                c["ad_group_id"] = None
    return {"ok": True}


# ---------- guide + IPTV outputs ----------

@app.get("/api/guide")
def guide(hours: int = 168):
    hours = max(1, min(hours, 168))
    cfg = store.load()
    t = time.time()
    chans = []
    for ch in sorted(_enabled(cfg), key=lambda c: c["number"]):
        tl = schedule.build_timeline(cfg, ch)
        chans.append({"id": ch["id"], "number": ch["number"], "name": ch["name"],
                      "programmes": schedule.programmes(tl, ch, hours, t)})
    return {"now": t, "channels": chans}


@app.get("/iptv/channels.m3u")
def m3u(request: Request, format: str = "hls"):
    b = base_url(request)
    lines = [f'#EXTM3U url-tvg="{b}/iptv/xmltv.xml" x-tvg-url="{b}/iptv/xmltv.xml"']
    for ch in sorted(_enabled(store.load()), key=lambda c: c["number"]):
        n = ch["number"]
        name = ch["name"].replace('"', "'")
        lines.append(f'#EXTINF:-1 tvg-id="sunnytv.{n}" tvg-chno="{n}" tvg-name="{name}" '
                     f'group-title="Sunny TV",{ch["name"]}')
        lines.append(f"{b}/stream/{n}.ts" if format == "ts" else f"{b}/stream/{n}/index.m3u8")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="audio/x-mpegurl")


_BAD_XML = re.compile("[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")


def _x(text):
    """Escape for XML and drop characters XML 1.0 does not allow (strict readers like Plex reject them)."""
    return xesc(_BAD_XML.sub("", str(text)))


# ---------- HDHomeRun emulation, so Plex/Jellyfin/Emby can add this as a network tuner ----------

@app.get("/discover.json")
def hdhr_discover(request: Request):
    b = base_url(request)
    return {"FriendlyName": "Sunny TV", "Manufacturer": "Silicondust", "ModelNumber": "HDTC-2US",
            "FirmwareName": "hdhomeruntc_atsc", "FirmwareVersion": "20200101", "TunerCount": HDHR_TUNERS,
            "DeviceID": HDHR_ID, "DeviceAuth": "sunnytv", "BaseURL": b, "LineupURL": f"{b}/lineup.json"}


@app.get("/lineup_status.json")
def hdhr_status():
    return {"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]}


@app.get("/lineup.json")
def hdhr_lineup(request: Request):
    b = base_url(request)
    return [{"GuideNumber": str(c["number"]), "GuideName": c["name"], "HD": 1,
             "URL": f"{b}/stream/{c['number']}.ts"}
            for c in sorted(_enabled(store.load()), key=lambda c: c["number"])]


@app.post("/lineup.post")
def hdhr_scan():
    return PlainTextResponse("")


@app.get("/device.xml")
def hdhr_device(request: Request):
    b = base_url(request)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0"><specVersion><major>1</major><minor>0</minor></specVersion>'
        f'<URLBase>{b}</URLBase><device><deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
        '<friendlyName>Sunny TV</friendlyName><manufacturer>Silicondust</manufacturer>'
        '<modelName>HDTC-2US</modelName><modelNumber>HDTC-2US</modelNumber>'
        f'<serialNumber>{HDHR_ID}</serialNumber><UDN>uuid:{HDHR_ID}-sunnytv</UDN></device></root>',
        media_type="application/xml")


def _xt(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


@app.get("/iptv/xmltv.xml")
def xmltv(hours: int = 168):
    hours = max(1, min(hours, 168))
    cfg = store.load()
    t = time.time()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="Sunny TV">']
    chans = sorted(_enabled(cfg), key=lambda c: c["number"])
    for ch in chans:
        parts.append(f'<channel id="sunnytv.{ch["number"]}"><display-name>{_x(ch["name"])}</display-name>'
                     f'<display-name>{ch["number"]}</display-name></channel>')
    for ch in chans:
        tl = schedule.build_timeline(cfg, ch)
        for p in schedule.programmes(tl, ch, hours, t):
            parts.append(f'<programme start="{_xt(p["start"])}" stop="{_xt(p["stop"])}" '
                         f'channel="sunnytv.{ch["number"]}"><title>{_x(p["title"])}</title></programme>')
    parts.append("</tv>")
    return Response("\n".join(parts), media_type="application/xml")


# ---------- streams ----------

def _by_number(number):
    ch = store.find_channel_by_number(store.load(), number)
    if not ch or not ch.get("enabled", True):
        raise HTTPException(404, "no such channel (or it is turned off)")
    return ch


@app.get("/stream/{number}/{filename}")
async def hls_file(number: int, filename: str):
    if not re.fullmatch(r"index\.m3u8|seg_\d{6}\.ts", filename):
        raise HTTPException(404)
    ch = _by_number(number)
    streamer.ensure(ch["id"], always=bool(ch.get("always_on")))
    if filename == "index.m3u8":
        if not await streamer.wait_ready(ch["id"]):
            raise HTTPException(503, "This channel has nothing to play yet")
    path = os.path.join(streamer.out_dir(ch["id"]), filename)
    if not os.path.exists(path):
        raise HTTPException(404)
    mt = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
    return FileResponse(path, media_type=mt, headers={"Cache-Control": "no-cache"})


@app.get("/stream/{number}.ts")
async def stream_ts(number: int):
    """Continuous MPEG-TS, for players (like Plex) that prefer it over HLS."""
    ch = _by_number(number)
    runner = streamer.ensure(ch["id"], always=bool(ch.get("always_on")))
    if not await streamer.wait_ready(ch["id"]):
        raise HTTPException(503, "This channel has nothing to play yet")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", f"http://127.0.0.1:{PORT}/stream/{number}/index.m3u8",
        "-c", "copy", "-f", "mpegts", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def gen():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                runner.touch()
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()

    return StreamingResponse(gen(), media_type="video/mp2t", headers={"Cache-Control": "no-cache"})
