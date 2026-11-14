"""Works out what every channel is airing, from the wall clock alone.

A channel's timeline is its videos in order, with an advert pod after each one.
The timeline loops forever, anchored at the moment the channel was created, so
every viewer sees the same thing at the same time.
"""
import bisect
import os
import random
import time

from . import media, store

_cache = {}


def channel_videos(ch):
    src = ch.get("source", {})
    out = []
    if src.get("type") == "youtube":
        for it in ch.get("yt_items", []):
            if it.get("duration") and os.path.exists(it["path"]):
                out.append({"path": it["path"], "title": it["title"], "duration": float(it["duration"])})
    else:
        try:
            rels = media.list_files(src.get("folder", ""))
        except ValueError:
            rels = []
        for rel in rels:
            p = media.abs_path(rel)
            d = media.get_duration(p)
            if d:
                out.append({"path": p, "title": media.title_from(rel), "duration": d})
    if ch.get("order") == "shuffle":
        random.Random(ch["id"]).shuffle(out)
    return out


def ad_clips(cfg, ch):
    gid = ch.get("ad_group_id")
    if not gid:
        return []
    g = store.find_group(cfg, gid)
    if not g:
        return []
    out = []
    for rel in g.get("clips", []):
        try:
            p = media.abs_path(rel)
        except ValueError:
            continue
        d = media.get_duration(p)
        if d and os.path.exists(p):
            out.append({"path": p, "title": media.title_from(rel), "duration": d})
    return out


def build_timeline(cfg, ch):
    vids = channel_videos(ch)
    ads = ad_clips(cfg, ch)
    pod = int(ch.get("ad_break_seconds") or 0)
    key = (
        tuple((v["path"], v["duration"]) for v in vids),
        tuple((a["path"], a["duration"]) for a in ads),
        pod,
    )
    hit = _cache.get(ch["id"])
    if hit and hit[0] == key:
        return hit[1]

    segs = []
    ai = 0
    for v in vids:
        segs.append({**v, "kind": "video"})
        if ads and pod > 0:
            filled, n = 0.0, 0
            while filled < pod and n < 20:
                a = ads[ai % len(ads)]
                ai += 1
                n += 1
                segs.append({**a, "kind": "ad"})
                filled += a["duration"]
    t = 0.0
    starts = []
    for s in segs:
        s["start"] = t
        starts.append(t)
        t += s["duration"]
    tl = {"segments": segs, "starts": starts, "total": t}
    _cache[ch["id"]] = (key, tl)
    return tl


def position(tl, ch, t=None):
    """What is airing at time t: segment, offset into it, seconds remaining."""
    t = time.time() if t is None else t
    total = tl["total"]
    if not tl["segments"] or total <= 0:
        return None
    pos = (t - ch.get("anchor", 0)) % total
    i = max(bisect.bisect_right(tl["starts"], pos) - 1, 0)
    seg = tl["segments"][i]
    off = pos - seg["start"]
    return {"index": i, "segment": seg, "offset": off, "remaining": seg["duration"] - off}


def programmes(tl, ch, hours=6, t=None):
    """Guide entries. Each video's slot runs through its advert pod."""
    t = time.time() if t is None else t
    p = position(tl, ch, t)
    if not p:
        return []
    segs = tl["segments"]
    horizon = t + hours * 3600
    i = p["index"]
    cursor = t - p["offset"]
    out, cur, n = [], None, 0
    while cursor < horizon and n < 5000:
        seg = segs[i]
        end = cursor + seg["duration"]
        if seg["kind"] == "video":
            cur = {"title": seg["title"], "start": cursor, "stop": end, "ad": False}
            out.append(cur)
        elif cur is None:
            cur = {"title": "Ad break", "start": cursor, "stop": end, "ad": True}
            out.append(cur)
        else:
            cur["stop"] = end
        cursor = end
        i = (i + 1) % len(segs)
        n += 1
    return out
