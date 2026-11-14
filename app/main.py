"""Sunny TV: web UI, JSON API, IPTV outputs (M3U + XMLTV) and live streams."""
import asyncio
import base64
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List, Literal, Optional
from xml.sax.saxutils import escape as xesc

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import logos, media, schedule, store, streamer, updater, xtream, youtube

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
    subtitles: bool = False
    sub_lang: str = Field("en", min_length=2, max_length=12, pattern=r"^[A-Za-z-]+$")


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
    out.update({"logo": logos.url_for(ch), "enabled": ch.get("enabled", True), "always_on": ch.get("always_on", False),
                "ad_break_every": ch.get("ad_break_every", 15), "videos": len(vids), "now": now, "live": streamer.is_live(ch["id"])})
    return out


def _enabled(cfg):
    """Channels to list in the guide, playlists and tuner lineup. Switched-off channels stay listed
    (so Plex and IPTV apps keep a stable channel list) and show the offline screen when tuned."""
    return list(cfg["channels"])


def _programmes(cfg, ch, hours, t):
    if not ch.get("enabled", True):
        return [{"title": "Off air", "start": t - 1800, "stop": t + hours * 3600, "ad": False, "breaks": []}]
    return schedule.programmes(schedule.build_timeline(cfg, ch), ch, hours, t)


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
            "xmltv": f"{b}/iptv/xmltv.xml", "hdhr": b.split("://", 1)[-1], "xtream_server": b,
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
            resync = old.get("type") != "youtube" or any(old.get(k) != new[k] for k in ("url", "keep", "quality", "subtitles", "sub_lang"))
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
        streamer.release(ch_id)      # a running stream switches to the "offline" slate and idles out
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
    logos.remove_files(ch_id)
    return {"ok": True}


