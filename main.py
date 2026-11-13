import array
import asyncio
import random
import colorsys
import glob
import json
import math
import os
import re
import signal
import socket
import subprocess
import time

import decky

LED_DIR = "/sys/class/leds"
PS_DIR = "/sys/class/power_supply"
SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
# LED state from before BETTR LED took over; on disk so a crash/restart can still restore it.
SNAPSHOT_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "led_snapshot.json")

# While charging the sticks show a steady color by battery level, at a low brightness.
# Colors are given as the actual LED output mix (not gamma-corrected): at 2% the LEDs
# only have ~5 steps, and orange needs a real bit of green to not look like red.
CHARGE_BRIGHTNESS = 0.03     # charging breathes from 0% up to this (3%)
CHARGE_LOW_PCT = 10          # below this: red
CHARGE_HIGH_PCT = 90         # this and above: green; in between: orange
CHARGE_OUTPUT_RED = (255, 0, 0)
CHARGE_OUTPUT_ORANGE = (255, 50, 0)  # low green share: more green read as yellow at low brightness
CHARGE_OUTPUT_GREEN = (0, 255, 0)
CHARGE_COLOR = CHARGE_OUTPUT_ORANGE  # kept for older code paths
# LED React: the game's sound drives the sticks - loudness pulses the brightness and
# sudden hits flash, each stick following its own stereo side.
REACT_PEAK = 0.12            # flashes / loudest moments top out at 12%
REACT_REST = 0.2             # resting glow, as a share of REACT_PEAK (2.4%)
REACT_PULSE = 0.85           # how much loudness lifts the brightness (0..1); flashes still reach the full peak
REACT_FLASH_WHITE = 0.35     # how much whiter a flash gets
REACT_FLASH_SHARE = 0.6      # a hit must reach this share of the loudest recent sound to flash
REACT_FLASH_MIN = 0.17      # ...and be at least this loud outright (0..1 of full scale)
REACT_RATE = 8000            # audio sample rate we listen at (plenty for loudness/bass)
REACT_CHUNK = REACT_RATE // 50  # samples per side per update (20ms pieces, 50 updates/s)
REACT_MAX_BACKLOG = 3        # if more pieces than this are waiting, skip to the newest
REACT_MAX_UPDATES = 40       # LED writes per second cap in LED React (eases the LED chip's bus)
# Ambilight: the sticks take the main color shown on screen (left stick = left half,
# right stick = right half). Screenshots come from gamescope itself (gamescopectl), the
# method Armada's own ambilight used safely; ffmpeg shrinks each to a tiny image.
AMBI_FAST = 0.5              # seconds between screenshots while the screen's colors change
AMBI_SLOW = 2.0              # ...stretching up to this while they stay the same (saves power)
AMBI_SAME = 24               # color change (0-255 per channel) that counts as "the same"
AMBI_W, AMBI_H = 32, 18      # size the screenshot is shrunk to before picking a color
AMBI_GLIDE = 0.12            # how far the LED color moves toward the screen color per frame
SCREEN = "screen"            # marks "color comes from the screen" in a look
RUN_USER_DIR = "/run/user"   # where the session user's runtime folder lives

# LED Rain Mode: white raindrops landing on random stick zones (home screen only).
RAIN_PEAK = 0.08             # brightest a drop gets (8%)
RAIN_DROPS_PER_SEC = 3.2     # average new drops per stick per second
RAIN_FADE = 0.55             # seconds for a drop to fade out
RAIN_FPS = 30
# Home screen light (Rain Mode off): BETT-R LED drives the sticks instead of Armada.
HOME_BRIGHTNESS = 0.05       # 5%, like a new game color
HOME_HUE = 30                # default home color
# A game with no color picked yet: the sticks blink white while its menu is open.
FLASH_PERIOD = 1.0           # seconds per on/off blink
# Armada may rewrite the sticks right after a wake or a switch (e.g. with its LEDs
# switched off it writes them dark). Check at these times after either and put ours back.
RECHECK_AFTER = (1.0, 3.0)   # seconds
# Unplug "dance": a white dot spins around each stick before handing back.
UNPLUG_DANCE_SECONDS = 2.0
UNPLUG_DANCE_BRIGHTNESS = 0.02        # 2%
UNPLUG_DANCE_SPEED = 6.0              # zones per second
PERIOD = 4.0                 # seconds per breath
USE_FADES = True             # False = jump straight to each look instead of fading
FADE = 0.8                   # seconds to cross-fade into / out of a game color
CHARGE_FADE = 0.15           # plugging in: drop the current color to dark almost at once
                             # (lingering on Armada's full color reads as a flash next to the dim breath)
FADE_FPS = 40                # fade updates per second: smooth, but slow enough for the
                             # LED chip to finish each update (faster made zones shimmer)
FPS = 100                    # high frame rate so dithering looks like smooth in-between levels
POLL = 0.5                   # seconds between checks while idle / solid (how fast plug-in is noticed)
PREVIEW_TIMEOUT = 10.0       # preview ends this long after the menu stops refreshing it
GAME_MODE_TIMEOUT = 15.0     # no check-in from the Steam UI for this long = left game mode
CLOSING_HOLD = 4.0           # after a game quits, ignore it for this long (Steam may still list it)
RESTORE_CHECKS = 5           # after handing back, re-check this many times (1s apart)

# Armada OS stick lights (Retroid / AYN): 4 zones per stick, each zone a separate
# r/g/b LED (l:r1 .. r:b4), normally drawn by Armada's own LED service.
ARMADA_SERVICE = "armada-stick-led"
# Armada creates this right before sleep (both its fake and real suspend). With real
# suspend the processor stops ~0.6s later and the sticks keep whatever was last written.
ARMADA_SUSPEND_FLAG = "/run/armada/fake-suspend.active"
ARMADA_GAMMA = 2.2  # same correction Armada applies, so colors look the same as in Armada Control
# Armada's own stick LED tool (what Armada Control uses). Writing through its code
# means we drive the lights exactly the way Armada does on whatever version is installed.
ARMADA_LED_TOOL = "/usr/libexec/armada/stick-led-color"


def load_armada_tool():
    """Load Armada's stick LED tool as a module; None if it can't be used."""
    try:
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("armada_stick_led_color", ARMADA_LED_TOOL)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod if callable(getattr(mod, "write_zone", None)) else None
    except BaseException:  # includes SystemExit if the tool runs itself on import
        return None
STICK_LED = re.compile(r"^[lr]:[rgb][1-4]$")
# Newer Armada releases may draw the sticks with a differently named program, so
# also look for whatever is running that looks like an LED/lighting daemon...
LED_WRITER = re.compile(r"stick[-_]?led|led[-_]?colou?r|led[-_]?(daemon|ctl|control)|rgb|lighting", re.I)
# ...but never pause anything that input, the UI or the system depends on.
NEVER_PAUSE = re.compile(r"inputplumber|hhd|steam|gamescope|plugin_loader|pluginloader|systemd|dbus|bettr|decky", re.I)

# LEDs that aren't the device's lights (keyboard locks, storage, radios, ...)
SKIP = re.compile(r"capslock|numlock|scrolllock|kana|compose|mmc|phy|wlan|bluetooth|hci|^input")



def breath(phase):
    """Plain sine breath for phase 0..1. Not squared: at a few % the LEDs only have
    ~a dozen brightness steps, and lingering at the dark end (where each step is
    the biggest visible jump) made the stepping obvious. This walks through the
    steps at an even, steady pace."""
    return (1 - math.cos(2 * math.pi * phase)) / 2


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


LAST_WRITE_ERROR = [""]  # last failed LED write, shown in the menu's status


def _write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
    except OSError as e:
        LAST_WRITE_ERROR[0] = f"{os.path.basename(os.path.dirname(path))}: {e.strerror or e}"


def find_battery():
    try:
        names = sorted(os.listdir(PS_DIR))
    except OSError:
        return None
    for n in names:
        path = os.path.join(PS_DIR, n)
        if _read(os.path.join(path, "type")) == "Battery":
            return path
    return None


class Led:
    def __init__(self, name):
        self.name = name
        self.path = os.path.join(LED_DIR, name)
        self.max = int(_read(self._f("max_brightness"), "255") or 255)
        if os.path.exists(self._f("multi_index")):
            self.kind = "multi"
        elif "red" in name:
            self.kind = "red"
        elif "green" in name:
            self.kind = "green"
        elif "blue" in name:
            self.kind = "blue"
        else:
            self.kind = "mono"
        self.color = (255, 255, 255)
        self.last = None

    def _f(self, attr):
        return os.path.join(self.path, attr)

    def _trigger(self):
        m = re.search(r"\[([^\]]+)\]", _read(self._f("trigger")))
        return m.group(1) if m else "none"

    # Settings some kernel triggers keep in their own files (timer, pattern, ...).
    TRIGGER_FILES = ("delay_on", "delay_off", "pattern", "hw_pattern", "repeat", "invert")

    def snapshot(self):
        return {
            "trigger": self._trigger(),
            "brightness": _read(self._f("brightness")),
            "multi_intensity": _read(self._f("multi_intensity"), None),
            "trigger_files": {
                f: _read(self._f(f), None) for f in self.TRIGGER_FILES if os.path.exists(self._f(f))
            },
        }

    def apply(self, state):
        """Put the LED back to a snapshot, writing only what differs.
        Order matters: the trigger first (changing it can reset brightness),
        then the color, then brightness last so it's what sticks."""
        self.last = None
        if not state:
            return
        trigger = state.get("trigger") or "none"
        if self._trigger() != trigger:
            _write(self._f("trigger"), trigger)
            # A trigger's own settings files only exist once it's set again.
            for f, v in (state.get("trigger_files") or {}).items():
                if v not in (None, "") and os.path.exists(self._f(f)):
                    _write(self._f(f), v)
        mi = state.get("multi_intensity")
        if mi is not None and _read(self._f("multi_intensity")) != mi:
            _write(self._f("multi_intensity"), mi)
        # With a kernel trigger running, the trigger owns brightness.
        if trigger == "none" and state.get("brightness", "") != "":
            if _read(self._f("brightness")) != state["brightness"]:
                _write(self._f("brightness"), state["brightness"])

    def set_color(self, color):
        self.color = color
        self.last = None
        _write(self._f("trigger"), "none")
        if self.kind == "multi":
            # Keep brightness at max and do all dimming in multi_intensity, so each
            # channel is rounded once (brightness * intensity truncates twice and
            # makes the color jump between steps). Zero the color first so raising
            # brightness can't light the LED up at full strength in between.
            n = len(_read(self._f("multi_index")).split())
            _write(self._f("multi_intensity"), " ".join(["0"] * n))
            _write(self._f("brightness"), self.max)

    def set_level(self, level):
        r, g, b = self.color
        if self.kind == "multi":
            channels = {"red": r, "green": g, "blue": b}
            value = " ".join(
                str(round(level * channels.get(c, 0) * self.max / 255))
                for c in _read(self._f("multi_index")).split()
            )
            attr = "multi_intensity"
        else:
            scale = {"red": r, "green": g, "blue": b}.get(self.kind, 255) / 255
            value = str(round(level * scale * self.max))
            attr = "brightness"
        if value != self.last:  # only write when the visible value changes
            _write(self._f(attr), value)
            self.last = value


