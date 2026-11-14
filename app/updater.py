"""In-app updater: pull the latest app code (the app/ folder) from GitHub.

New code is saved to /data/code/app. On the next start the entrypoint picks it
instead of the code baked into the image, and falls back to the image code if the
new code fails to load. Changes to the Dockerfile or requirements are not applied
this way: those need a new image.
"""
import hashlib
import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REPO = os.environ.get("UPDATE_REPO", "Fudgeamin99/SunnyTV")
BRANCH = os.environ.get("UPDATE_BRANCH", "main")
TOKEN = os.environ.get("GITHUB_TOKEN", "")  # only needed if the repo is private
DATA_DIR = os.environ.get("DATA_DIR", "/data")
CODE_ROOT = os.path.join(DATA_DIR, "code")
IMAGE_DIR = os.environ.get("IMAGE_APP_DIR", "/srv/app")
BUILD_META = os.environ.get("BUILD_META_DIR", "/srv/build-meta")
RUNNING_DIR = os.path.dirname(os.path.abspath(__file__))
META_FILES = ("Dockerfile", "requirements.txt", "docker-entrypoint.sh")
MAX_BYTES = 50 * 1024 * 1024


class UpdateError(Exception):
    pass


def hash_tree(root):
    h = hashlib.sha256()
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d != "__pycache__")
        for f in sorted(fn):
            if f.endswith(".pyc"):
                continue
            p = os.path.join(dp, f)
            h.update(os.path.relpath(p, root).replace(os.sep, "/").encode() + b"\0")
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def _get(url, accept=None):
    headers = {"User-Agent": "sunny-tv-updater"}
    if accept:
        headers["Accept"] = accept
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(MAX_BYTES + 1)
    except Exception as e:
        raise UpdateError(f"Could not reach GitHub ({e}). Is the repo public, or GITHUB_TOKEN set?")
    if len(data) > MAX_BYTES:
        raise UpdateError("Download too large")
    return data


def fetch_tarball():
    if TOKEN:
        url = f"https://api.github.com/repos/{REPO}/tarball/{BRANCH}"
    else:
        url = f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{BRANCH}"
    return _get(url)


def latest_commit():
    try:
        j = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                            "application/vnd.github+json"))
        return {"sha": j["sha"][:7], "message": (j["commit"]["message"] or "").splitlines()[0],
                "date": j["commit"]["committer"]["date"]}
    except Exception:
        return None


def download_source(dest):
    """Unpack only app/ and the build files from the repo tarball into dest."""
    data = fetch_tarball()
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except Exception:
        raise UpdateError("GitHub sent something that is not a tarball")
    with tf:
        for m in tf.getmembers():
            if not m.isfile() or "/" not in m.name:
                continue
            rel = m.name.split("/", 1)[1]
            if not (rel.startswith("app/") or rel in META_FILES):
                continue
            target = os.path.realpath(os.path.join(dest, rel))
            if not target.startswith(os.path.realpath(dest) + os.sep):
                raise UpdateError("Unsafe path in download")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                out.write(tf.extractfile(m).read())
    if not os.path.exists(os.path.join(dest, "app", "main.py")):
        raise UpdateError("The repo has no app/main.py on that branch")


def needs_rebuild(src):
    if not os.path.isdir(BUILD_META):
        return False
    for f in META_FILES:
        a, b = os.path.join(BUILD_META, f), os.path.join(src, f)
        if os.path.exists(a) and os.path.exists(b):
            with open(a, "rb") as x, open(b, "rb") as y:
                if x.read() != y.read():
                    return True
    return False


def check():
    with tempfile.TemporaryDirectory() as td:
        download_source(td)
        new = hash_tree(os.path.join(td, "app"))
        cur = hash_tree(RUNNING_DIR)
        return {"available": new != cur, "needs_rebuild": needs_rebuild(td),
                "current": cur[:8], "new": new[:8], "latest": latest_commit()}


def apply():
    with tempfile.TemporaryDirectory() as td:
        download_source(td)
        if needs_rebuild(td):
            raise UpdateError("This update changes the Docker image (Dockerfile or requirements). "
                              "Pull the newest image on your NAS instead.")
        src = os.path.join(td, "app")
        for dp, _, fn in os.walk(src):
            for f in fn:
                if f.endswith(".py"):
                    try:
                        py_compile.compile(os.path.join(dp, f), cfile=os.path.join(td, "chk.pyc"), doraise=True)
                    except py_compile.PyCompileError as e:
                        raise UpdateError(f"Downloaded code has an error, not installing it: {e.msg[:200]}")
        new_hash = hash_tree(src)
        os.makedirs(CODE_ROOT, exist_ok=True)
        staging = os.path.join(CODE_ROOT, "app.new")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(src, staging, ignore=shutil.ignore_patterns("__pycache__"))
        live, prev = os.path.join(CODE_ROOT, "app"), os.path.join(CODE_ROOT, "app.prev")
        shutil.rmtree(prev, ignore_errors=True)
        if os.path.exists(live):
            os.rename(live, prev)
        os.rename(staging, live)
        if os.path.isdir(IMAGE_DIR):
            with open(os.path.join(CODE_ROOT, "base_hash"), "w") as f:
                f.write(hash_tree(IMAGE_DIR))
        return {"ok": True, "new": new_hash[:8]}


def _park(live, suffix):
    dest = os.path.join(CODE_ROOT, "app." + suffix)
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(live, dest)


def choose_code_dir():
    """Called by the entrypoint: which folder should the app start from?"""
    image_root = os.path.dirname(IMAGE_DIR)
    live = os.path.join(CODE_ROOT, "app")
    if not os.path.isdir(live):
        return image_root
    try:
        with open(os.path.join(CODE_ROOT, "base_hash")) as f:
            base = f.read().strip()
    except OSError:
        base = ""
    if base != hash_tree(IMAGE_DIR):
        print("Image was rebuilt, using the code in the image", file=sys.stderr)
        _park(live, "stale")
        return image_root
    r = subprocess.run([sys.executable, "-c", "import app.main"], cwd=CODE_ROOT,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print("Updated code failed to load, falling back to the image code:\n" + r.stderr[-600:], file=sys.stderr)
        _park(live, "broken")
        return image_root
    return CODE_ROOT
