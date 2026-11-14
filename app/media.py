"""Media library: scanning folders, probing durations (cached), background prober."""
import json
import os
import subprocess
import threading
import time

from . import store

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
CACHE_DIR = os.path.join(store.DATA_DIR, "cache")
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv", ".flv", ".mpg", ".mpeg",
             ".vob", ".m2ts", ".mts", ".3gp", ".ogv", ".divx"}

_dur_path = os.path.join(store.DATA_DIR, "durations.json")
_dur = {}
_dur_lock = threading.Lock()
_dirty = False
_kick = threading.Event()


def _load_durations():
    global _dur
    try:
        with open(_dur_path, "r", encoding="utf-8") as f:
            _dur = json.load(f)
    except Exception:
        _dur = {}


_load_durations()


def flush():
    global _dirty
    with _dur_lock:
        if not _dirty:
            return
        os.makedirs(store.DATA_DIR, exist_ok=True)
        tmp = _dur_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_dur, f)
        os.replace(tmp, _dur_path)
        _dirty = False


def kick():
    """Ask the background prober to run now."""
    _kick.set()


def abs_path(rel):
    """Resolve a path relative to the media folder, refusing anything that escapes it."""
    root = os.path.realpath(MEDIA_DIR)
    p = os.path.realpath(os.path.join(root, (rel or "").lstrip("/")))
    if p != root and not p.startswith(root + os.sep):
        raise ValueError("path is outside the media folder")
    return p


def rel_path(abs_p):
    return os.path.relpath(abs_p, os.path.realpath(MEDIA_DIR))


def title_from(path):
    return os.path.splitext(os.path.basename(path))[0]


def _key(path):
    st = os.stat(path)
    return f"{path}|{int(st.st_mtime)}|{st.st_size}"


def _ffprobe(path):
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
        return float(p.stdout.strip().splitlines()[0])
    except Exception:
        return 0.0


def is_cached(path):
    try:
        return _key(path) in _dur
    except OSError:
        return True  # missing file: nothing to probe


def get_duration(path, probe=False):
    """Seconds, or None if unknown. Only runs ffprobe when probe=True."""
    global _dirty
    try:
        key = _key(path)
    except OSError:
        return None
    with _dur_lock:
        if key in _dur:
            return _dur[key] or None
    if not probe:
        return None
    d = _ffprobe(path)
    with _dur_lock:
        _dur[key] = d
        _dirty = True
    return d or None


_lf = {}


def list_files(folder="", ttl=30):
    """Recursive list of video files (paths relative to the media folder)."""
    now = time.time()
    c = _lf.get(folder)
    if c and now - c[0] < ttl:
        return c[1]
    root = abs_path(folder)
    base = os.path.realpath(MEDIA_DIR)
    out = []
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if not d.startswith("."))
        for f in sorted(fn):
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() in VIDEO_EXT:
                out.append(os.path.relpath(os.path.join(dp, f), base))
    _lf[folder] = (now, out)
    return out


def browse(rel=""):
    """One folder level: sub folders and video files."""
    root = abs_path(rel)
    dirs, files = [], []
    if os.path.isdir(root):
        for e in sorted(os.scandir(root), key=lambda e: e.name.lower()):
            if e.name.startswith("."):
                continue
            if e.is_dir():
                dirs.append(e.name)
            elif os.path.splitext(e.name)[1].lower() in VIDEO_EXT:
                files.append({"name": e.name, "rel": rel_path(e.path), "duration": get_duration(e.path)})
    return {"path": rel_path(root) if rel else "", "dirs": dirs, "files": files,
            "mounted": os.path.isdir(os.path.realpath(MEDIA_DIR)), "root": MEDIA_DIR}


def all_dirs(max_depth=3):
    base = os.path.realpath(MEDIA_DIR)
    out = []
    for dp, dn, _ in os.walk(base):
        depth = dp[len(base):].count(os.sep)
        dn[:] = sorted(d for d in dn if not d.startswith("."))
        if depth >= max_depth:
            dn[:] = []
        if dp != base:
            out.append(os.path.relpath(dp, base))
    return out


def prober_loop():
    """Probe durations for every file a channel or advert group refers to."""
    while True:
        try:
            cfg = store.load()
            paths = set()
            for ch in cfg["channels"]:
                s = ch.get("source", {})
                if s.get("type") == "folder":
                    try:
                        for rel in list_files(s.get("folder", ""), ttl=15):
                            paths.add(abs_path(rel))
                    except ValueError:
                        pass
            for g in cfg["ad_groups"]:
                for rel in g.get("clips", []):
                    try:
                        paths.add(abs_path(rel))
                    except ValueError:
                        pass
            for p in sorted(paths):
                if not is_cached(p):
                    get_duration(p, probe=True)
            flush()
        except Exception as e:  # keep the thread alive
            print("prober error:", e, flush=True)
        _kick.wait(20)
        _kick.clear()
