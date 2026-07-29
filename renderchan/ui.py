"""
renderchan/ui.py — console output in the style of the codegraph CLI
(https://github.com/colbymchenry/codegraph).

Two modes:
  * default (quiet): clack-style frame (intro/outro, log lines with a rail),
    animated "shimmer" progress (spinner, bar, percent);
  * verbose (ui.set_verbose(True)): the historical plain-text RenderChan output.

Fallbacks: non-TTY -> plain lines without ANSI; NO_COLOR/FORCE_COLOR/--no-color;
Unicode/ASCII glyphs (RENDERCHAN_ASCII=1 / RENDERCHAN_UNICODE=1, TERM=linux,
Windows terminals).

The animation runs in a background thread, so output does not freeze while the
main thread is busy rendering.
"""

import colorsys
import math
import os
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------- glyphs ----

UNICODE_GLYPHS = {
    "ok": "✓", "err": "✗", "info": "ℹ", "warn": "⚠",
    "spinner": ["·", "✢", "✳", "✶", "✻", "✽"],
    "bar_filled": "█", "bar_empty": "░",
    "rail": "│", "phase_done": "◆", "dash": "—",
    "info_dot": "●", "corner_tl": "┌", "corner_bl": "└",
}

ASCII_GLYPHS = {
    "ok": "[OK]", "err": "[ERR]", "info": "[i]", "warn": "[!]",
    "spinner": [".", "*", "+", "x", "o", "O"],
    "bar_filled": "#", "bar_empty": "-",
    "rail": "|", "phase_done": "*", "dash": "-",
    "info_dot": "*", "corner_tl": "+", "corner_bl": "+",
}


def supports_unicode() -> bool:
    env = os.environ
    if env.get("RENDERCHAN_ASCII") == "1":
        return False
    if env.get("RENDERCHAN_UNICODE") == "1":
        return True
    if sys.platform == "win32":
        return bool(
            env.get("CI")
            or env.get("WT_SESSION")                    # Windows Terminal
            or env.get("TERMINUS_SUBLIME")
            or env.get("ConEmuTask") == "{cmd::Cmder}"  # ConEmu / cmder
            or env.get("TERM_PROGRAM") in ("Terminus-Sublime", "vscode")
            or env.get("TERM") in ("xterm-256color", "alacritty")
            or env.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm"
        )
    return env.get("TERM") != "linux"


G = UNICODE_GLYPHS if supports_unicode() else ASCII_GLYPHS


# ---------------------------------------------------------------- colors ----

def ansi_colors_enabled() -> bool:
    """--no-color > --color > NO_COLOR > FORCE_COLOR > TTY > CI > off."""
    argv = sys.argv
    if "--no-color" in argv:
        return False
    if "--color" in argv:
        return True
    if os.environ.get("NO_COLOR"):
        return False
    force = os.environ.get("FORCE_COLOR")
    if force:
        return force != "0" and force.lower() != "false"
    if sys.stdout.isatty() and os.environ.get("TERM") != "dumb":
        return True
    if os.environ.get("CI"):
        return True
    return False


COLORS = ansi_colors_enabled()

AMBER_DARK = (160, 100, 9)
AMBER_BRIGHT = (251, 191, 36)


def _derive_bright(dark) -> tuple:
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in dark))
    r, g, b = colorsys.hsv_to_rgb(h, s * 0.9, 0.98)
    return round(r * 255), round(g * 255), round(b * 255)


RST = "\x1b[0m" if COLORS else ""
DM = "\x1b[2m" if COLORS else ""
BOLD = "\x1b[1m" if COLORS else ""
GRN = "\x1b[32m" if COLORS else ""
BLU = "\x1b[34m" if COLORS else ""
YLW = "\x1b[33m" if COLORS else ""
RED = "\x1b[31m" if COLORS else ""


def _amber(t: float, dark=AMBER_DARK, bright=AMBER_BRIGHT) -> str:
    if not COLORS:
        return ""
    r = round(dark[0] + (bright[0] - dark[0]) * t)
    g = round(dark[1] + (bright[1] - dark[1]) * t)
    b = round(dark[2] + (bright[2] - dark[2]) * t)
    return f"\x1b[38;2;{r};{g};{b}m{BOLD}"


# ------------------------------------------------------------- verbosity ----

_VERBOSE = False
_MUTED = False


def set_verbose(flag: bool) -> None:
    global _VERBOSE
    _VERBOSE = bool(flag)


def is_verbose() -> bool:
    return _VERBOSE


def set_muted(flag: bool) -> None:
    """Mute warnings (e.g. during a read-only analysis pre-pass)."""
    global _MUTED
    _MUTED = bool(flag)


