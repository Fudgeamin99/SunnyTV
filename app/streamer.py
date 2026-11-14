"""Per-channel FFmpeg runner that produces a live HLS stream.

A runner starts when someone asks for a channel and stops after IDLE_STOP_SECONDS
with no viewers. It plays whatever the schedule says is on right now, seeking to
the right offset, and moves on to the next item when one ends.
"""
import asyncio
import os
import shutil
import subprocess
import threading
import time

from . import logos, schedule, store

HLS_DIR = os.path.join(store.DATA_DIR, "hls")
LOG_DIR = os.path.join(store.DATA_DIR, "logs")
IDLE = int(os.environ.get("IDLE_STOP_SECONDS", "45"))
RESOLUTION = os.environ.get("RESOLUTION", "1280x720")
VIDEO_BITRATE = os.environ.get("VIDEO_BITRATE", "3000k")
PRESET = os.environ.get("X264_PRESET", "veryfast")

_runners = {}
_rlock = threading.Lock()


def out_dir(ch_id):
    return os.path.join(HLS_DIR, ch_id)


HWACCEL = os.environ.get("HWACCEL", "none").lower()                 # none | vaapi
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")
_hw_ok = {"v": HWACCEL == "vaapi" and os.path.exists(VAAPI_DEVICE)}
if HWACCEL == "vaapi" and not _hw_ok["v"]:
    print(f"HWACCEL=vaapi but {VAAPI_DEVICE} was not found: using CPU encoding. "
          "Map /dev/dri into the container to use the GPU.", flush=True)


def hw_active():
    return _hw_ok["v"]


def encoder_info():
    return {"mode": "vaapi" if hw_active() else "cpu", "requested": HWACCEL,
            "device": VAAPI_DEVICE, "device_found": os.path.exists(VAAPI_DEVICE)}


def _sub_filter(subs, offset):
    """Burn subtitles into the picture. Input seeking restarts timestamps at 0, so shift them
    back to the position in the source file while the subtitle filter runs."""
    esc = subs.replace("\\", "/").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")
    style = "FontName=DejaVu Sans,FontSize=18,Bold=1,Outline=2,Shadow=0,MarginV=24"
    return (f"setpts=PTS+{offset:.3f}/TB,subtitles=filename='{esc}':force_style='{style}',"
            "setpts=PTS-STARTPTS,")


SLATE = os.path.join(os.path.dirname(__file__), "static", "offline.png")
SLATE_SECONDS = 20


def _encode_args():
    """(input options, video encoder options) for the current encoder."""
    if hw_active():   # decode on the CPU (works for every format), encode on the GPU
        return (["-vaapi_device", VAAPI_DEVICE],
                ["-c:v", "h264_vaapi", "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE,
                 "-bufsize", "6000k", "-g", "60", "-bf", "0"])
    return ([], ["-c:v", "libx264", "-preset", PRESET,
                 "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE, "-bufsize", "6000k",
                 "-g", "60", "-keyint_min", "60", "-sc_threshold", "0"])


def build_slate_cmd(out, logo=None, secs=SLATE_SECONDS):
    """The "offline" picture (with the channel's logo on top, if it has one) as a live stream."""
    w, h = RESOLUTION.split("x")
    w, h = int(w), int(h)
    pre, venc = _encode_args()
    tail = "format=nv12,hwupload" if hw_active() else "format=yuv420p"
    ins = ["-re", "-loop", "1", "-framerate", "30", "-t", str(secs), "-i", SLATE]
    bg = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if logo:
        ins += ["-loop", "1", "-framerate", "30", "-t", str(secs), "-i", logo]
        fc = (f"[0:v]{bg}[bg];[1:v]scale={int(w * 0.3) // 2 * 2}:{int(h * 0.115) // 2 * 2}"
              f":force_original_aspect_ratio=decrease,format=rgba[lg];"
              f"[bg][lg]overlay=(W-w)/2:{int(h * 0.05)}:format=auto,fps=30,{tail}[v]")
        vmap, aidx = ["-filter_complex", fc, "-map", "[v]"], 2
    else:
        vmap, aidx = ["-vf", f"{bg},fps=30,{tail}", "-map", "0:v:0"], 1
    ins += ["-f", "lavfi", "-t", str(secs), "-i", "anullsrc=r=48000:cl=stereo"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", *pre, *ins,
        *vmap, "-map", f"{aidx}:a:0", *venc,
        "-c:a", "aac", "-b:a", "64k", "-ar", "48000", "-ac", "2",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "10",
        "-hls_flags", "delete_segments+append_list+omit_endlist+discont_start",
        "-hls_segment_filename", os.path.join(out, "seg_%06d.ts"),
        os.path.join(out, "index.m3u8"),
    ]