@app.post("/api/channels/{ch_id}/logo")
async def upload_logo(ch_id: str, request: Request):
    """The request body is the raw image file."""
    get_channel_or_404(store.load(), ch_id)
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > logos.MAX_BYTES + 1024:
        raise HTTPException(413, "That image is over 3 MB. Please use a smaller one")
    data = await request.body()
    try:
        name, ver = logos.save(ch_id, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with store.edit() as cfg:
        ch = get_channel_or_404(cfg, ch_id)
        ch["logo"], ch["logo_v"] = name, ver
    return {"ok": True, "logo": f"/logo/{ch_id}?v={ver}"}


@app.delete("/api/channels/{ch_id}/logo")
def delete_logo(ch_id: str):
    with store.edit() as cfg:
        ch = get_channel_or_404(cfg, ch_id)
        ch.pop("logo", None)
        ch.pop("logo_v", None)
    logos.remove_files(ch_id)
    return {"ok": True}


@app.get("/logo/{ch_id}")
def get_logo(ch_id: str):
    ch = store.find_channel(store.load(), ch_id)
    p = logos.path_for(ch) if ch else None
    if not p:
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


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
    vids = []
    for s in tl["segments"]:
        if s["kind"] != "video":
            continue
        if s.get("part", 0) == 0:
            vids.append({"title": s["title"], "duration": 0.0, "cc": bool(s.get("subs"))})
        # a video split by ad breaks is several parts: its length is where the last part ends
        vids[-1]["duration"] = s.get("src_start", 0.0) + s["duration"]
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
        chans.append({"id": ch["id"], "number": ch["number"], "name": ch["name"], "logo": logos.url_for(ch),
                      "enabled": ch.get("enabled", True), "programmes": _programmes(cfg, ch, hours, t)})
    return {"now": t, "channels": chans}


@app.get("/iptv/channels.m3u")
def m3u(request: Request, format: str = "hls"):
    b = base_url(request)
    lines = [f'#EXTM3U url-tvg="{b}/iptv/xmltv.xml" x-tvg-url="{b}/iptv/xmltv.xml"']
    for ch in sorted(_enabled(store.load()), key=lambda c: c["number"]):
        n = ch["number"]
        name = ch["name"].replace('"', "'")
        logo = f' tvg-logo="{b}{logos.url_for(ch)}"' if logos.url_for(ch) else ""
        lines.append(f'#EXTINF:-1 tvg-id="sunnytv.{n}" tvg-chno="{n}" tvg-name="{name}"{logo} '
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
def xmltv(request: Request, hours: int = 168):
    hours = max(1, min(hours, 168))
    b = base_url(request)
    cfg = store.load()
    t = time.time()
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="Sunny TV">']
    chans = sorted(_enabled(cfg), key=lambda c: c["number"])
    for ch in chans:
        icon = f'<icon src="{_x(b + logos.url_for(ch))}"/>' if logos.url_for(ch) else ""
        parts.append(f'<channel id="sunnytv.{ch["number"]}"><display-name>{_x(ch["name"])}</display-name>'
                     f'<display-name>{ch["number"]}</display-name>{icon}</channel>')
    for ch in chans:
        for p in _programmes(cfg, ch, hours, t):
            parts.append(f'<programme start="{_xt(p["start"])}" stop="{_xt(p["stop"])}" '
                         f'channel="sunnytv.{ch["number"]}"><title>{_x(p["title"])}</title></programme>')
    parts.append("</tv>")
    return Response("\n".join(parts), media_type="application/xml")


# ---------- Xtream Codes API (IPTV Smarters, TiviMate, ...) ----------

class XtreamIn(BaseModel):
    username: str
    password: str


@app.get("/api/xtream")
def xtream_get(request: Request):
    u, p = xtream.get()
    return {"server": base_url(request), "username": u, "password": p}


@app.put("/api/xtream")
def xtream_put(body: XtreamIn):
    try:
        xtream.set_credentials(body.username.strip(), body.password.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _b64(s):
    return base64.b64encode(str(s).encode("utf-8")).decode("ascii")


def _xdt(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _xt_stream_url(b, user, pw, number, hls=False):
    return f"{b}/live/{user}/{pw}/{number}.{'m3u8' if hls else 'ts'}"


@app.get("/player_api.php")
def xtream_api(request: Request, username: str = "", password: str = "", action: str = "",
               stream_id: int = 0, limit: int = 4):
    if not xtream.check(username, password):
        return JSONResponse({"user_info": {"auth": 0, "status": "Disabled", "message": "Invalid login"}})
    cfg = store.load()
    b = base_url(request)
    chans = sorted(_enabled(cfg), key=lambda c: c["number"])
    t = time.time()
    if action == "get_live_categories":
        return [{"category_id": "1", "category_name": "Sunny TV", "parent_id": 0}]
    if action == "get_live_streams":
        return [{"num": i, "name": c["name"], "stream_type": "live", "stream_id": c["number"],
                 "stream_icon": (b + logos.url_for(c)) if logos.url_for(c) else "",
                 "epg_channel_id": f"sunnytv.{c['number']}", "added": str(int(c.get("anchor", t))),
                 "category_id": "1", "custom_sid": "", "tv_archive": 0, "direct_source": "",
                 "tv_archive_duration": 0} for i, c in enumerate(chans, 1)]
    if action in ("get_short_epg", "get_simple_data_table"):
        ch = next((c for c in chans if c["number"] == stream_id), None)
        if not ch:
            return {"epg_listings": []}
        progs = [p for p in _programmes(cfg, ch, 48, t) if p["stop"] > t]
        progs = progs[:max(1, min(limit, 50))] if action == "get_short_epg" else progs[:200]
        return {"epg_listings": [{
            "id": str(i), "epg_id": "", "title": _b64(p["title"]), "lang": "", "start": _xdt(p["start"]),
            "end": _xdt(p["stop"]), "description": _b64(""), "channel_id": f"sunnytv.{ch['number']}",
            "start_timestamp": str(int(p["start"])), "stop_timestamp": str(int(p["stop"])),
            "now_playing": 1 if p["start"] <= t < p["stop"] else 0, "has_archive": 0}
            for i, p in enumerate(progs, 1)]}
    if action in ("get_vod_categories", "get_series_categories", "get_vod_streams", "get_series"):
        return []
    host = b.split("://", 1)[-1]
    return {
        "user_info": {"username": username, "password": password, "message": "", "auth": 1, "status": "Active",
                      "exp_date": None, "is_trial": "0", "active_cons": "0", "created_at": str(int(t)),
                      "max_connections": str(HDHR_TUNERS), "allowed_output_formats": ["m3u8", "ts"]},
        "server_info": {"url": host.split(":")[0], "port": host.split(":")[1] if ":" in host else "80",
                        "https_port": "", "server_protocol": b.split("://", 1)[0], "rtmp_port": "",
                        "timezone": "UTC", "timestamp_now": int(t), "time_now": _xdt(t)}}


@app.get("/get.php")
def xtream_m3u(request: Request, username: str = "", password: str = "", output: str = "ts"):
    if not xtream.check(username, password):
        raise HTTPException(401, "Invalid login")
    b = base_url(request)
    lines = [f'#EXTM3U url-tvg="{b}/xmltv.php?username={username}&password={password}"']
    for c in sorted(_enabled(store.load()), key=lambda c: c["number"]):
        n = c["number"]
        logo = f' tvg-logo="{b}{logos.url_for(c)}"' if logos.url_for(c) else ""
        lines.append(f'#EXTINF:-1 tvg-id="sunnytv.{n}" tvg-chno="{n}" tvg-name="{c["name"].replace(chr(34), chr(39))}"'
                     f'{logo} group-title="Sunny TV",{c["name"]}')
        lines.append(_xt_stream_url(b, username, password, n, hls=output in ("hls", "m3u8")))
    return PlainTextResponse("\n".join(lines) + "\n", media_type="audio/x-mpegurl")


@app.get("/xmltv.php")
def xtream_xmltv(request: Request, username: str = "", password: str = ""):
    if not xtream.check(username, password):
        raise HTTPException(401, "Invalid login")
    return xmltv(request)


@app.get("/live/{username}/{password}/{file}")
async def xtream_live(username: str, password: str, file: str, request: Request):
    m = re.fullmatch(r"(\d{1,5})\.(ts|m3u8)", file)
    if not m or not xtream.check(username, password):
        raise HTTPException(404)
    number = int(m.group(1))
    if m.group(2) == "ts":
        return await stream_ts(number)
    ch = _by_number(number)
    streamer.ensure(ch["id"], always=bool(ch.get("always_on")) and ch.get("enabled", True))
    if not await streamer.wait_ready(ch["id"]):
        raise HTTPException(503, "This channel has nothing to play yet")
    b = base_url(request)
    with open(os.path.join(streamer.out_dir(ch["id"]), "index.m3u8"), "r") as f:
        text = f.read()
    text = "\n".join(f"{b}/stream/{number}/{l}" if l.startswith("seg_") else l for l in text.splitlines()) + "\n"
    return Response(text, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-cache"})


# ---------- streams ----------

def _by_number(number):
    ch = store.find_channel_by_number(store.load(), number)
    if not ch:
        raise HTTPException(404, "no such channel")
    return ch


@app.get("/stream/{number}/{filename}")
async def hls_file(number: int, filename: str):
    if not re.fullmatch(r"index\.m3u8|seg_\d{6}\.ts", filename):
        raise HTTPException(404)
    ch = _by_number(number)
    streamer.ensure(ch["id"], always=bool(ch.get("always_on")) and ch.get("enabled", True))
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
    runner = streamer.ensure(ch["id"], always=bool(ch.get("always_on")) and ch.get("enabled", True))
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
