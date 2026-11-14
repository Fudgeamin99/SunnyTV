"""Channel logos: one small image per channel, stored in /data/logos."""
import os
import time

from . import store

LOGO_DIR = os.path.join(store.DATA_DIR, "logos")
MAX_BYTES = 3 * 1024 * 1024


def sniff(data):
    """File type from the first bytes (never trust the upload's name or content type)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"GIF8":
        return "gif"
    return None


def path_for(ch):
    name = ch.get("logo")
    if not name or os.path.basename(name) != name:
        return None
    p = os.path.join(LOGO_DIR, name)
    return p if os.path.isfile(p) else None


def url_for(ch):
    return f"/logo/{ch['id']}?v={ch.get('logo_v', 0)}" if path_for(ch) else None


def remove_files(ch_id):
    if os.path.isdir(LOGO_DIR):
        for f in os.listdir(LOGO_DIR):
            if f.split(".")[0] == ch_id:
                try:
                    os.remove(os.path.join(LOGO_DIR, f))
                except OSError:
                    pass


def save(ch_id, data):
    ext = sniff(data)
    if not ext:
        raise ValueError("Use a PNG, JPG, WEBP or GIF image")
    if len(data) > MAX_BYTES:
        raise ValueError("That image is over 3 MB. Please use a smaller one")
    os.makedirs(LOGO_DIR, exist_ok=True)
    remove_files(ch_id)
    name = f"{ch_id}.{ext}"
    tmp = os.path.join(LOGO_DIR, name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(LOGO_DIR, name))
    return name, int(time.time())
