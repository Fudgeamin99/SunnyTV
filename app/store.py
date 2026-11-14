"""Tiny JSON config store. One file, one lock, atomic writes."""
import json
import os
import threading
import uuid
from contextlib import contextmanager

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
_lock = threading.RLock()


def _read():
    if not os.path.exists(CONFIG_PATH):
        return {"channels": [], "ad_groups": []}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("channels", [])
    cfg.setdefault("ad_groups", [])
    return cfg


def _write(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load():
    with _lock:
        return _read()


@contextmanager
def edit():
    """Load, yield for changes, save. If the body raises, nothing is saved."""
    with _lock:
        cfg = _read()
        yield cfg
        _write(cfg)


def new_id():
    return uuid.uuid4().hex[:8]


def find_channel(cfg, ch_id):
    for c in cfg["channels"]:
        if c["id"] == ch_id:
            return c
    return None


def find_channel_by_number(cfg, number):
    for c in cfg["channels"]:
        if c["number"] == number:
            return c
    return None


def find_group(cfg, group_id):
    for g in cfg["ad_groups"]:
        if g["id"] == group_id:
            return g
    return None


def update_channel(ch_id, **fields):
    with edit() as cfg:
        ch = find_channel(cfg, ch_id)
        if ch:
            ch.update(fields)