def build_cmd(path, offset, remaining, out, subs=None):
    w, h = RESOLUTION.split("x")
    hw = hw_active()
    # Fix non-square pixels (DVD/VOB) and interlacing first, then fit into the frame.
    vf = ("scale=trunc(iw*sar/2)*2:ih,setsar=1,yadif=deint=interlaced,"
          f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          + (_sub_filter(subs, offset) if subs else "")
          + "fps=30,"
          + ("format=nv12,hwupload" if hw else "format=yuv420p"))
    if hw:   # decode on the CPU (works for every format), encode on the GPU
        pre = ["-vaapi_device", VAAPI_DEVICE]
        venc = ["-c:v", "h264_vaapi", "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE,
                "-bufsize", "6000k", "-g", "60", "-bf", "0"]
    else:
        pre = []
        venc = ["-c:v", "libx264", "-preset", PRESET,
                "-b:v", VIDEO_BITRATE, "-maxrate", VIDEO_BITRATE, "-bufsize", "6000k",
                "-g", "60", "-keyint_min", "60", "-sc_threshold", "0"]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", *pre,
        "-re", "-ss", f"{offset:.3f}", "-i", path, "-t", f"{remaining:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, *venc,
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-af", "aresample=async=1",
        "-f", "hls", "-hls_time", "2", "-hls_list_size", "10",
        "-hls_flags", "delete_segments+append_list+omit_endlist+discont_start",
        "-hls_segment_filename", os.path.join(out, "seg_%06d.ts"),
        os.path.join(out, "index.m3u8"),
    ]