def format_duration(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    t = int(seconds)
    return "%02d:%02d:%02d" % (t // 3600, (t % 3600) // 60, t % 60)


_indent = 0  # block nesting depth (quiet mode); each level adds a rail prefix


def _prefix() -> str:
    return "".join(f"{DM}{G['rail']}{RST}  " for _ in range(_indent))


def _rail() -> str:
    return f"{DM}{G['rail']}{RST}  " if _indent == 0 else ""


def _safe_write(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # consumer closed the pipe (e.g. `renderchan | head`) - go quietly
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)


def _write(message: str) -> None:
    if _progress is not None:
        _progress.interrupt()
    _safe_write(_prefix() + message + "\n")


# --------------------------------------------------- clack-like scaffold ----

def intro(title: str) -> None:
    global _indent
    if _VERBOSE:
        return
    if _progress is not None:
        _progress.close_phase()
    _write(f"{DM}{G['corner_tl']}{RST}  {title}")
    _indent += 1


def outro(message: str = "") -> None:
    global _indent
    if _VERBOSE:
        return
    if _progress is not None:
        _progress.close_phase()
    if _indent > 0:
        _indent -= 1
    _write(f"{DM}{G['corner_bl']}{RST}  {message}")


def log_success(message: str) -> None:
    if _VERBOSE:
        return
    _write(f"{_rail()}{GRN}{G['phase_done']}{RST} {message}")


def log_info(message: str) -> None:
    if _VERBOSE:
        return
    _write(f"{_rail()}{BLU}{G['info_dot']}{RST} {message}")


def log_warn(message: str) -> None:
    if _VERBOSE:
        return
    _write(f"{_rail()}{YLW}{G['warn']}{RST} {message}")


def log_line(message: str) -> None:
    """Plain line at the current indent (list items inside blocks)."""
    if _VERBOSE:
        return
    _write("  " + str(message))


def rail_blank() -> None:
    if _VERBOSE:
        return
    _write(f"{_rail()}")


def quiet_subprocess() -> dict:
    """Kwargs for subprocess.* calls: suppress child output in quiet mode."""
    if _VERBOSE:
        return {}
    return {"stdout": subprocess.DEVNULL, "stderr": subprocess.STDOUT}


# ------------------------------------------------------ leveled logging ----

def info(message="") -> None:
    """Informational chatter — verbose only, plain historical format."""
    if _VERBOSE:
        _write(str(message))


def debug(message="") -> None:
    """Debug output — verbose only."""
    if _VERBOSE:
        _write(str(message))


def blank() -> None:
    """Empty line — verbose only (used to space out the historical output)."""
    if _VERBOSE:
        _write("")


def notice(message="") -> None:
    """User-facing info in both modes: plain in verbose, clack line in quiet."""
    if _VERBOSE:
        _write(str(message))
    else:
        log_info(str(message))


def warn(message) -> None:
    if _MUTED:
        return
    if _VERBOSE:
        _write("Warning: %s" % message)
    else:
        _write(f"{_rail()}{YLW}{G['warn']}{RST} {message}")


def error(message, stderr=False) -> None:
    if _VERBOSE:
        stream = sys.stderr if stderr else sys.stdout
        print("ERROR: %s" % message, file=stream)
    else:
        _write(f"{_rail()}{RED}{G['err']}{RST} {message}")


# ------------------------------------------------------ shimmer progress ----

_ANIM_INTERVAL = 0.15      # 150 ms per frame
_FRAMES_PER_GLYPH = 3
_BAR_WIDTH = 25
_CYCLE_FRAMES = 24
_SHIMMER_WIDTH = 3


class ShimmerProgress:
    """Animated per-phase progress.

    Usage:
        progress = ShimmerProgress()
        progress.on_progress("Rendering", current=i, total=n)
        ...
        progress.stop()   # prints the final "◆ Phase — done" line

    Custom gradient:
        ShimmerProgress(gradient=(0, 100, 200))                     # bright derived
        ShimmerProgress(gradient=((0, 100, 200), (200, 200, 255)))  # explicit pair
    """

    def __init__(self, gradient=None) -> None:
        if gradient is None:
            self._dark, self._bright = AMBER_DARK, AMBER_BRIGHT
        elif isinstance(gradient[0], int):      # single color: derive bright
            self._dark, self._bright = gradient, _derive_bright(gradient)
        else:                                   # explicit (dark, bright) pair
            self._dark, self._bright = gradient
        self._tty = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

        self._last_phase = ""
        self._phase_name = ""
        self._percent = -1
        self._count = 0
        self._context = ""

        # Current state read by the animation thread
        self._msg = ""
        self._anim_percent = -1
        self._anim_count = 0
        self._start = time.monotonic()

        if self._tty:
            self._thread = threading.Thread(target=self._render_loop, daemon=True)
            self._thread.start()

    # ---------------------------------------------------------- API ----

    def set_context(self, text: str) -> None:
        self._context = text

    def on_progress(self, phase: str, current: int = 0, total: int = 0) -> None:
        phase_name = phase
        with self._lock:
            if self._last_phase and phase != self._last_phase:
                self._print_phase_done_locked()
            if phase != self._last_phase:
                self._print_phase_start_locked(phase_name)

            self._last_phase = phase
            self._phase_name = phase_name

            percent = round(current / total * 100) if total > 0 else -1
            count = current if total <= 0 and current > 0 else 0
            self._percent, self._count = percent, count
            self._msg, self._anim_percent, self._anim_count = phase_name, percent, count

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            self._print_phase_done_locked()

    def close_phase(self) -> None:
        """Print the done-line of the currently open phase, if any."""
        with self._lock:
            self._print_phase_done_locked()

    def interrupt(self) -> None:
        """Erase the animation line so a permanent line can be printed cleanly."""
        with self._lock:
            if self._tty and self._msg:
                _safe_write("\r\x1b[K")

    # ------------------------------------------------------ internals ----

    def _print_phase_start_locked(self, phase_name: str) -> None:
        prefix = "\r\x1b[K" if self._tty else ""
        title = f"{phase_name} {self._context}" if self._context and _indent == 0 else phase_name
        _safe_write(f"{prefix}{_prefix()}{DM}{G['corner_tl']}{RST}  {title}\n")

    def _print_phase_done_locked(self) -> None:
        if not self._phase_name:
            return
        prefix = "\r\x1b[K" if self._tty else ""
        if self._count > 0:
            detail = "  %s found" % f"{self._count:,}".replace(",", " ")
        else:
            detail = "  done"
        _safe_write(f"{prefix}{_prefix()}{DM}{G['corner_bl']}{RST}{detail}\n")
        self._phase_name, self._percent, self._count = "", -1, 0

    def _render_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                self._render_frame()
            time.sleep(0.05)  # 50 ms render tick

    def _render_frame(self) -> None:
        if not self._msg:
            return
        frame = int((time.monotonic() - self._start) / _ANIM_INTERVAL)
        spinner = G["spinner"]
        glyph = spinner[(frame // _FRAMES_PER_GLYPH) % len(spinner)]
        t = (math.sin(frame * 2 * math.pi / 13) + 1) / 2
        color = _amber(t, self._dark, self._bright)

        if self._anim_percent >= 0:
            filled = round(_BAR_WIDTH * self._anim_percent / 100)
            empty = _BAR_WIDTH - filled
            bar = self._render_bar(frame, filled, empty)
            line = (f"{_prefix()}{DM}{G['rail']}{RST}  {color}{glyph}{RST} "
                    f"{self._msg}  {bar}  {self._anim_percent}%")
        elif self._anim_count > 0:
            count = f"{self._anim_count:,} found".replace(",", " ")
            line = (f"{_prefix()}{DM}{G['rail']}{RST}  {color}{glyph}{RST} "
                    f"{self._msg}... {count}")
        else:
            line = f"{_prefix()}{DM}{G['rail']}{RST}  {color}{glyph}{RST} {self._msg}..."

        _safe_write(f"\r\x1b[K{line}")

    def _render_bar(self, frame: int, filled: int, empty: int) -> str:
        if filled == 0:
            return f"{DM}{G['bar_empty'] * empty}{RST}"
        shimmer_pos = ((frame % _CYCLE_FRAMES) / _CYCLE_FRAMES) * (filled + 6) - 3
        out = []
        for i in range(filled):
            if not COLORS:
                out.append(G["bar_filled"])
                continue
            dist = abs(i - shimmer_pos)
            t = max(0.0, 1 - dist / _SHIMMER_WIDTH)
            out.append(f"{_amber(t, self._dark, self._bright)}{G['bar_filled']}")
        out.append(f"{RST}{DM}{G['bar_empty'] * empty}{RST}")
        return "".join(out)


# ----------------------------------------------------- progress plumbing ----

_progress = None
_progress_context = ""


def progress_context(text: str) -> None:
    """Set the context suffix for phase block titles (e.g. 'scroll.sif to .png')."""
    global _progress_context
    _progress_context = text
    if _progress is not None:
        _progress.set_context(text)


def progress_start() -> None:
    """Start the shared shimmer animation (quiet mode only)."""
    global _progress
    if _VERBOSE:
        return
    if _progress is None:
        _progress = ShimmerProgress()
        _progress.set_context(_progress_context)


def progress(phase: str, current: float = 0, total: float = 0) -> None:
    """Report progress; no-op in verbose mode (callers print there instead)."""
    if _VERBOSE or _progress is None:
        return
    _progress.on_progress(phase, round(current), round(total))


_tick_counts = {}


def progress_tick(phase: str) -> None:
    """Count-mode progress: 'Resolving dependencies... 12 found'."""
    if _VERBOSE or _progress is None:
        return
    _tick_counts[phase] = _tick_counts.get(phase, 0) + 1
    _progress.on_progress(phase, _tick_counts[phase], 0)


def progress_stop() -> None:
    global _progress
    if _progress is not None:
        _progress.stop()
        _progress = None
    _tick_counts.clear()


if __name__ == "__main__":
    intro("RenderChan")
    log_info("scene.sif")
    progress_start()
    for i in range(0, 101, 5):
        progress("Rendering", i, 100)
        time.sleep(0.05)
    progress("Merging", 100, 100)
    time.sleep(0.3)
    progress_stop()
    warn("something looks odd")
    outro("Completed in 00:00:01")
