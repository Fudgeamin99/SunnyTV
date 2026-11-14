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
                v = {"path": it["path"], "title": it["title"], "duration": float(it["duration"])}
                if src.get("subtitles") and it.get("subs") and os.path.exists(it["subs"]):
                    v["subs"] = it["subs"]
                out.append(v)
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


MINUTE = 60          # seconds per minute (tests shrink this)
MIN_TAIL = 60        # do not leave a final part shorter than this


def _pod(ads, pod, ai):
    """Ad segments filling exactly `pod` seconds (the last clip is cut short if needed).
    Returns (segments, next ad index)."""
    out, filled, n = [], 0.0, 0
    while filled < pod - 0.5 and n < 60:
        a = ads[ai % len(ads)]
        ai += 1
        n += 1
        left = pod - filled
        if out and left < 3:
            break                       # not worth a clip that short
        d = min(a["duration"], left)
        out.append({**a, "kind": "ad", "part": 0, "src_start": 0.0, "duration": d})
        filled += d
    return out, ai


def build_timeline(cfg, ch):
    vids = channel_videos(ch)
    ads = ad_clips(cfg, ch)
    pod = int(ch.get("ad_break_seconds") or 0)
    every = int(ch.get("ad_break_every", 15) or 0) * MINUTE
    key = (
        tuple((v["path"], v["duration"]) for v in vids),
        tuple((a["path"], a["duration"]) for a in ads),
        pod, every,
    )
    hit = _cache.get(ch["id"])
    if hit and hit[0] == key:
        return hit[1]

    breaks = bool(ads and pod > 0)
    segs = []
    ai = 0
    for v in vids:
        cuts = []
        if breaks and every > 0:
            t = every
            while v["duration"] - t >= MIN_TAIL:
                cuts.append(t)
                t += every
        bounds = [0.0] + cuts + [v["duration"]]
        for pi in range(len(bounds) - 1):
            segs.append({**v, "kind": "video", "part": pi, "src_start": bounds[pi],
                         "duration": bounds[pi + 1] - bounds[pi]})
            if breaks:
                podsegs, ai = _pod(ads, pod, ai)
                segs.extend(podsegs)
    t = 0.0
    starts = []
    for s in segs:
        s["start"] = t
        starts.append(t)
        t += s["duration"]
    tl = {"segments": segs, "starts": starts, "total": t,
          "adkey": (pod, every, tuple(a["path"] for a in ads))}
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
    """Guide entries. A video (all its parts, with the ad breaks between them) is one entry."""
    t = time.time() if t is None else t
    p = position(tl, ch, t)
    if not p:
        return []
    segs = tl["segments"]
    horizon = t + hours * 3600
    i = p["index"]
    cursor = t - p["offset"]
    out, cur, n = [], None, 0
    while cursor < horizon and n < 30000:
        seg = segs[i]
        end = cursor + seg["duration"]
        if seg["kind"] == "video" and seg.get("part", 0) == 0:
            cur = {"title": seg["title"], "start": cursor, "stop": end, "ad": False, "breaks": []}
            out.append(cur)
        elif cur is None:
            cur = {"title": "Ad break" if seg["kind"] == "ad" else seg["title"],
                   "start": cursor, "stop": end, "ad": seg["kind"] == "ad",
                   "breaks": [[cursor, end]] if seg["kind"] == "ad" else []}
            out.append(cur)
        else:
            if seg["kind"] == "ad":
                if cur["breaks"] and abs(cur["breaks"][-1][1] - cursor) < 0.01:
                    cur["breaks"][-1][1] = end
                else:
                    cur["breaks"].append([cursor, end])
            cur["stop"] = end
        cursor = end
        i = (i + 1) % len(segs)
        n += 1
    return out