class Runner:
    def __init__(self, ch_id, always=False):
        self.ch_id = ch_id
        self.always = always      # 24/7 channels never stop for lack of viewers
        self.last_hit = time.time()
        self.stop_evt = threading.Event()
        self.proc = None
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"runner-{ch_id}")

    def touch(self):
        self.last_hit = time.time()

    def alive(self):
        return self.thread.is_alive() and not self.stop_evt.is_set()

    def stop(self):
        self.stop_evt.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()

    def _idle(self):
        return (not self.always) and time.time() - self.last_hit > IDLE

    def _playable(self):
        """Is the channel on, with something scheduled? (re-read from the config)"""
        ch = store.find_channel(store.load(), self.ch_id)
        if not ch or not ch.get("enabled", True):
            return False
        return bool(schedule.position(schedule.build_timeline(store.load(), ch), ch))

    def _slate(self, ch, out):
        """Play the "offline" picture for a short stretch, then let the main loop re-check."""
        os.makedirs(LOG_DIR, exist_ok=True)
        log = open(os.path.join(LOG_DIR, f"{ch['number']}.log"), "ab")
        started, last_check = time.time(), time.time()
        try:
            self.proc = subprocess.Popen(build_slate_cmd(out, logos.path_for(ch)),
                                         stdout=subprocess.DEVNULL, stderr=log)
            while self.proc.poll() is None:
                if self.stop_evt.is_set() or self._idle():
                    self.proc.terminate()
                    try:
                        self.proc.wait(5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                    break
                time.sleep(0.5)
                if time.time() - last_check > 3:
                    last_check = time.time()
                    if self._playable():
                        self.proc.terminate()   # switched back on / playlist ready: go to the real thing
                        break
        finally:
            log.close()
        rc = self.proc.returncode
        if (hw_active() and time.time() - started < 8 and rc not in (0, None, -15, -9)
                and not self.stop_evt.is_set()):
            _hw_ok["v"] = False
            print(f"GPU encoding failed (ffmpeg exit {rc}); switching to CPU encoding.", flush=True)
        elif time.time() - started < 2:
            self.stop_evt.wait(3)       # failing fast: do not spin

    def _run(self):
        out = out_dir(self.ch_id)
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            while not self.stop_evt.is_set() and not self._idle():
                cfg = store.load()
                ch = store.find_channel(cfg, self.ch_id)
                if not ch:
                    break
                tl = schedule.build_timeline(cfg, ch)
                pos = schedule.position(tl, ch) if ch.get("enabled", True) else None
                if not pos:
                    self._slate(ch, out)     # switched off, or nothing to play yet: show the offline picture
                    continue
                rem = pos["remaining"]
                if rem < 1.0:
                    self.stop_evt.wait(max(rem, 0.2))
                    continue
                path = pos["segment"]["path"]
                if not os.path.exists(path):
                    self.stop_evt.wait(2)
                    continue
                started = time.time()
                last_check = started
                adkey = tl["adkey"]
                log = open(os.path.join(LOG_DIR, f"{ch['number']}.log"), "ab")
                try:
                    self.proc = subprocess.Popen(
                        build_cmd(path, pos["segment"].get("src_start", 0.0) + pos["offset"], rem, out,
                                  pos["segment"].get("subs")),
                        stdout=subprocess.DEVNULL, stderr=log)
                    while self.proc.poll() is None:
                        if self.stop_evt.is_set() or self._idle():
                            self.proc.terminate()
                            try:
                                self.proc.wait(5)
                            except subprocess.TimeoutExpired:
                                self.proc.kill()
                            break
                        time.sleep(0.5)
                        if time.time() - last_check > 3:
                            last_check = time.time()
                            c2 = store.load()
                            ch2 = store.find_channel(c2, self.ch_id)
                            if not ch2 or schedule.build_timeline(c2, ch2)["adkey"] != adkey:
                                self.proc.terminate()   # ad settings changed: re-plan from the new schedule
                finally:
                    log.close()
                rc = self.proc.returncode
                if (hw_active() and time.time() - started < 8 and rc not in (0, None, -15, -9)
                        and not self.stop_evt.is_set()):
                    _hw_ok["v"] = False
                    print(f"GPU encoding failed (ffmpeg exit {rc}); switching to CPU encoding. "
                          f"See data/logs/{ch['number']}.log", flush=True)
                    continue
                if time.time() - started < 2 and rem > 5:
                    self.stop_evt.wait(5)  # failing fast: do not spin
        finally:
            self.stop_evt.set()


def ensure(ch_id, always=None):
    with _rlock:
        r = _runners.get(ch_id)
        if r and r.alive():
            r.touch()
            if always is not None:
                r.always = always
            return r
        r = Runner(ch_id, bool(always))
        _runners[ch_id] = r
        r.thread.start()
        return r


def is_live(ch_id):
    r = _runners.get(ch_id)
    return bool(r and r.alive())


def release(ch_id):
    """Turn off 24/7 for a running channel: it now stops when nobody is watching."""
    r = _runners.get(ch_id)
    if r:
        r.always = False
        r.touch()


def keepalive_once():
    cfg = store.load()
    for ch in cfg["channels"]:
        r = _runners.get(ch["id"])
        if not ch.get("enabled", True):
            if r and r.alive() and r.always:
                release(ch["id"])       # keep the offline slate only while someone is watching
        elif ch.get("always_on", False):
            ensure(ch["id"], always=True)
        elif r and r.alive() and r.always:
            release(ch["id"])


def keepalive_loop():
    """Every 10s: start 24/7 channels, restart ones that died, stop channels that were turned off."""
    while True:
        try:
            keepalive_once()
        except Exception as e:
            print("keepalive error:", e, flush=True)
        time.sleep(10)


def stop(ch_id):
    r = _runners.get(ch_id)
    if r:
        r.stop()
    shutil.rmtree(out_dir(ch_id), ignore_errors=True)


async def wait_ready(ch_id, timeout=30):
    """Wait until the playlist exists and lists at least one segment."""
    path = os.path.join(out_dir(ch_id), "index.m3u8")
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(path, "r") as f:
                if ".ts" in f.read():
                    return True
        except OSError:
            pass
        r = _runners.get(ch_id)
        if not r or not r.alive():
            return False
        await asyncio.sleep(0.5)
    return False