def _systemctl(*args):
    # Decky's bundled Python sets LD_LIBRARY_PATH, which breaks system binaries.
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    try:
        return subprocess.run(["systemctl", *args], env=env, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None


def _unit_of(pid):
    """The systemd service a process belongs to, if it's its own service."""
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        part = line.rsplit("/", 1)[-1]
        if part.endswith(".service") and not NEVER_PAUSE.search(part):
            return part
    return None


def dominant_color(px, x0, x1):
    """The main color of part of a tiny RGB image: the hue with the most vivid
    pixels (near-black and grey/white areas like letterboxing, menus and HUD text
    barely count), returned at full strength. None if the area is basically black."""
    bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(24)]
    total = [0.0, 0.0, 0.0, 0.0]
    for y in range(AMBI_H):
        row = y * AMBI_W * 3
        for x in range(x0, x1):
            r, g, b = px[row + x * 3], px[row + x * 3 + 1], px[row + x * 3 + 2]
            h, sat, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            total[0] += v
            total[1] += r * v
            total[2] += g * v
            total[3] += b * v
            if v < 0.08 or sat < 0.2:
                continue
            w = sat * sat * v
            bn = bins[int(h * 24) % 24]
            bn[0] += w
            bn[1] += r * w
            bn[2] += g * w
            bn[3] += b * w
    i = max(range(24), key=lambda k: bins[k][0] + 0.5 * (bins[k - 1][0] + bins[(k + 1) % 24][0]))
    pick = [sum(bins[k][j] for k in (i - 1, i, (i + 1) % 24)) for j in range(4)]
    if pick[0] > 0.3:
        col = [pick[1] / pick[0], pick[2] / pick[0], pick[3] / pick[0]]
    elif total[0] > 0.5:
        col = [total[1] / total[0], total[2] / total[0], total[3] / total[0]]  # grey/white scene
    else:
        return None
    m = max(col) or 1
    return tuple(c * 255 / m for c in col)


def _screen_sizes():
    """Likely (width, height) of gamescope's output, from the display's modes,
    landscape first."""
    sizes = []
    for d in sorted(glob.glob("/sys/class/drm/card*-*")):
        if _read(os.path.join(d, "status")) != "connected":
            continue
        m = re.match(r"(\d+)x(\d+)", _read(os.path.join(d, "modes")))
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            for size in ((max(w, h), min(w, h)), (min(w, h), max(w, h))):
                if size not in sizes:
                    sizes.append(size)
    return sizes


def nv12_to_tiny(data, sizes):
    """A raw NV12 frame from gamescope (no PNG) shrunk to AMBI_W x AMBI_H rgb24 by
    reading a few pixels per cell, or None if its layout isn't one we can be sure of
    (then the PNG way is used instead). Only exact size matches are trusted, so a
    padded or unexpected layout can't produce wrong colors."""
    n = len(data)
    for w, h in sizes:
        for align in (1, 16, 32, 64, 128, 256, 512):
            pitch = -(-w // align) * align
            if pitch * h + pitch * ((h + 1) // 2) == n:
                break
        else:
            continue
        uv0 = pitch * h
        out = bytearray()
        for cy in range(AMBI_H):
            for cx in range(AMBI_W):
                r = g = b = 0.0
                for sy in (0.25, 0.75):
                    for sx in (0.25, 0.75):
                        x = int((cx + sx) * w / AMBI_W)
                        y = int((cy + sy) * h / AMBI_H)
                        yy = data[y * pitch + x]
                        c = uv0 + (y // 2) * pitch + (x // 2) * 2
                        u, v = data[c] - 128, data[c + 1] - 128
                        yv = 1.164 * (yy - 16)  # BT.709, limited range
                        r += yv + 1.793 * v
                        g += yv - 0.213 * u - 0.533 * v
                        b += yv + 2.112 * u
                out += bytes(max(0, min(255, round(c / 4))) for c in (r, g, b))
        return bytes(out)
    return None


def _stop_proc(proc):
    if proc.returncode is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


class ScreenSampler:
    """Takes a few screenshots a second through gamescope and keeps each stick's
    target color. Screenshots are shrunk, read for a color and deleted at once."""

    def __init__(self):
        self.task = None
        self.source = ""
        self.error = ""
        self.target = [(255, 255, 255), (255, 255, 255)]
        self.current = [[255.0, 255.0, 255.0], [255.0, 255.0, 255.0]]
        self.raw_ok = None   # gamescope can give raw frames (None = not tried yet)
        self.sizes = None

    def running(self):
        return self.task is not None and not self.task.done()

    def start(self):
        if not self.running():
            self.task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None

    def colors(self):
        """Each stick's color, gliding smoothly toward the latest screen color."""
        for i in (0, 1):
            self.current[i] = [c + AMBI_GLIDE * (t - c) for c, t in zip(self.current[i], self.target[i])]
        return [tuple(c) for c in self.current]

    @staticmethod
    def _display(runtime):
        try:
            names = sorted(os.listdir(runtime))
        except OSError:
            names = []
        for n in names:
            if re.fullmatch(r"gamescope-\d+", n):
                return n
        return "gamescope-0"

    async def _run(self):
        uid, gid, home = AudioMeter._session_user()
        runtime = os.path.join(RUN_USER_DIR, str(uid))
        # /run/user/<uid> is in memory (tmpfs), so screenshots never touch storage.
        shot = os.path.join(runtime, ".bettr-led-screen.png")
        raw = os.path.join(runtime, ".bettr-led-screen.nv12.bin")
        as_user = {}
        if os.getuid() == 0 and uid != 0:
            as_user = {"user": uid, "group": gid, "extra_groups": []}
        gap = AMBI_FAST
        while True:
            began = time.monotonic()
            before = list(self.target)
            try:
                await self._capture(runtime, shot, raw, home, as_user)
                self.error = ""
            except asyncio.CancelledError:
                raise
            except FileNotFoundError as e:
                self.error = f"needs {os.path.basename(str(e.filename or 'gamescopectl/ffmpeg'))}"
            except Exception as e:
                self.error = str(e)[:120]
            finally:
                for f in (shot, raw):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            # Same colors as last time: wait longer before the next screenshot.
            same = all(abs(a - b) < AMBI_SAME for t0, t1 in zip(before, self.target) for a, b in zip(t0, t1))
            gap = min(AMBI_SLOW, gap * 1.5) if same else AMBI_FAST
            await asyncio.sleep(max(0.05, gap - (time.monotonic() - began)))

    async def _capture(self, runtime, shot, raw, home, as_user):
        display = self._display(runtime)
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        env.update({"XDG_RUNTIME_DIR": runtime, "WAYLAND_DISPLAY": display,
                    "GAMESCOPE_WAYLAND_DISPLAY": display, "HOME": home})
        if self.raw_ok is not False:
            # Raw frame: gamescope skips the PNG compression and ffmpeg isn't needed.
            # Older gamescope can't do this; then it's PNG from here on.
            if self.sizes is None:
                self.sizes = _screen_sizes()
            try:
                got = await self._shoot(raw, env, as_user, 2 if self.raw_ok else 0.6)
            except RuntimeError:
                got = False
            if got:
                with open(raw, "rb") as f:
                    px = nv12_to_tiny(f.read(), self.sizes)
                if px is not None:
                    self.raw_ok, self.source = True, "gamescope raw frames"
                    self._pick(px)
                    return
            if self.raw_ok:
                raise RuntimeError("raw frame never arrived")
            self.raw_ok = False
        if not await self._shoot(shot, env, as_user, 2):
            raise RuntimeError("screenshot never arrived")
        self.source = "gamescope PNG"
        ff = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", shot, "-vf", f"scale={AMBI_W}:{AMBI_H}:flags=area",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, cwd="/")
        try:
            out, _ = await asyncio.wait_for(ff.communicate(), 3)
        except asyncio.TimeoutError:
            ff.kill()
            raise RuntimeError("ffmpeg timed out")
        except asyncio.CancelledError:
            _stop_proc(ff)
            raise
        if len(out) < AMBI_W * AMBI_H * 3:
            raise RuntimeError("couldn't read the screenshot")
        self._pick(out)

    def _pick(self, px):
        half = AMBI_W // 2
        for i, (x0, x1) in enumerate(((0, half), (half, AMBI_W))):
            col = dominant_color(px, x0, x1)
            if col is not None:  # a black screen keeps the last color
                self.target[i] = col

    async def _shoot(self, shot, env, as_user, wait):
        """Ask gamescope for a screenshot; True once the file is fully written."""
        try:
            os.remove(shot)
        except OSError:
            pass
        proc = await asyncio.create_subprocess_exec(
            "gamescopectl", "screenshot", shot, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env=env, cwd="/", start_new_session=True, **as_user)
        try:
            rc = await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("gamescopectl timed out")
        except asyncio.CancelledError:
            _stop_proc(proc)  # Ambilight stopped mid-screenshot: don't leave it running
            raise
        if rc:
            raise RuntimeError(f"gamescopectl failed ({rc})")
        # gamescope writes the file after the command returns: wait for it to settle.
        last, deadline = -1, time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                size = os.path.getsize(shot)
            except OSError:
                size = -1
            if size > 0 and size == last:
                return True
            last = size
            await asyncio.sleep(0.05)
        return False


def react_level(pulse, flash):
    """LED React brightness for one stick, as a share of REACT_PEAK.
    - Loudness is squared first: quiet sounds barely lift the glow, loud ones push
      it up hard (calm when things are calm, alive when things get loud).
    - The result is spread on a perceived-brightness curve: equal steps in sound
      look like equal steps in light, instead of the low end looking flat."""
    drive = min(1.0, REACT_PULSE * pulse * pulse + flash)
    rest_seen = REACT_REST ** (1 / ARMADA_GAMMA)
    seen = rest_seen + (1 - rest_seen) * drive
    return seen ** ARMADA_GAMMA


class AudioMeter:
    """Listens to the game's sound - the speaker/headphone output, never the
    microphone - and keeps, per stereo side, a smoothed loudness (pulse) and a
    hit value (flash). Nothing is recorded or saved; samples are measured and
    dropped as they arrive."""

    def __init__(self):
        self.task = None
        self.proc = None
        self.source = ""
        self.error = ""
        self.pulse = [0.0, 0.0]
        self.flash = [0.0, 0.0]
        self._ref = 0.15              # shared loudness reference (both sticks on one scale)
        self._bass_avg = [0.0, 0.0]
        self._avg = [0.0, 0.0]        # short-term loudness average (~0.1s)
        self._frames = 0              # frames since listening started
        self._lp = [0.0, 0.0]
        self.updated = None           # set each time new audio has been measured

    def running(self):
        return self.task is not None and not self.task.done()

    def start(self):
        if not self.running():
            self.updated = asyncio.Event()
            self.task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        self._kill()
        self.pulse, self.flash = [0.0, 0.0], [0.0, 0.0]

    def _kill(self):
        """Stop the audio tool. Only ever signals our own process (never a process
        group by number: if the tool had already exited, that number could belong
        to some other program by now). Asks it to quit cleanly first so it leaves
        the audio system properly, and force-stops it only if it hangs."""
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()  # checks the process is still ours before signalling
        except (ProcessLookupError, OSError):
            return

        async def reap():
            try:
                await asyncio.wait_for(proc.wait(), 0.5)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except (asyncio.TimeoutError, Exception):
                    pass
            except Exception:
                pass

        try:
            asyncio.get_event_loop().create_task(reap())
        except RuntimeError:
            pass

    @staticmethod
    def _session_user():
        """The logged-in user whose audio we listen to: (uid, gid, home)."""
        import pwd
        user = os.environ.get("DECKY_USER") or "armada"
        try:
            pw = pwd.getpwnam(user)
            return pw.pw_uid, pw.pw_gid, pw.pw_dir
        except KeyError:
            return 1000, 1000, "/home/" + user

    @staticmethod
    def _commands():
        """Capture the default output's monitor, with a 20ms buffer so the lights
        keep up with the sound. Started directly as the session user (no runuser:
        that opened a new login session each time, and those piled up)."""
        rate = str(REACT_RATE)
        return [
            ("pw-record", ["pw-record", "-P", "{ stream.capture.sink = true }",
                           "--rate", rate, "--channels", "2", "--format", "s16",
                           "--latency", "20ms", "-"]),
            ("parec", ["parec", "--device=@DEFAULT_MONITOR@", f"--rate={rate}",
                       "--channels=2", "--format=s16le", "--raw", "--latency-msec=20"]),
        ]

    async def _run(self):
        uid, gid, home = self._session_user()
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        env.update({
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            "PULSE_RUNTIME_PATH": f"/run/user/{uid}/pulse",
            "HOME": home,
        })
        as_user = {}
        if os.getuid() == 0 and uid != 0:
            as_user = {"user": uid, "group": gid, "extra_groups": []}
        chunk = REACT_CHUNK * 2 * 2  # stereo, 16-bit
        while True:
            got_audio = False
            for name, cmd in self._commands():
                try:
                    self.proc = await asyncio.create_subprocess_exec(
                        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        stdin=asyncio.subprocess.DEVNULL, env=env, cwd="/",
                        start_new_session=True, **as_user)
                except (OSError, ValueError):
                    continue
                buf = bytearray()
                try:
                    while True:
                        data = await self.proc.stdout.read(65536)
                        if not data:
                            break
                        buf += data
                        got_audio = True
                        self.source, self.error = name, ""
                        pieces = len(buf) // chunk
                        if not pieces:
                            continue
                        # Never fall behind: if audio piled up (e.g. the game kept the
                        # CPU busy), skip to the newest so the lights stay in sync.
                        start = max(0, pieces - REACT_MAX_BACKLOG)
                        for i in range(start, pieces):
                            self._measure(bytes(buf[i * chunk:(i + 1) * chunk]))
                        del buf[:pieces * chunk]
                        self.updated.set()
                except OSError:
                    pass
                finally:
                    self._kill()
                if got_audio:
                    break  # the tool worked but stopped (e.g. audio restarted): retry it
            if not got_audio:
                self.error = "couldn't listen to game audio (pw-record/parec)"
                self.source = ""
            await asyncio.sleep(1.0)

    def _measure(self, data):
        samples = array.array("h")
        samples.frombytes(data)
        measured = []
        for side in (0, 1):
            ch = samples[side::2]
            n = len(ch) or 1
            lp = self._lp[side]
            sumsq = bass_sq = 0.0
            for x in ch:
                v = x / 32768.0
                sumsq += v * v
                lp += 0.118 * (v - lp)  # ~150 Hz low-pass: the "thump" of hits
                bass_sq += lp * lp
            self._lp[side] = lp
            measured.append((math.sqrt(sumsq / n), math.sqrt(bass_sq / n)))
        # One loudness scale for both sticks (so a quiet side stays dimmer than a
        # loud one). It follows the loudest recent sound, eases down over a few
        # seconds, and never drops below a floor - quiet sound stays dim instead
        # of being boosted to look loud.
        self._ref = max(max(m[0] for m in measured), self._ref * 0.995, 0.15)
        for side, (rms, bass) in enumerate(measured):
            loud = min(1.0, rms / self._ref) if rms > 0.002 else 0.0
            # Pulse: rises quickly, falls back gently.
            p = self.pulse[side]
            self.pulse[side] = p + (0.6 if loud > p else 0.12) * (loud - p)
            # Flash on hits: a jump in the bass (thumps, explosions) or a sudden jump
            # in the whole sound (gunshots). No cooldown, so every hit can flash.
            avg, bavg = self._avg[side], self._bass_avg[side]
            # Must be a real jump AND loud enough next to the game's recent level, so
            # soft sounds in quiet scenes don't count as hits.
            # Flashes are only for the loudest moments: the hit must be close to the
            # loudest sound the game played recently AND loud outright. Anything
            # less just drives the pulse.
            thump = bass > max(REACT_FLASH_MIN, REACT_FLASH_SHARE * self._ref) and bass > 2.0 * bavg + 0.008
            sharp = rms > max(REACT_FLASH_MIN, REACT_FLASH_SHARE * self._ref) and rms > 1.8 * avg + 0.01
            if self._frames > 20 and (sharp or thump):
                self.flash[side] = 1.0
            else:
                self.flash[side] *= 0.8  # fades out in about a quarter second
            self._avg[side] = avg * 0.75 + rms * 0.25
            self._bass_avg[side] = bavg * 0.9 + bass * 0.1
        self._frames += 1


ARMADA_CONTROL_SOCKET = "/run/armada/control.sock"


def armada_call(action, **payload):
    """Send one command to Armada's control service, exactly as Armada Control does."""
    request = {"action": action, **payload}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)  # keep shutdown/updates quick even if the service is slow
        sock.connect(ARMADA_CONTROL_SOCKET)
        sock.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    response = json.loads(data.decode("utf-8") or "{}")
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "Armada control call failed")
    return response.get("result", {})


def discover_armada_commands():
    """The lighting commands this Armada actually has: read from its control service
    and from the installed Armada Control plugin (both are plain files on the device)."""
    found = {}
    pat = re.compile(r"stick|led|light|rgb", re.I)
    service = _read("/usr/libexec/armada/armada-control", "")
    if service:
        found["service"] = sorted({m for m in re.findall(r'"([a-z_]+)"\s*:\s*action_', service) if pat.search(m)})
    else:
        found["service"] = ["(control service file not readable)"]
    calls = set()
    home = os.environ.get("DECKY_USER_HOME") or os.path.expanduser("~")
    plugins = os.path.join(home, "homebrew", "plugins")
    try:
        for d in os.listdir(plugins):
            if "armada" not in d.lower():
                continue
            for dirpath, _, files in os.walk(os.path.join(plugins, d)):
                if "node_modules" in dirpath:
                    continue
                for fn in files:
                    if fn.endswith(".py"):
                        src = _read(os.path.join(dirpath, fn), "")
                        calls.update(m for m in re.findall(r'call\(\s*"([a-z_]+)"', src) if pat.search(m))
    except OSError:
        pass
    found["armada_control"] = sorted(calls) or ["(Armada Control plugin not found)"]
    tool = _read(ARMADA_LED_TOOL, "")
    found["tool"] = "missing" if not tool else ("python" if tool.startswith("#!") and "python" in tool.split("\n", 1)[0] else "other")
    return found


class ArmadaControlLights:
    """Drive the sticks through Armada's control service - the same commands
    Armada Control sends, so it works however this Armada version reaches the
    lights. Armada's own lighting settings are saved first and put back after."""

    SIDES = ("l", "r")

    def __init__(self):
        self.saved = None     # Armada's lighting settings from before we took over
        self.applied = {}     # what we last sent, to skip repeats
        self.last_error = ""
        self.failed = {}      # action -> error, every command that failed
        self.unsupported = set()  # commands this Armada doesn't have ("unknown action")

    @staticmethod
    def present():
        return os.path.exists(ARMADA_CONTROL_SOCKET)

    def _send(self, key, action, **payload):
        if action in self.unsupported or self.applied.get(key) == payload:
            return
        try:
            armada_call(action, **payload)
            self.applied[key] = payload
            self.failed.pop(action, None)
        except Exception as e:
            if "unknown action" in str(e).lower():
                # Not in this Armada version: skip it from now on, send the rest.
                self.unsupported.add(action)
            else:
                self.failed[action] = str(e)[:80]
        parts = [f"{a}: {m}" for a, m in self.failed.items()]
        if self.unsupported:
            parts.append("not in this Armada: " + ", ".join(sorted(self.unsupported)))
        self.last_error = "; ".join(parts)[:300]

    def take_over(self):
        try:
            self.saved = armada_call("get_stick_led")
        except Exception as e:
            self.saved = None
            self.last_error = f"get_stick_led: {e}"[:200]
        self.applied = {}

    def state(self):
        return {"control_saved": self.saved}

    def show(self, color, effect, level):
        hexcolor = "%02X%02X%02X" % tuple(max(0, min(255, int(c))) for c in color)
        self._send("enabled", "set_stick_led_enabled", enabled=True)
        self._send("screen_link", "set_stick_led_screen_link", enabled=False)
        self._send("max", "set_stick_led_max_brightness", value=round(max(0.0, min(1.0, level)), 3))
        for side in self.SIDES:
            self._send(f"src_{side}", "set_stick_led_color_source", side=side, source="static")
            if effect == "breathe":
                # Armada's breathing: from fully dark (intensity floor 0) up to max brightness.
                self._send(f"floor_{side}", "set_stick_led_param", side=side, param="intensity", mode="breathing", value=0.0)
                self._send(f"speed_{side}", "set_stick_led_param", side=side, param="speed", mode="breathing", value=round(3.5 / PERIOD, 2))
            self._send(f"color_{side}", "set_stick_led_color", side=side, value=hexcolor)
            self._send(f"mode_{side}", "set_stick_led_mode", side=side, mode="breathing" if effect == "breathe" else "static")

    def hand_back(self, state=None):
        saved = (state or {}).get("control_saved") if state else self.saved
        if saved:
            def put(action, **payload):
                if action in self.unsupported:
                    return
                try:
                    armada_call(action, **payload)
                except Exception as e:
                    self.last_error = f"restore {action}: {e}"[:200]
            for side in self.SIDES:
                sd = (saved.get("sides") or {}).get(side) or {}
                params = sd.get("params") or {}
                for param in ("intensity", "speed"):
                    key = f"{param}_breathing"
                    if key in params:
                        put("set_stick_led_param", side=side, param=param, mode="breathing", value=float(params[key]))
                if sd.get("colorSource"):
                    put("set_stick_led_color_source", side=side, source=sd["colorSource"])
                if sd.get("color"):
                    put("set_stick_led_color", side=side, value=str(sd["color"]))
                if sd.get("mode"):
                    put("set_stick_led_mode", side=side, mode=sd["mode"])
            put("set_stick_led_max_brightness", value=float(saved.get("maxBrightness", 1.0)))
            put("set_stick_led_screen_link", enabled=bool(saved.get("screenLink", False)))
            put("set_stick_led_enabled", enabled=bool(saved.get("enabled", True)))
        self.saved = None
        self.applied = {}


def find_led_writers():
    """Running programs that look like they draw the LEDs: [(pid, unit or None, cmd)]."""
    me, parent = os.getpid(), os.getppid()
    found = []
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        if pid in (me, parent):
            continue
        cmd = _read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
        if cmd and LED_WRITER.search(cmd) and not NEVER_PAUSE.search(cmd):
            found.append((pid, _unit_of(pid), cmd[:80]))
    return found


class ArmadaSticks:
    """Armada's stick lights. While BETTR LED uses them, Armada's LED service is
    stopped (so the two don't fight); handing back fades to what Armada was
    showing and starts it again, so Armada redraws its own colors and effects."""

    NAMES = [f"{side}:{chan}{zone}" for side in "lr" for zone in (1, 2, 3, 4) for chan in "rgb"]

    def __init__(self):
        self.color = (255, 255, 255)
        self.current = {n: 0 for n in self.NAMES}  # what's on the hardware now
        self.err = {n: 0.0 for n in self.NAMES}    # dithering carry-over per channel
        self.stopped_service = False
        self.triggers = {}        # stick channels whose kernel trigger we switched off
        self.stopped_units = []   # other LED services we stopped
        self.paused_pids = []     # LED programs that aren't their own service, paused
        self.paused_desc = []     # what was paused, for the status line
        self.saved = None  # what Armada was showing when we took over
        self.tool = load_armada_tool()
        self.gamma = float(getattr(self.tool, "LED_GAMMA", ARMADA_GAMMA)) if self.tool else ARMADA_GAMMA
        if self.tool is not None:
            # We pass zones 1:1 (no stick-orientation remap), like our own snapshots.
            try:
                self.tool._ZONE_ROTATION = {"l": 0.0, "r": 0.0}
            except Exception:
                pass

    @property
    def method(self):
        return "Armada's LED tool" if self.tool is not None else "LED files directly"

    @staticmethod
    def present():
        return os.path.exists(os.path.join(LED_DIR, "l:r1"))

    def _put(self, name, value):
        if self.current.get(name) != value:
            _write(os.path.join(LED_DIR, name, "brightness"), value)
            self.current[name] = value

    def _write_zones(self, values):
        """Write whole zones whose value changed. Through Armada's tool when we have
        it: its write_zone applies its own gamma, so pass the inverse to land on
        exactly the raw value we want."""
        for side in "lr":
            for zone in (1, 2, 3, 4):
                names = [f"{side}:{c}{zone}" for c in "rgb"]
                new = [values.get(n, self.current.get(n, 0)) for n in names]
                if all(self.current.get(n) == v for n, v in zip(names, new)):
                    continue
                if self.tool is not None:
                    inv = [255 * (v / 255) ** (1 / self.gamma) if v > 0 else 0 for v in new]
                    try:
                        self.tool.write_zone(side, zone, *inv)
                    except Exception as e:
                        LAST_WRITE_ERROR[0] = f"Armada tool: {type(e).__name__}: {e}"
                        for n, v in zip(names, new):
                            _write(os.path.join(LED_DIR, n, "brightness"), v)
                else:
                    for n, v in zip(names, new):
                        _write(os.path.join(LED_DIR, n, "brightness"), v)
                for n, v in zip(names, new):
                    self.current[n] = v

    def _show(self, targets, dither=True):
        # At low brightness each channel only has a handful of whole steps (4% of
        # 255 is ~10), so a fade jumps visibly and the color shifts as green
        # rounds to 0. Temporal dithering: each frame write whichever whole value
        # keeps the running average on the exact target, so at 100 fps the eye
        # sees the in-between brightness and the true hue.
        values = {}
        for name, target in targets.items():
            if not dither:
                # Steady colors are only rewritten every so often, where dithering
                # would show as a slow flicker; just round.
                values[name] = max(0, min(255, round(target)))
                continue
            out = max(0, min(255, round(target + self.err[name])))
            self.err[name] += target - out
            values[name] = out
        self._write_zones(values)

    def _uniform(self, level):
        raw = [level * 255 * (c / 255) ** ARMADA_GAMMA for c in self.color]
        return {n: raw["rgb".index(n[2])] for n in self.NAMES}

    def take_over(self):
        r = _systemctl("is-active", ARMADA_SERVICE)
        self.stopped_service = bool(r and r.stdout.strip() == "active")
        if self.stopped_service:
            _systemctl("stop", ARMADA_SERVICE)
        # Whatever else is drawing the LEDs: stop it if it's its own service,
        # otherwise pause the process itself until we hand back.
        self.stopped_units, self.paused_pids, self.paused_desc = [], [], []
        for pid, unit, cmd in find_led_writers():
            if unit and unit != f"{ARMADA_SERVICE}.service":
                if unit not in self.stopped_units:
                    _systemctl("stop", unit)
                    self.stopped_units.append(unit)
                    self.paused_desc.append(unit)
            elif not unit:
                try:
                    os.kill(pid, signal.SIGSTOP)
                    self.paused_pids.append(pid)
                    self.paused_desc.append(cmd)
                except OSError:
                    pass
        if self.stopped_service:
            self.paused_desc.insert(0, ARMADA_SERVICE)
        # Newer Armada may drive the sticks with kernel LED triggers instead of a
        # service; while a trigger runs it owns the LED and our writes don't show.
        # Save each channel's trigger (and its settings) and switch it off.
        self.triggers = {}
        for n in self.NAMES:
            led = Led(n)
            snap = led.snapshot()
            if snap["trigger"] != "none":
                self.triggers[n] = snap
                _write(led._f("trigger"), "none")
        if self.triggers:
            kinds = sorted({t["trigger"] for t in self.triggers.values()})
            self.paused_desc.append(f"LED trigger {', '.join(kinds)} on {len(self.triggers)} channels")
        # Read after stopping so this is the last frame Armada drew.
        self.current = {n: int(_read(os.path.join(LED_DIR, n, "brightness"), "0") or 0) for n in self.NAMES}
        self.saved = dict(self.current)
        self.err = {n: 0.0 for n in self.NAMES}

    def state(self):
        return {
            "stopped_service": self.stopped_service,
            "stopped_units": self.stopped_units,
            "paused_pids": self.paused_pids,
            "triggers": self.triggers,
            "saved": self.saved,
        }

    def set_color(self, color):
        self.color = color
        self.err = {n: 0.0 for n in self.NAMES}

    def set_level(self, level, dither=True):
        self._show(self._uniform(level), dither)

    def zone_channels(self, side, zone):
        """Channel names for one zone, in red, green, blue order."""
        return [f"{side}:{c}{zone}" for c in "rgb"]

    def set_zone_levels(self, peak, zone_levels):
        """Set each zone's own brightness (0..1 of peak) in the current color.
        zone_levels: {(side, zone): level}."""
        raw = [255 * (c / 255) ** ARMADA_GAMMA for c in self.color]
        values = {}
        for (side, zone), lvl in zone_levels.items():
            for name, v in zip(self.zone_channels(side, zone), raw):
                values[name] = max(0, min(255, round(peak * lvl * v)))
        self._write_zones(values)

    def set_sides(self, peak, levels, whites, colors=None):
        """Each stick its own brightness (0..1 of peak), white mix (for flashes) and,
        optionally, its own color (Ambilight)."""
        values = {}
        for i, (side, lvl, w) in enumerate(zip("lr", levels, whites)):
            base = colors[i] if colors else self.color
            col = [c * (1 - w) + 255 * w for c in base]
            raw = [peak * lvl * 255 * (c / 255) ** ARMADA_GAMMA for c in col]
            for zone in (1, 2, 3, 4):
                for name, v in zip(self.zone_channels(side, zone), raw):
                    values[name] = max(0, min(255, round(v)))
        self._write_zones(values)

    async def dance(self, should_stop, seconds=None, level=None):
        """White dot spinning around each stick (left clockwise, right the other
        way) with a soft trail. Zones 1..4 run SW, NW, NE, SE around the stick."""
        seconds = UNPLUG_DANCE_SECONDS if seconds is None else seconds
        raw = 255 * (UNPLUG_DANCE_BRIGHTNESS if level is None else level)  # white: same on every channel
        began = time.monotonic()
        while time.monotonic() - began < seconds:
            if should_stop():
                return False
            pos = (time.monotonic() - began) * UNPLUG_DANCE_SPEED
            values = {}
            for side, direction in (("l", 1), ("r", -1)):
                head = (direction * int(pos)) % 4
                for i, zone in enumerate((1, 2, 3, 4)):
                    behind = ((head - i) * direction) % 4  # 0 = the dot, 1 = just passed
                    lvl = 1.0 if behind == 0 else 0.4 if behind == 1 else 0.0
                    for name in self.zone_channels(side, zone):
                        values[name] = round(raw * lvl)
            self._write_zones(values)
            await asyncio.sleep(1 / FADE_FPS)
        return True

    def blank(self):
        self._write_zones({n: 0 for n in self.NAMES})

    async def fade(self, targets, duration=None):
        """Cross-fade every channel from what's showing to `targets` (raw values),
        eased and interpolated in perceived brightness so it looks even."""
        duration = FADE if duration is None else duration
        start = {n: float(v) for n, v in self.current.items()}
        began = time.monotonic()
        t = 0.0
        while t < 1.0:
            # Timed by the clock, not frame count, so a slow frame can't stretch it.
            t = min(1.0, (time.monotonic() - began) / duration) if duration > 0 else 1.0
            e = t * t * (3 - 2 * t)  # ease in/out
            frame = {}
            for n in self.NAMES:
                a = (start[n] / 255) ** (1 / ARMADA_GAMMA)
                b = (max(0.0, targets[n]) / 255) ** (1 / ARMADA_GAMMA)
                frame[n] = 255 * (a + (b - a) * e) ** ARMADA_GAMMA
            self._show(frame)
            await asyncio.sleep(1 / FADE_FPS)

    async def fade_to_level(self, level, duration=None):
        await self.fade(self._uniform(level), duration)

    def hand_back(self, state=None):
        state = state or self.state()
        for pid in state.get("paused_pids") or []:
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
        for unit in state.get("stopped_units") or []:
            _systemctl("start", unit)
        if state.get("stopped_service"):
            _systemctl("start", ARMADA_SERVICE)
        elif state.get("saved") and not (state.get("stopped_units") or state.get("paused_pids")):
            for name, v in state["saved"].items():
                if v != "" and name not in (state.get("triggers") or {}):
                    _write(os.path.join(LED_DIR, name, "brightness"), v)
        # Put the kernel triggers back last, so they take over the lights again.
        for name, snap in (state.get("triggers") or {}).items():
            Led(name).apply(snap)
        self.stopped_service = False
        self.stopped_units, self.paused_pids, self.paused_desc = [], [], []
        self.triggers = {}
        self.saved = None

    async def fade_back(self):
        # Fade our color out to black, then hand back: Armada's own color just
        # comes on (no fade-in), set by Armada itself.
        if USE_FADES:
            await self.fade({n: 0.0 for n in self.NAMES})
        self.hand_back()


ARMADA_REAPPLY_RESULT = [""]  # last re-apply outcome, shown in the menu's status


def reapply_armada_rgb():
    """Make Armada apply its own stick light settings again - what turning the LEDs
    off and on in Armada Control does - so the lights come back exactly as Armada
    sets them, not just as raw values we copied back. Returns True on success."""
    if not os.path.exists(ARMADA_CONTROL_SOCKET):
        ARMADA_REAPPLY_RESULT[0] = "no Armada control service"
        return False
    try:
        cur = armada_call("get_rgb")
    except Exception as e:
        ARMADA_REAPPLY_RESULT[0] = f"get_rgb: {e}"[:150]
        return False
    if not isinstance(cur, dict) or not cur:
        ARMADA_REAPPLY_RESULT[0] = f"get_rgb returned {type(cur).__name__}"
        return False
    try:
        # Make Armada write its settings again without a visible off/on blink:
        # nudge brightness by a hair and back if there is one, else just re-send.
        nudged = None
        for key in ("brightness", "max_brightness", "maxBrightness"):
            v = cur.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                step = 0.001 if isinstance(v, float) and v <= 1 else 1
                nudged = {**cur, key: v - step if v - step >= 0 else v + step}
                break
        if nudged is not None:
            armada_call("set_rgb", **nudged)
        armada_call("set_rgb", **cur)
        ARMADA_REAPPLY_RESULT[0] = "ok"
        return True
    except Exception as e:
        ARMADA_REAPPLY_RESULT[0] = f"set_rgb: {e}"[:150]
        # Never leave the lights switched off because of us.
        try:
            armada_call("set_rgb", **cur)
        except Exception:
            pass
        return False


class RgbStickLeds(ArmadaSticks):
    """Stick lights exposed as one multicolor LED per zone (rgb:l1..rgb:l4,
    rgb:r1..rgb:r4), as on the Retroid Pocket Nova with current Armada. Same
    breath, fades and dithering as ArmadaSticks; writes each zone's color as its
    multi_intensity and puts every LED back exactly as it was afterwards."""

    PATTERN = re.compile(r"^rgb:[lr]\d+$")

    def __init__(self):
        self.leds = {}
        for n in self.names():
            led = Led(n)
            order = _read(led._f("multi_index")).split()
            self.leds[n] = (led, order)
        # One channel per LED color: "rgb:l1|red" ...
        self.NAMES = [f"{n}|{c}" for n, (_, order) in self.leds.items() for c in order]
        self.color = (255, 255, 255)
        self.current = {n: 0 for n in self.NAMES}
        self.err = {n: 0.0 for n in self.NAMES}
        self.saved = None
        self.snaps = None
        self.checks = []     # times (monotonic) to make sure the LEDs still show ours
        self.stale = set()   # LEDs something else rewrote: write ours again
        self.tool = None
        self.gamma = ARMADA_GAMMA
        self.paused_desc = []

    @classmethod
    def names(cls):
        try:
            names = sorted(os.listdir(LED_DIR))
        except OSError:
            return []
        return [n for n in names if cls.PATTERN.match(n) and os.path.exists(os.path.join(LED_DIR, n, "multi_index"))]

    @classmethod
    def present(cls):
        return bool(cls.names())

    @property
    def method(self):
        return f"stick color LEDs ({len(self.leds)} zones)"

    def zone_channels(self, side, zone):
        name = f"rgb:{side}{zone}"
        if name not in self.leds:
            return []
        order = self.leds[name][1]
        return [f"{name}|{c}" for c in ("red", "green", "blue") if c in order]

    def _show(self, targets, dither=True):
        # No dithering on these LEDs: at a few % one brightness step is a big jump,
        # so flipping between neighbouring steps shows as flicker. Plain rounding
        # gives clean, steady steps.
        super()._show(targets, dither=False)

    def _uniform(self, level):
        rgb = {"red": self.color[0], "green": self.color[1], "blue": self.color[2]}
        return {n: level * 255 * (rgb.get(n.split("|")[1], 0) / 255) ** ARMADA_GAMMA for n in self.NAMES}

    def recheck_soon(self):
        now = time.monotonic()
        self.checks = [now + t for t in RECHECK_AFTER]

    def _recheck(self):
        """Armada (or the system on wake) can rewrite the LEDs after we take over -
        with its LEDs switched off it writes them dark - and a steady look is only
        written when it changes, so it would stay lost. Put ours back if so.
        Only runs just after a wake or a switch (see RECHECK_AFTER)."""
        for name, (led, order) in self.leds.items():
            if _read(led._f("brightness")) != str(led.max):
                if led._trigger() != "none":
                    _write(led._f("trigger"), "none")
                _write(led._f("brightness"), led.max)
            ours = " ".join(str(round(self.current.get(f"{name}|{c}", 0) * led.max / 255)) for c in order)
            if _read(led._f("multi_intensity")) != ours:
                self.stale.add(name)

    def _write_zones(self, values):
        if self.checks and time.monotonic() >= self.checks[0]:
            self.checks.pop(0)
            if self.snaps is not None:
                self._recheck()
        for name, (led, order) in self.leds.items():
            new = [values.get(f"{name}|{c}", self.current.get(f"{name}|{c}", 0)) for c in order]
            if name not in self.stale and all(self.current.get(f"{name}|{c}") == v for c, v in zip(order, new)):
                continue
            self.stale.discard(name)
            scale = led.max / 255
            _write(led._f("multi_intensity"), " ".join(str(round(v * scale)) for v in new))
            for c, v in zip(order, new):
                self.current[f"{name}|{c}"] = v

    def take_over(self):
        self.snaps = {}
        for name, (led, order) in self.leds.items():
            self.snaps[name] = led.snapshot()
            # Read what's showing now (as 0..255) so fades start from it.
            mi = [int(x) for x in (self.snaps[name].get("multi_intensity") or "").split() if x.lstrip("-").isdigit()]
            br = int(self.snaps[name].get("brightness") or 0) if str(self.snaps[name].get("brightness") or "").isdigit() else 0
            for i, c in enumerate(order):
                v = mi[i] if i < len(mi) else 0
                # what the LED actually emits: intensity scaled by brightness
                self.current[f"{name}|{c}"] = round(v * br / max(1, led.max) * 255 / max(1, led.max))
            # Take the LED over: no kernel trigger, full brightness; we set the
            # color (and so the brightness) through multi_intensity.
            if led._trigger() != "none":
                _write(led._f("trigger"), "none")
            _write(led._f("brightness"), led.max)
            _write(led._f("multi_intensity"), " ".join(
                str(round(self.current[f"{name}|{c}"] * led.max / 255)) for c in order))
        self.saved = dict(self.current)
        self.err = {n: 0.0 for n in self.NAMES}

    def state(self):
        return {"rgb_snaps": self.snaps}

    def hand_back(self, state=None):
        snaps = (state or {}).get("rgb_snaps") if state else self.snaps
        for name, snap in (snaps or {}).items():
            Led(name).apply(snap)
            # apply() skips brightness when a trigger owns it; otherwise make sure
            # brightness is back too (we set it to max).
            if (snap.get("trigger") or "none") == "none" and snap.get("brightness", "") != "":
                _write(os.path.join(LED_DIR, name, "brightness"), snap["brightness"])
        self.snaps = None
        self.saved = None
        # Then let Armada re-apply its own settings (in the background, so a slow
        # service never holds anything up).
        import threading
        threading.Thread(target=reapply_armada_rgb, daemon=True).start()

    async def fade_back(self):
        """Fade to black, then have Armada put its own color on directly. The saved
        raw values are only written if Armada can't be reached: writing them first
        (sometimes too bright) and correcting a moment later is what flashed."""
        if USE_FADES:
            await self.fade({n: 0.0 for n in self.NAMES})
        snaps = self.snaps or {}
        # Kernel triggers we switched off go back first (rare on these LEDs).
        for name, snap in snaps.items():
            if (snap.get("trigger") or "none") != "none":
                Led(name).apply(snap)
        if await asyncio.to_thread(reapply_armada_rgb):
            self.snaps = None
            self.saved = None
            return
        # Armada didn't answer: fall back to the saved values.
        self.hand_back()


def find_leds():
    try:
        names = sorted(os.listdir(LED_DIR))
    except OSError:
        return []
    return [
        Led(n) for n in names
        if not SKIP.search(n)
        and not STICK_LED.match(n)
        and os.access(os.path.join(LED_DIR, n, "brightness"), os.W_OK)
    ]


class Plugin:
    enabled = False           # breathe orange while charging
    profiles_enabled = False  # per-game colors
    show_debug = False        # show the Status (debug) section in the menu
    rain = False              # LED Rain Mode on the home screen
    home_on = True            # home screen light on (Rain Mode off); off = dark
    home_hue = HOME_HUE       # home screen color
    profiles = {}             # app_id -> {name, hue, brightness, color}
    app_id = ""               # currently running game ("" = none)
    preview = None            # color being edited in the menu, shown live
    preview_level = 1.0       # its brightness, 0..1 of the LED's output
    preview_react = False     # LED React on for the color being edited
    meter = None              # AudioMeter while LED React is showing
    screen = None             # ScreenSampler while Ambilight is showing
    preview_ambi = False      # Ambilight on for the game being edited
    preview_flash = False     # no color picked yet for this game: blink white
    preview_until = 0.0
    last_seen = 0.0           # last check-in from the Steam UI (game mode)
    closed_app = ""           # game that just quit, and until when to ignore it
    closed_until = 0.0
    clock_offset = None       # wall clock minus monotonic clock, to spot a real suspend
    task = None
    changed = None
    snapshot = None           # {led name: state} while BETTR LED has the LEDs
    sticks = None             # ArmadaSticks on Armada devices
    ctl = None                # ArmadaControlLights when Armada's control service is there
    was_suspended = False
    was_plugged = False       # charger state last time round, to spot unplugging
    owning = False            # True while BETT-R LED has the lights
    showing = None            # what the loop is showing now (for the menu's status line)
    last_error = ""           # last loop failure, kept in memory only (for the status line)
    leds = []
    battery = None

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump({
                "enabled": self.enabled,
                "profiles_enabled": self.profiles_enabled,
                "profiles": self.profiles,
                "show_debug": self.show_debug,
                "rain": self.rain,
                "home_on": self.home_on,
                "home_hue": self.home_hue,
            }, f, indent=2)

    def _charging(self):
        # "Full" while still plugged in counts too (it shows green).
        return bool(self.battery) and _read(os.path.join(self.battery, "status")) in ("Charging", "Full")

    def _charge_color(self):
        """Orange, or red below CHARGE_LOW_PCT, as the color to hand the LED code.
        The LED code gamma-corrects colors, so undo that here: these are meant as
        the actual output mix on the LEDs."""
        try:
            pct = int(_read(os.path.join(self.battery, "capacity"), "50") or 50)
        except ValueError:
            pct = 50
        if _read(os.path.join(self.battery, "status")) == "Full":
            pct = 100
        # Orange while charging; red when the battery is low.
        out = CHARGE_OUTPUT_RED if pct < CHARGE_LOW_PCT else CHARGE_OUTPUT_ORANGE
        return tuple(round(255 * (c / 255) ** (1 / ARMADA_GAMMA)) for c in out)

    def _desired(self):
        """What the LEDs should show right now: (color, effect) or None to leave them alone.
        An open game with a profile shows its color (or LED React), even while
        charging; otherwise charging shows a steady red/orange/green by battery level.
        Outside game mode (no check-ins from the Steam UI: Desktop Mode, Steam closed)
        it's the same as the home screen: rain, the home color, or dark."""
        suspended = os.path.exists(ARMADA_SUSPEND_FLAG)
        # A real suspend stops the monotonic clock but not the wall clock, so the
        # gap between them jumps by the time spent asleep.
        offset = time.time() - time.monotonic()
        woke = self.clock_offset is not None and offset - self.clock_offset > 2.0
        self.clock_offset = offset
        if woke or (self.was_suspended and not suspended):
            # Steam is frozen during sleep; give it time to check in again. A "game
            # quit" Steam sent while going to sleep doesn't count either.
            self.last_seen = time.monotonic()
            self.closed_until = 0.0
            if hasattr(self.sticks, "recheck_soon"):
                self.sticks.recheck_soon()
        self.was_suspended = suspended
        charging = self.enabled and self._charging()
        if suspended:
            # Going to sleep. Charging: a steady battery color as a charging light
            # (it holds until wake). Otherwise, if we have the lights, just go dark
            # and keep them - no hand-back and no calls to Armada while the device
            # is shutting down; everything is sorted out properly on wake.
            if charging:
                return (self._charge_color(), "solid", CHARGE_BRIGHTNESS, "sleep")
            if self.owning:
                return ((0, 0, 0), "solid", 0.0, "sleep")
            return None
        in_ui = time.monotonic() - self.last_seen <= GAME_MODE_TIMEOUT
        app_id = self.app_id if in_ui else ""  # a game left over from Steam closing doesn't count
        if app_id and app_id == self.closed_app and time.monotonic() < self.closed_until:
            app_id = ""  # just quit: no screenshots or audio while it shuts down
        if in_ui and self.preview is not None and time.monotonic() < self.preview_until:
            if self.preview_flash:
                return ((255, 255, 255), "flash", self.preview_level, "preview")
            effect = "react" if self.preview_react else ("ambi" if self.preview_ambi else "solid")
            return (SCREEN if self.preview_ambi else self.preview, effect, self.preview_level, "preview")
        # A game's own color (or LED React) wins over the charging colors.
        if self.profiles_enabled and app_id in self.profiles:
            p = self.profiles[app_id]
            color, level = self._profile_look(p)
            if p.get("ambi"):
                return (SCREEN, "react" if p.get("react") else "ambi", level, "game")
            return (color, "react" if p.get("react") else "solid", level, "game")
        if charging:
            return (self._charge_color(), "breathe", CHARGE_BRIGHTNESS, "charge")
        # LED Rain Mode: on the home screen only (no game running), instead of
        # Armada's own color.
        # The LEDs switch off keeps the home screen dark, rain or not.
        if self.rain and self.home_on and not app_id and self.sticks:
            return ((255, 255, 255), "rain", RAIN_PEAK, "rain")
        # Rain Mode off, or a game with no color of its own: the home color (or dark
        # when switched off), not Armada's. Games never hand back to Armada, so Steam
        # briefly losing track of the game (e.g. around sleep) changes nothing.
        if self.sticks:
            r, g, b = colorsys.hsv_to_rgb((self.home_hue % 360) / 360, 1.0, 1.0)
            color = (round(r * 255), round(g * 255), round(b * 255))
            return (color, "solid", HOME_BRIGHTNESS if self.home_on else 0.0, "home")
        return None

    @staticmethod
    def _profile_look(p):
        """A saved game color as (full-strength color, brightness 0..1).
        Brightness is a straight share of the LED's output, like the charging
        breath; folding it into the color instead got gamma-dimmed a second
        time, so a low brightness like 5% came out as LEDs that looked off."""
        if "hue" in p and "brightness" in p:
            r, g, b = colorsys.hsv_to_rgb((p["hue"] % 360) / 360, 1.0, 1.0)
            return (round(r * 255), round(g * 255), round(b * 255)), max(0.0, min(1.0, p["brightness"] / 100))
        return tuple(p["color"]), 1.0

    def _take_over(self):
        self.snapshot = {led.name: led.snapshot() for led in self.leds}
        if self.ctl:
            self.ctl.take_over()
        elif self.sticks:
            self.sticks.take_over()
        try:
            os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
            with open(SNAPSHOT_FILE, "w") as f:
                json.dump({
                    "leds": self.snapshot,
                    "armada": self.sticks.state() if (self.sticks and not self.ctl) else None,
                    "control": self.ctl.state() if self.ctl else None,
                }, f)
        except OSError:
            pass

    def _hand_back(self):
        self._apply_snapshot(self.snapshot or {})
        if self.ctl:
            self.ctl.hand_back()
        elif self.sticks:
            self.sticks.hand_back()

    async def _fade_back(self):
        self._apply_snapshot(self.snapshot or {})
        if self.ctl:
            await asyncio.to_thread(self.ctl.hand_back)
        elif self.sticks:
            await self.sticks.fade_back()

    async def _transition(self, want, prev):
        """Switch to a new look. Fades on the sticks, except slider moves in the
        menu (preview -> preview, or a new home color), which stay instant so they
        track the slider."""
        color, effect = want[0], want[1]
        if hasattr(self.sticks, "recheck_soon"):
            self.sticks.recheck_soon()
        if color == SCREEN:
            # Fade toward the screen's color (or white until the first screenshot).
            if self.screen is None:
                self.screen = ScreenSampler()
            self.screen.start()
            c = self.screen.colors()[0]
            color = tuple(round(x) for x in c)
        if self.ctl:
            # Armada draws it (and animates the breath) itself.
            await asyncio.to_thread(self.ctl.show, color, effect, want[2])
            return
        self._set_color(color)
        for led in self.leds:  # non-stick LEDs switch straight to the new look
            led.set_level(0.0 if effect == "breathe" else want[2])
        if self.sticks and not USE_FADES:
            self.sticks.set_level(0.0 if effect == "breathe" else want[2], dither=False)
        elif self.sticks and want[3] == "sleep":
            # About to sleep: set it right away, there's no time for a fade.
            self.sticks.set_level(want[2], dither=False)
        elif self.sticks and not (
            prev and prev[3] == want[3]
            and (want[3] == "preview" or (want[3] == "home" and prev[2] == want[2]))
        ):
            # A breath starts dark, so drop to off quickly; otherwise fade to the color.
            if effect == "breathe":
                await self.sticks.fade_to_level(0.0, CHARGE_FADE)
            elif effect == "rain":
                await self.sticks.fade_to_level(0.0)  # rain falls on a dark stick
            else:
                await self.sticks.fade_to_level(REACT_PEAK * REACT_REST if effect == "react" else want[2])

    async def _rain(self, want):
        """White raindrops: each frame a zone may get a new drop (random strength),
        and every drop fades out. Runs until the look changes or the charger is
        plugged/unplugged (so the unplug dance can play)."""
        zones = [(side, z) for side in "lr" for z in (1, 2, 3, 4)]
        drops = {k: 0.0 for k in zones}
        chance = RAIN_DROPS_PER_SEC / 4 / RAIN_FPS   # per zone per frame
        decay = 0.02 ** (1 / (RAIN_FADE * RAIN_FPS))  # ~fully faded after RAIN_FADE
        plugged_at_start = self._charging()
        self.sticks.set_color((255, 255, 255))
        while self._desired() == want and self._charging() == plugged_at_start:
            for k in zones:
                if drops[k] < 0.15 and random.random() < chance:
                    drops[k] = random.uniform(0.55, 1.0)
                else:
                    drops[k] = drops[k] * decay if drops[k] > 0.01 else 0.0
            # Drops look natural with a quick perceived fade: shape them on the eye's curve.
            self.sticks.set_zone_levels(want[2], {k: v ** ARMADA_GAMMA for k, v in drops.items()})
            await asyncio.sleep(1 / RAIN_FPS)

    def _set_color(self, color):
        for led in self.leds:
            led.set_color(color)
        if self.sticks:
            self.sticks.set_color(color)

    def _set_level(self, level, dither=True):
        for led in self.leds:
            led.set_level(level)
        if self.sticks:
            self.sticks.set_level(level, dither)

    def _apply_snapshot(self, snapshot):
        for led in self.leds:
            led.apply(snapshot.get(led.name))

    def _drop_snapshot(self):
        self.snapshot = None
        try:
            os.remove(SNAPSHOT_FILE)
        except OSError:
            pass

    def _poke(self):
        if self.changed is not None:
            self.changed.set()

    async def _wait(self, timeout):
        # The event is cleared at the top of each loop pass (not here), so a change
        # that arrives mid-fade still wakes this wait immediately.
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def _supervise(self):
        """Keep the LED loop alive: if it ever fails, start it again."""
        while True:
            try:
                await self._run()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.showing = None
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                await asyncio.sleep(1.0)

    async def _run(self):
        active = None
        handed_back = None  # (snapshot, checks left) after giving the LEDs back
        try:
            while True:
                self.changed.clear()
                self.owning = active is not None
                want = self._desired()
                # Just unplugged: do the white dance first, whatever was showing
                # (battery color, a game color or LED React). Stops early if it's
                # plugged back in.
                plugged = self._charging()
                unplugged = self.was_plugged and not plugged
                self.was_plugged = plugged
                if (
                    unplugged and self.enabled and self.sticks
                    and not os.path.exists(ARMADA_SUSPEND_FLAG)
                ):
                    if self.meter and self.meter.running():
                        await self.meter.stop()
                    if active is None:
                        if handed_back:
                            self._apply_snapshot(handed_back[0])
                            handed_back = None
                        self._take_over()
                    await self.sticks.dance(lambda: self._charging())
                    self.sticks.set_color((255, 255, 255))
                    active = (None, "dance", 0.0, "dance")
                    want = self._desired()
                if want is None and self.meter and self.meter.running():
                    await self.meter.stop()
                if want is None and self.screen and self.screen.running():
                    await self.screen.stop()
                if want is None:
                    if active is not None:
                        handed_back = (self.snapshot, RESTORE_CHECKS)
                        if os.path.exists(ARMADA_SUSPEND_FLAG) and not self.ctl:
                            # Going to sleep: go dark now (a fade wouldn't finish before
                            # the processor stops) and let Armada keep them off.
                            if self.sticks:
                                self.sticks.blank()
                            self._hand_back()
                        else:
                            await self._fade_back()
                        self._drop_snapshot()
                        active = None
                        self.showing = None
                    if handed_back:
                        # The system may touch the LEDs right as we hand back (e.g. on
                        # unplug); keep re-applying the original state briefly so every
                        # LED ends up exactly as it was.
                        await self._wait(1.0)
                        snap, left = handed_back
                        if self._desired() is None:
                            self._apply_snapshot(snap)
                        handed_back = (snap, left - 1) if left > 1 and self._desired() is None else None
                    else:
                        await self._wait(POLL)
                    continue

                if active is None:
                    # Taking over again before the checks finished: the LEDs already
                    # hold the original state, so snapshot that rather than mid-restore.
                    if handed_back:
                        self._apply_snapshot(handed_back[0])
                        handed_back = None
                    if self.ctl:
                        await asyncio.to_thread(self._take_over)
                    else:
                        self._take_over()
                if want != active:
                    await self._transition(want, active)
                    active = want
                    self.showing = want[3]

                if want[1] != "react" and self.meter and self.meter.running():
                    await self.meter.stop()
                if want[0] != SCREEN and self.screen and self.screen.running():
                    await self.screen.stop()
                if self.ctl:
                    await self._wait(0.25)
                elif want[1] == "react" and self.sticks:
                    if self.meter is None:
                        self.meter = AudioMeter()
                    self.meter.start()
                    plugged_at_start = self._charging()
                    # Leave the loop if the look changes or the charger is pulled/plugged
                    # (so the unplug dance can play).
                    while self._desired() == want and self._charging() == plugged_at_start:
                        m = self.meter
                        levels = [react_level(m.pulse[i], m.flash[i]) for i in (0, 1)]
                        whites = [REACT_FLASH_WHITE * m.flash[i] for i in (0, 1)]
                        colors = self.screen.colors() if (want[0] == SCREEN and self.screen) else None
                        self.sticks.set_sides(REACT_PEAK, levels, whites, colors)
                        wrote_at = time.monotonic()
                        # Update the moment new audio is measured (not on a separate beat)...
                        try:
                            await asyncio.wait_for(m.updated.wait(), 0.05)
                        except asyncio.TimeoutError:
                            pass
                        m.updated.clear()
                        # ...but no more than REACT_MAX_UPDATES times a second.
                        gap = 1 / REACT_MAX_UPDATES - (time.monotonic() - wrote_at)
                        if gap > 0:
                            await asyncio.sleep(gap)
                elif want[1] == "ambi" and self.sticks:
                    # Steady screen color at the game's brightness.
                    if self.screen is None:
                        self.screen = ScreenSampler()
                    self.screen.start()
                    plugged_at_start = self._charging()
                    while self._desired() == want and self._charging() == plugged_at_start:
                        self.sticks.set_sides(want[2], [1.0, 1.0], [0.0, 0.0], self.screen.colors())
                        await asyncio.sleep(1 / RAIN_FPS)
                elif want[1] == "rain" and self.sticks:
                    await self._rain(want)
                elif want[1] == "flash":
                    # No color picked yet for this game: blink white until one is.
                    began = time.monotonic()
                    while self._desired() == want:
                        on = ((time.monotonic() - began) / FLASH_PERIOD) % 1.0 < 0.5
                        self._set_level(want[2] if on else 0.0, dither=False)
                        await asyncio.sleep(0.05)
                elif want[1] == "breathe":
                    began = time.monotonic()
                    while True:
                        phase = (time.monotonic() - began) / PERIOD
                        if phase >= 1.0:
                            break
                        self._set_level(breath(phase) * want[2])
                        await asyncio.sleep(1 / FPS)
                        if self._desired() != want:
                            break
                else:
                    self._set_level(want[2], dither=False)
                    # Short wait: Armada only gives ~0.6s between announcing sleep and
                    # the processor stopping, so notice it quickly.
                    await self._wait(0.25)
        finally:
            if active is not None:
                if self.ctl:
                    # Restoring Armada's settings goes through its service; never let
                    # that hold up Decky. If it doesn't finish in 4s, the saved state
                    # stays on disk and is restored the next time BETTR LED starts.
                    import threading
                    t = threading.Thread(target=self._hand_back, daemon=True)
                    t.start()
                    t.join(4)
                    if not t.is_alive():
                        self._drop_snapshot()
                else:
                    self._hand_back()
                    self._drop_snapshot()

    # ---------- frontend API ----------

    def _write_check(self):
        """Do the stick LED files keep what BETTR LED last wrote? If something else
        is writing them, they won't; if they do but the lights don't change, the
        files no longer drive the lights."""
        if not (self.sticks and self.showing):
            return "idle"
        if isinstance(self.sticks, RgbStickLeds):
            diffs = []
            for name, (led, order) in list(self.sticks.leds.items())[:2]:
                ours = " ".join(str(round(self.sticks.current.get(f"{name}|{c}", 0) * led.max / 255)) for c in order)
                now = _read(led._f("multi_intensity"), "?")
                if ours != now:
                    diffs.append(f"{name} file={now} ours={ours}")
            return "files keep our values" if not diffs else "OVERWRITTEN: " + "; ".join(diffs)
        diffs = []
        for n in ("l:r1", "l:g1", "r:r1"):
            ours = self.sticks.current.get(n)
            now = _read(os.path.join(LED_DIR, n, "brightness"), "?")
            if str(ours) != now:
                diffs.append(f"{n} file={now} ours={ours}")
        return "files keep our values" if not diffs else "OVERWRITTEN: " + "; ".join(diffs)

    async def get_status(self):
        """What the backend sees right now, for the status line in the menu."""
        svc = _systemctl("is-active", ARMADA_SERVICE) if self.sticks else None
        try:
            all_leds = sorted(os.listdir(LED_DIR))
        except OSError:
            all_leds = []
        return {
            "sticks": bool(self.sticks),
            "leds": [l.name for l in self.leds][:6],
            "service": svc.stdout.strip() if svc and svc.stdout else ("n/a" if not self.sticks else "unknown"),
            "battery": (os.path.basename(self.battery) + " " + _read(os.path.join(self.battery, "status"), "?")) if self.battery else "none",
            "checkin": round(time.monotonic() - self.last_seen, 1) if self.last_seen else -1,
            "asleep": os.path.exists(ARMADA_SUSPEND_FLAG),
            "loop": "running" if self.task and not self.task.done() else "stopped",
            "showing": self.showing or "Armada",
            "error": self.last_error,
            "write_check": self._write_check(),
            "ambi": (f"watching via {self.screen.source}" if self.screen and self.screen.source and self.screen.running() and not self.screen.error
                     else (self.screen.error if self.screen and self.screen.error else "off")),
            "react": (f"listening via {self.meter.source}" if self.meter and self.meter.source and self.meter.running()
                      else (self.meter.error if self.meter and self.meter.error else "off")),
            "method": "Armada Control's service" if self.ctl else (self.sticks.method if self.sticks else "n/a"),
            "commands": discover_armada_commands(),
            "write_error": (self.ctl.last_error if self.ctl else "") or LAST_WRITE_ERROR[0]
            or (f"Armada re-apply: {ARMADA_REAPPLY_RESULT[0]}" if ARMADA_REAPPLY_RESULT[0] not in ("", "ok") else ""),
            "others": [c for _, _, c in find_led_writers()][:6],
            "paused": (self.sticks.paused_desc if self.sticks else []),
            "all_leds": [n for n in all_leds if not STICK_LED.match(n)][:12],
            "stick_count": sum(1 for n in all_leds if STICK_LED.match(n)),
        }

    async def get_settings(self):
        return {
            "enabled": self.enabled,
            "profiles_enabled": self.profiles_enabled,
            "profiles": self.profiles,
            "app_id": self.app_id,
            "show_debug": self.show_debug,
            "rain": self.rain,
            "home_on": self.home_on,
            "home_hue": self.home_hue,
        }

    async def set_home(self, on: bool, hue: int):
        """Home screen light (Rain Mode off): on/off and its color."""
        self.home_on = bool(on)
        self.home_hue = int(hue) % 360
        self._save_settings()
        self._poke()
        return {"home_on": self.home_on, "home_hue": self.home_hue}

    async def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        self._save_settings()
        self._poke()
        return self.enabled

    async def set_rain(self, enabled: bool):
        self.rain = bool(enabled)
        self._save_settings()
        self._poke()
        return self.rain

    async def set_show_debug(self, show: bool):
        self.show_debug = bool(show)
        self._save_settings()
        return self.show_debug

    async def set_profiles_enabled(self, enabled: bool):
        self.profiles_enabled = bool(enabled)
        self._save_settings()
        self._poke()
        return self.profiles_enabled

    async def set_running_game(self, app_id: str):
        """Called by the Steam UI on game changes and every few seconds as a check-in."""
        app_id = str(app_id or "")
        changed = app_id != self.app_id or time.monotonic() - self.last_seen > GAME_MODE_TIMEOUT
        self.app_id = app_id
        self.last_seen = time.monotonic()
        if changed:
            self._poke()

    async def game_closing(self, app_id: str):
        """Called the moment Steam says a game quit (before it's gone from the
        running list). Stop Ambilight screenshots and LED React audio right away:
        taking screenshots of a game while it shuts down can hang the display."""
        self.closed_app = str(app_id or "")
        self.closed_until = time.monotonic() + CLOSING_HOLD
        if self.screen is not None and self.screen.running():
            await self.screen.stop()
        if self.meter is not None and self.meter.running():
            await self.meter.stop()
        self._poke()

    async def save_profile(self, app_id: str, name: str, hue: int, brightness: int, color: list,
                           react: bool = False, ambi: bool = False):
        self.profiles[str(app_id)] = {
            "name": name,
            "hue": int(hue),
            "brightness": int(brightness),
            "color": [max(0, min(255, int(c))) for c in color[:3]],
            "react": bool(react),
            "ambi": bool(ambi),
        }
        self._save_settings()
        self._poke()
        return self.profiles

    async def set_preview(self, color: list, level: float = 1.0, react: bool = False, ambi: bool = False,
                          flash: bool = False):
        """Show a color live while it's being edited; call again to keep it alive."""
        self.preview = tuple(max(0, min(255, int(c))) for c in color[:3])
        self.preview_level = max(0.0, min(1.0, float(level)))
        self.preview_react = bool(react)
        self.preview_ambi = bool(ambi)
        self.preview_flash = bool(flash)
        self.preview_until = time.monotonic() + PREVIEW_TIMEOUT
        self._poke()

    async def end_preview(self):
        self.preview = None
        self._poke()

    async def delete_all_profiles(self):
        """Start fresh: forget every saved game color."""
        self.profiles = {}
        self._save_settings()
        self._poke()
        return self.profiles

    async def delete_profile(self, app_id: str):
        self.profiles.pop(str(app_id), None)
        self._save_settings()
        self._poke()
        return self.profiles

    # ---------- lifecycle ----------

    async def _main(self):
        s = self._load_settings()
        self.enabled = bool(s.get("enabled", False))
        self.profiles_enabled = bool(s.get("profiles_enabled", False))
        self.profiles = s.get("profiles", {}) or {}
        self.show_debug = bool(s.get("show_debug", False))
        self.rain = bool(s.get("rain", False))
        self.home_on = bool(s.get("home_on", True))
        try:
            self.home_hue = int(s.get("home_hue", HOME_HUE)) % 360
        except (TypeError, ValueError):
            self.home_hue = HOME_HUE
        self.battery = find_battery()
        if RgbStickLeds.present():
            # Sticks as one color LED per zone (Pocket Nova on current Armada).
            self.sticks = RgbStickLeds()
            self.ctl = None
        else:
            self.sticks = ArmadaSticks() if ArmadaSticks.present() else None
            self.ctl = None
        # On Armada stick devices only the sticks are ours; leave every other LED
        # (status/charge/flash lights) alone.
        self.leds = [] if self.sticks else find_leds()  # only the sticks when we have them
        # Left over from a crash/restart while we had the LEDs: put them back first.
        try:
            with open(SNAPSHOT_FILE) as f:
                left = json.load(f)
            self._apply_snapshot(left.get("leds") or {})
            if self.sticks and left.get("armada"):
                self.sticks.hand_back(left["armada"])
            if left.get("control") and left["control"].get("control_saved"):
                ArmadaControlLights().hand_back(left["control"])
        except (OSError, ValueError, AttributeError):
            pass
        self._drop_snapshot()
        self.changed = asyncio.Event()
        self.task = asyncio.get_event_loop().create_task(self._supervise())

    async def _unload(self):
        if self.screen is not None:
            try:
                await asyncio.wait_for(self.screen.stop(), 2)
            except Exception:
                pass
        if self.meter is not None:
            try:
                await asyncio.wait_for(self.meter.stop(), 2)
            except Exception:
                pass
        # Never hold up Decky removing/updating the plugin: give the hand-back a few
        # seconds at most (the saved state stays on disk and is restored next start).
        if self.task is not None:
            self.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            self.task = None
