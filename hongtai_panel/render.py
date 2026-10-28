"""Pillow renderers for the system-monitor layouts.

Layouts are driven by a Theme (colours plus which metric sits in which slot),
so the GUI's theme editor and the service render identical output.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import Theme
from .sysinfo import Stats, metric

FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
    "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
]


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    try:
        return tuple(int(v[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (255, 0, 255)  # obvious magenta beats a crash on a bad hex string


class Palette:
    """Theme colours resolved to RGB tuples once per render."""

    def __init__(self, theme: Theme):
        self.bg = hex_to_rgb(theme.background)
        self.fg = hex_to_rgb(theme.foreground)
        self.dim = hex_to_rgb(theme.dim)
        self.track = hex_to_rgb(theme.track)
        self.cool = hex_to_rgb(theme.cool)
        self.warm = hex_to_rgb(theme.warm)
        self.hot = hex_to_rgb(theme.hot)

    def load(self, pct: float | None) -> tuple[int, int, int]:
        """Cool below 50%, warm to 80%, hot beyond."""
        if pct is None:
            return self.dim
        pct = max(0.0, min(100.0, pct))
        if pct < 50:
            return lerp(self.cool, self.warm, pct / 50)
        return lerp(self.warm, self.hot, (pct - 50) / 50)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


class Fonts:
    """Lazily-built font cache; TrueType loading is too slow for a render loop."""

    def __init__(self):
        self._cache: dict[int, ImageFont.FreeTypeFont] = {}

    def at(self, size: int) -> ImageFont.FreeTypeFont:
        size = max(6, int(size))
        if size not in self._cache:
            self._cache[size] = _font(size)
        return self._cache[size]


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _center(d: ImageDraw.ImageDraw, xy: tuple, text: str, font, fill) -> None:
    d.text(xy, text, font=font, fill=fill, anchor="mm")


def arc_gauge(d, cx, cy, radius, pct, label, sub, fonts, pal, width=14) -> None:
    """A 270-degree arc gauge with a centered value."""
    start, sweep = 135, 270
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    d.arc(box, start, start + sweep, fill=pal.track, width=width)

    if pct is not None:
        shown = max(0.0, min(100.0, pct))
        if shown > 0.5:
            d.arc(box, start, start + sweep * shown / 100, fill=pal.load(pct), width=width)
        value = f"{pct:.0f}"
    else:
        value = "--"

    _center(d, (cx, cy - radius // 6), value, fonts.at(radius * 0.62), pal.fg)
    _center(d, (cx, cy + radius // 3), label, fonts.at(radius * 0.26), pal.dim)
    if sub:
        _center(d, (cx, cy + int(radius * 0.60)), sub, fonts.at(radius * 0.24), pal.dim)


def bar(d, x, y, w, h, pct, label, value, fonts, pal) -> None:
    """A labelled horizontal meter."""
    f = fonts.at(max(11, h - 2))
    d.text((x, y - h - 4), label, font=f, fill=pal.dim)
    d.text((x + w, y - h - 4), value, font=f, fill=pal.fg, anchor="ra")
    d.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=pal.track)
    if pct is not None:
        fill_w = int(w * max(0.0, min(100.0, pct)) / 100)
        if fill_w > h:
            d.rounded_rectangle((x, y, x + fill_w, y + h), radius=h // 2, fill=pal.load(pct))


def _net_row(d, w, y, stats, fonts, pal, s) -> None:
    """Up/down rates with drawn triangles.

    Arrow glyphs are missing from some default fonts, so they are drawn as
    polygons rather than rendered as text.
    """
    font = fonts.at(20 * s)
    up_text = f"{stats.net_up_mbps:.1f}"
    down_text = f"{stats.net_down_mbps:.1f} Mb/s"
    tri, gap = int(7 * s), int(10 * s)

    up_w = d.textlength(up_text, font=font)
    down_w = d.textlength(down_text, font=font)
    total = tri * 4 + gap * 2 + up_w + down_w + int(26 * s)
    x = int(w // 2 - total // 2)

    d.polygon([(x, y + tri), (x + tri * 2, y + tri), (x + tri, y - tri)], fill=pal.dim)
    x += tri * 2 + gap
    d.text((x, y), up_text, font=font, fill=pal.dim, anchor="lm")
    x += up_w + int(26 * s)
    d.polygon([(x, y - tri), (x + tri * 2, y - tri), (x + tri, y + tri)], fill=pal.dim)
    x += tri * 2 + gap
    d.text((x, y), down_text, font=font, fill=pal.dim, anchor="lm")


def render_gauges(stats: Stats, size: tuple[int, int], fonts: Fonts, theme: Theme,
                  transparent: bool = False) -> Image.Image:
    """Arcs across the top, meters beneath, optional network and footer rows."""
    w, h = size
    pal = Palette(theme)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if transparent \
        else Image.new("RGB", (w, h), pal.bg)
    d = ImageDraw.Draw(img)
    s = min(w, h) / 480.0  # the design grid is 480x480

    arcs = theme.arcs[:3] or ["cpu"]
    radius = int((88 if len(arcs) < 3 else 66) * s)
    cy = int(130 * s)
    for i, key in enumerate(arcs):
        cx = int(w * (i + 0.5) / len(arcs))
        label, pct, sub = metric(key, stats)
        arc_gauge(d, cx, cy, radius, pct, label, sub, fonts, pal, width=int(14 * s))

    margin = int(44 * s)
    bar_w = w - margin * 2
    bar_h = int(18 * s)
    y = int(280 * s)
    step = int(52 * s)

    for i, key in enumerate(theme.bars[:3]):
        label, pct, sub = metric(key, stats)
        bar(d, margin, y + step * i, bar_w, bar_h, pct, label, sub, fonts, pal)

    below = y + step * len(theme.bars[:3])
    if theme.show_network:
        _net_row(d, w, below + int(6 * s), stats, fonts, pal, s)

    if theme.show_footer:
        parts = []
        if stats.cpu_freq:
            parts.append(f"{stats.cpu_freq:.2f} GHz")
        parts.append(f"up {stats.uptime_hours:.0f}h")
        _center(d, (w // 2, h - int(24 * s)), "   ".join(parts), fonts.at(17 * s), pal.dim)
    return img


def render_compact(stats: Stats, size: tuple[int, int], fonts: Fonts, theme: Theme,
                   transparent: bool = False) -> Image.Image:
    """A 2x2 grid of large readouts, legible from across a desk."""
    w, h = size
    pal = Palette(theme)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if transparent \
        else Image.new("RGB", (w, h), pal.bg)
    d = ImageDraw.Draw(img)
    s = min(w, h) / 480.0

    cells = (theme.cells + ["cpu"] * 4)[:4]
    for i, key in enumerate(cells):
        label, pct, sub = metric(key, stats)
        cx = w // 4 + (w // 2) * (i % 2)
        cy = h // 4 + (h // 2) * (i // 2)
        _center(d, (cx, cy - int(34 * s)), label, fonts.at(26 * s), pal.dim)
        _center(d, (cx, cy + int(10 * s)),
                f"{pct:.0f}" if pct is not None else "--",
                fonts.at(78 * s), pal.load(pct))
        _center(d, (cx, cy + int(58 * s)), sub, fonts.at(24 * s), pal.dim)

    line = max(1, int(2 * s))
    d.line((w // 2, int(30 * s), w // 2, h - int(30 * s)), fill=pal.track, width=line)
    d.line((int(30 * s), h // 2, w - int(30 * s), h // 2), fill=pal.track, width=line)
    return img


LAYOUTS = {
    "gauges": render_gauges,
    "compact": render_compact,
}

# Backwards-compatible alias; older code referred to layouts as themes.
THEMES = LAYOUTS


def render_bars(bands: list[float], size: tuple[int, int], theme: Theme,
                transparent: bool = False) -> Image.Image:
    """A vertical spectrum-bar visualizer. `bands` are levels in 0..100."""
    w, h = size
    pal = Palette(theme)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if transparent \
        else Image.new("RGB", (w, h), pal.bg)
    if not bands:
        return img
    d = ImageDraw.Draw(img)
    n = len(bands)
    gap = max(1, w // (n * 8))
    bar_w = (w - gap * (n - 1)) / n
    for i, level in enumerate(bands):
        level = max(0.0, min(100.0, level))
        bar_h = h * level / 100
        x0 = i * (bar_w + gap)
        x1 = x0 + bar_w
        d.rectangle((x0, h - bar_h, x1, h), fill=pal.load(level))
    return img


def render_scope(samples: list[float], size: tuple[int, int], theme: Theme,
                 transparent: bool = False) -> Image.Image:
    """An oscilloscope trace of the raw waveform. `samples` are in -1..1."""
    w, h = size
    pal = Palette(theme)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0)) if transparent \
        else Image.new("RGB", (w, h), pal.bg)
    if len(samples) < 2:
        return img
    d = ImageDraw.Draw(img)
    mid = h / 2
    points = [
        (i * w / (len(samples) - 1), mid - max(-1.0, min(1.0, s)) * mid * 0.9)
        for i, s in enumerate(samples)
    ]
    d.line(points, fill=pal.cool, width=max(1, round(h / 160)), joint="curve")
    return img


# -- stateful spectrum visualizers -------------------------------------
#
# These keep per-instance state (falling peaks, particles, rain columns) and
# are fed the latest spectrum levels (0..100) once per frame.

def _canvas(size: tuple[int, int], pal: Palette, transparent: bool) -> Image.Image:
    w, h = size
    return Image.new("RGBA", (w, h), (0, 0, 0, 0)) if transparent \
        else Image.new("RGB", (w, h), pal.bg)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class Peaks:
    """Spectrum bars with slowly falling peak caps."""

    def __init__(self):
        self.caps: list[float] = []

    def draw(self, bands, size, theme, transparent=False):
        w, h = size
        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        n = len(bands)
        if not n:
            return img
        if len(self.caps) != n:
            self.caps = [0.0] * n
        d = ImageDraw.Draw(img)
        gap = max(1, w // (n * 8))
        bar_w = (w - gap * (n - 1)) / n
        cap_h = max(2, h // 60)
        for i, level in enumerate(bands):
            level = max(0.0, min(100.0, level))
            self.caps[i] = max(level, self.caps[i] - 1.2)
            x0 = i * (bar_w + gap)
            x1 = x0 + bar_w
            d.rectangle((x0, h - h * level / 100, x1, h), fill=pal.load(level))
            top = h - h * self.caps[i] / 100
            d.rectangle((x0, top - cap_h, x1, top), fill=pal.fg)
        return img


class Mirror:
    """cliamp's "mirror": thin dot bars pulsing about a horizontal axis.

    Bars span the middle 84% of the panel, one dot wide with one-dot gaps.
    Their height follows the overall loudness, tapers toward the edges, and
    wobbles on two slow sine waves so the shape keeps moving. The axis is
    cool, bar bodies warm, and the outer quarter of each bar hot.
    """

    def draw(self, bands, size, theme, transparent=False):
        import math
        import time

        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        grid = _dot_grid(size)
        _, cols, rows, _, _ = grid
        if cols < 4 or rows < 4:
            return img
        span = max(2, cols * 84 // 100)
        span = min(cols, span - span % 2)
        count = max(1, span // 2)
        x0 = (cols - span) // 2
        axis = rows // 2
        max_r = min(axis, rows - 1 - axis)
        dots = {(x, axis): 0 for x in range(x0, x0 + span)}
        env = sum(_clamp01(b / 100) for b in bands) / len(bands) if bands else 0.0
        t = time.monotonic()
        half = (count - 1) / 2
        for i in range(count):
            dist = abs(i - half) / half if half > 0 else 0.0
            wobble = 0.4 + 0.6 * abs(math.sin(t * 4.6 + i * 0.42)
                                     * math.sin(t * 1.9 - i * 0.13))
            amp = rows * 0.8 * (1 - dist * 0.55) * (0.3 + 0.7 * env) * (0.35 + 0.65 * wobble)
            radius = min(max_r, max(1, round(amp)))
            x = x0 + i * 2 + 1
            for y in range(axis - radius, axis + radius + 1):
                dots[(x, y)] = 2 if abs(y - axis) / radius >= 0.75 else 1
        _paint_dots(img, dots, pal, grid)
        return img


class Radial:
    """A ring of spokes whose length follows the spectrum."""

    def draw(self, bands, size, theme, transparent=False):
        import math

        w, h = size
        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        n = len(bands)
        if not n:
            return img
        d = ImageDraw.Draw(img)
        cx, cy = w / 2, h / 2
        r0 = min(w, h) * 0.22
        r_max = min(w, h) * 0.48 - r0
        cols = list(bands) + list(reversed(bands))
        m = len(cols)
        width = max(2, int(2 * math.pi * r0 / m * 0.7))
        for i, level in enumerate(cols):
            level = max(0.0, min(100.0, level))
            a = 2 * math.pi * i / m - math.pi / 2
            r1 = r0 + r_max * level / 100
            d.line((cx + math.cos(a) * r0, cy + math.sin(a) * r0,
                    cx + math.cos(a) * r1, cy + math.sin(a) * r1),
                   fill=pal.load(level), width=width)
        d.ellipse((cx - r0, cy - r0, cx + r0, cy + r0), outline=pal.track,
                  width=max(1, h // 200))
        return img


class Geyser:
    """Particles fired upward from each band; louder bands spout higher."""

    def __init__(self):
        self.parts: list[list[float]] = []  # x, y, vx, vy, level
        import random
        self.rng = random.Random()

    def draw(self, bands, size, theme, transparent=False):
        w, h = size
        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        n = len(bands)
        if not n:
            return img
        rng = self.rng
        colw = w / n
        gravity = h * 0.0035
        for i, level in enumerate(bands):
            level = max(0.0, min(100.0, level))
            if level < 4:
                continue
            for _ in range(1 + int(level / 35)):
                speed = h * (0.02 + 0.045 * level / 100) * rng.uniform(0.75, 1.1)
                self.parts.append([(i + rng.random()) * colw, float(h),
                                   rng.uniform(-0.25, 0.25) * colw * 0.3,
                                   -speed, level])
        d = ImageDraw.Draw(img)
        r = max(2, h // 90)
        alive = []
        for p in self.parts:
            p[0] += p[2]
            p[1] += p[3]
            p[3] += gravity
            if p[1] > h and p[3] > 0:
                continue
            alive.append(p)
            # Colour by height reached: cool near the ground, hot at the peak.
            d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r),
                      fill=pal.load(100 * (1 - p[1] / h) * 1.1))
        self.parts = alive[-1500:]
        return img


class Matrix:
    """Falling digital rain; each column's speed and brightness follow a band."""

    def __init__(self):
        import random
        self.rng = random.Random()
        self.cols: list[dict] = []
        self.cell = 0
        self.chars = "01234567890:;<>=+*#$%&@"

    def draw(self, bands, size, theme, transparent=False):
        w, h = size
        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        n = len(bands)
        if not n:
            return img
        fonts = getattr(self, "fonts", None)
        if fonts is None:
            fonts = self.fonts = Fonts()
        cell = max(10, h // 20)
        ncols = max(1, w // cell)
        if len(self.cols) != ncols or self.cell != cell:
            self.cell = cell
            self.cols = [{"y": self.rng.uniform(-20, 0), "trail": self.rng.randint(6, 16)}
                         for _ in range(ncols)]
        font = fonts.at(int(cell * 0.9))
        d = ImageDraw.Draw(img)
        rows = h / cell
        for c, col in enumerate(self.cols):
            level = bands[min(n - 1, c * n // ncols)] / 100
            col["y"] += 0.15 + level * 0.9
            if col["y"] - col["trail"] > rows:
                col["y"] = self.rng.uniform(-8, 0)
                col["trail"] = self.rng.randint(6, 16)
            head = int(col["y"])
            for k in range(col["trail"]):
                row = head - k
                if row < 0 or row >= rows:
                    continue
                fade = 1 - k / col["trail"]
                # Head of the trail is bright; the body fades toward the background.
                base = pal.fg if k == 0 else pal.cool
                colour = lerp(pal.bg, base, _clamp01(fade * (0.35 + level)))
                ch = self.chars[self.rng.randrange(len(self.chars))]
                d.text((c * cell, row * cell), ch, font=font, fill=colour)
        return img


class Flame:
    """Classic fire: a low-res heat grid fed from the bottom by the spectrum.

    Each band heats its slice of the bottom row; heat rises with sideways
    jitter and cools as it goes, then is mapped through the theme colours
    (background -> hot -> warm -> foreground) and upscaled.
    """

    GW, GH = 48, 36

    def __init__(self):
        import random
        self.rng = random.Random()
        self.heat = [[0.0] * self.GW for _ in range(self.GH)]
        self.lut: list[tuple] | None = None
        self.lut_key = None

    def _palette(self, pal: Palette) -> list[tuple]:
        key = (pal.bg, pal.hot, pal.warm, pal.fg)
        if self.lut_key != key:
            stops = [pal.bg, pal.hot, pal.warm, pal.fg]
            self.lut = []
            for i in range(256):
                t = i / 255 * (len(stops) - 1)
                k = min(int(t), len(stops) - 2)
                self.lut.append(lerp(stops[k], stops[k + 1], t - k))
            self.lut_key = key
        return self.lut

    def draw(self, bands, size, theme, transparent=False):
        w, h = size
        pal = Palette(theme)
        n = len(bands)
        gw, gh, rng = self.GW, self.GH, self.rng
        heat = self.heat
        bottom = heat[-1]
        for x in range(gw):
            level = bands[min(n - 1, x * n // gw)] / 100 if n else 0.0
            bottom[x] = min(1.0, level * rng.uniform(0.85, 1.25))
        # Sweep top-down so each row reads the not-yet-updated row below it.
        for y in range(gh - 1):
            below = heat[y + 1]
            row = heat[y]
            for x in range(gw):
                src = min(gw - 1, max(0, x + rng.randint(-1, 1)))
                v = (below[src] + below[x]) / 2 - rng.uniform(0.0, 0.07)
                row[x] = v if v > 0 else 0.0
        lut = self._palette(pal)
        small = Image.new("RGB", (gw, gh))
        small.putdata([lut[min(255, int(v * 255))] for row in heat for v in row])
        img = small.resize((w, h), Image.BILINEAR)
        if transparent:
            # Fade the background out so only the flame remains.
            img = img.convert("RGBA")
            bg = pal.bg
            img.putdata([(r, g, b, 0 if (r, g, b) == bg else 255) for r, g, b in
                         img.convert("RGB").getdata()])
        return img


class Heartbeat:
    """A scrolling ECG trace that pulses on bass hits and mid-range onsets.

    The trace is a rolling buffer of samples (-1..1). Each frame it scrolls
    left; when low-band energy jumps above its running average a full P-QRS-T
    beat is queued, and when mid-band energy does the same a smaller, sharper
    blip is queued. Pulses are summed, so a bass and a mid hit landing
    together overlap instead of cancelling each other.
    """

    # One beat: small P wave, sharp Q-R-S spike, broad T wave.
    BEAT = [0.0, 0.08, 0.15, 0.08, 0.0, 0.0, -0.12, 0.9, -0.35, 0.0, 0.0,
            0.0, 0.1, 0.22, 0.28, 0.22, 0.1, 0.0]
    # A mid-range onset: a short, narrow tick that dips then rises.
    BLIP = [0.0, -0.15, 0.45, -0.2, 0.0, 0.08, 0.0]

    def __init__(self, width: int = 160):
        self.width = width
        self.buf = [0.0] * width
        self.queue: list[float] = []
        self.avg = {"bass": 0.0, "mid": 0.0}
        self.cool = {"bass": 0, "mid": 0}

    def _onset(self, name: str, level: float, floor: float, ratio: float,
               cooldown: int) -> bool:
        """True when `level` jumps above its running average (a hit)."""
        self.avg[name] += (level - self.avg[name]) * 0.08
        self.cool[name] -= 1
        if self.cool[name] <= 0 and level > floor and level > self.avg[name] * ratio:
            self.cool[name] = cooldown
            return True
        return False

    def _add(self, pulse: list[float], amp: float) -> None:
        if len(self.queue) < len(pulse):
            self.queue += [0.0] * (len(pulse) - len(self.queue))
        for i, v in enumerate(pulse):
            self.queue[i] += v * amp

    def draw(self, bands, size, theme, transparent=False):
        w, h = size
        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        n = len(bands)
        lo = max(1, n // 6)
        hi = max(lo + 1, n // 2)
        bass = sum(bands[:lo]) / lo if n else 0.0
        mids = sum(bands[lo:hi]) / max(1, len(bands[lo:hi])) if n else 0.0
        if self._onset("bass", bass, 25, 1.3, 8):
            self._add(self.BEAT, 0.5 + 0.5 * _clamp01(bass / 100))
        if self._onset("mid", mids, 20, 1.25, 4):
            self._add(self.BLIP, 0.4 + 0.6 * _clamp01(mids / 100))
        # Advance a few samples per frame; idle samples carry a little noise.
        for _ in range(3):
            v = self.queue.pop(0) if self.queue else 0.0
            self.buf.append(v)
        del self.buf[: -self.width]
        d = ImageDraw.Draw(img)
        mid = h * 0.55
        scale = h * 0.4
        m = len(self.buf)
        pts = [(i * w / (m - 1), mid - v * scale) for i, v in enumerate(self.buf)]
        # Grid lines for the monitor look.
        step = max(8, h // 8)
        for gy in range(0, h, step):
            d.line((0, gy, w, gy), fill=pal.track, width=1)
        # Older samples fade: draw the trace in segments from dim to bright.
        segs = 6
        per = max(1, (m - 1) // segs)
        lw = max(2, round(h / 120))
        for k in range(segs):
            a, b = k * per, m if k == segs - 1 else (k + 1) * per + 1
            colour = lerp(pal.bg, pal.cool, 0.25 + 0.75 * (k + 1) / segs)
            d.line(pts[a:b], fill=colour, width=lw, joint="curve")
        hx, hy = pts[-1]
        r = lw * 2
        d.ellipse((hx - r, hy - r, hx + r, hy + r), fill=pal.fg)
        return img


def _dot_grid(size: tuple[int, int], divisions: int = 64) -> tuple[int, int, int, float, float]:
    """A braille-style dot grid for the panel: (pitch, cols, rows, x_off, y_off).

    `divisions` is roughly how many dots span the panel's height; more makes a
    finer grid for visualizers that draw thin lines.
    """
    w, h = size
    pitch = max(4, h // divisions)
    cols, rows = w // pitch, h // pitch
    return pitch, cols, rows, (w - cols * pitch + pitch) / 2, (h - rows * pitch + pitch) / 2


def _draw_dots(img: Image.Image, dots, pal: Palette, grid) -> None:
    """Draw (col, row) dots coloured by height band, as cliamp does: cool in
    the bottom 30%, warm to 60%, hot above."""
    rows = grid[2]

    def band(row: int) -> int:
        norm = 1 - row / rows
        return 2 if norm >= 0.6 else 1 if norm >= 0.3 else 0

    _paint_dots(img, {(c, r): band(r) for c, r in dots}, pal, grid)


def _paint_dots(img: Image.Image, tiers: dict, pal: Palette, grid, colours=None) -> None:
    """Draw dots from a {(col, row): tier} map. Tiers index `colours`, which
    defaults to cool/warm/hot."""
    pitch, _, _, x_off, y_off = grid
    colours = colours or (pal.cool, pal.warm, pal.hot)
    r = max(1.0, pitch * 0.32)
    d = ImageDraw.Draw(img)
    for (col, row), tier in tiers.items():
        cx, cy = x_off + col * pitch, y_off + row * pitch
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colours[tier])


class Wave:
    """cliamp's "wave": the raw waveform plotted on a braille-style dot grid.

    Samples are downsampled to one dot row per dot column, and each column
    also lights every dot back to the previous column's row so the trace is
    continuous. `samples` are in -1..1.
    """

    def draw(self, samples, size, theme, transparent=False):
        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        grid = _dot_grid(size)
        _, cols, rows, _, _ = grid
        n = len(samples)
        if cols < 2 or rows < 2:
            return img
        dots = []
        prev = None
        for x in range(cols):
            v = samples[min(n - 1, x * n // cols)] if n else 0.0
            v = max(-1.0, min(1.0, v))
            y = max(0, min(rows - 1, int((1 - v) * (rows - 1) / 2)))
            prev = y if prev is None else prev
            dots += [(x, row) for row in range(min(y, prev), max(y, prev) + 1)]
            prev = y
        _draw_dots(img, dots, pal, grid)
        return img


class Lissajous:
    """cliamp's "scope": an XY oscilloscope drawn on the dot grid.

    The audio is mono, so X is the signal and Y is a delayed copy of it. The
    delay slowly wobbles over time, so pure tones trace circles and ellipses
    while music ties itself into evolving knots. `samples` are in -1..1.
    """

    window = 2048  # wants a longer stretch of audio than the plain scope

    def __init__(self):
        self.frame = 0

    def draw(self, samples, size, theme, transparent=False):
        import math

        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        grid = _dot_grid(size)
        _, cols, rows, _, _ = grid
        n = len(samples)
        self.frame += 1
        if n < 2 or cols < 2 or rows < 2:
            return img
        wobble = int(math.sin(self.frame * 0.02) * (n // 8))
        delay = max(1, min(n - 1, n // 4 + wobble))
        step = max(1, (n - delay) // 512)
        dots = set()
        prev = None
        for i in range(0, n - delay, step):
            x = max(-1.0, min(1.0, samples[i]))
            y = max(-1.0, min(1.0, samples[i + delay]))
            px = max(0, min(cols - 1, int((x + 1) / 2 * (cols - 1))))
            py = max(0, min(rows - 1, int((1 - y) / 2 * (rows - 1))))
            dots.add((px, py))
            if prev is not None:
                # Fill in short gaps so the figure reads as a continuous curve.
                dx, dy = px - prev[0], py - prev[1]
                steps = max(abs(dx), abs(dy))
                if 0 < steps < 30:
                    for k in range(1, steps):
                        dots.add((prev[0] + dx * k // steps, prev[1] + dy * k // steps))
            prev = (px, py)
        _draw_dots(img, dots, pal, grid)
        return img


class Retro:
    """cliamp's "retro": an 80s synthwave scene on the dot grid.

    A striped setting sun sits above the horizon, the spectrum rides the
    horizon as a smooth wave, and a perspective grid floor scrolls toward the
    viewer. The grid is cool, the sun warm and the wave hot.
    """

    def draw(self, bands, size, theme, transparent=False):
        import math
        import time

        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        grid = _dot_grid(size)
        _, cols, rows, _, _ = grid
        if cols < 4 or rows < 4:
            return img
        horizon = max(rows * 2 // 5, 2)
        floor_rows = rows - horizon
        cx = (cols - 1) / 2
        dots: dict = {}

        # Sun: a semicircle above the horizon, its lower half striped.
        sun_r = horizon * 0.85
        stripe = max(1, int(sun_r * 0.15))
        for y in range(horizon):
            above = horizon - y
            if above > sun_r:
                continue
            if above < sun_r * 0.5 and (int(above) // stripe) % 2 == 1:
                continue
            half_w = math.sqrt(sun_r * sun_r - above * above)
            for x in range(max(0, int(cx - half_w)), min(cols - 1, int(cx + half_w)) + 1):
                dots[(x, y)] = 1

        # Floor: horizon line, lines converging on the vanishing point, and
        # horizontal lines that are dense near the horizon and scroll forward.
        for x in range(cols):
            dots[(x, horizon)] = 0
        for i in range(19):
            bottom_x = i * (cols - 1) / 18
            for y in range(horizon + 1, rows):
                t = (y - horizon) / max(1, floor_rows - 1)
                x = round(cx + (bottom_x - cx) * t)
                if 0 <= x < cols:
                    dots[(x, y)] = 0
        scroll = (time.monotonic() * 1.6) % 1.0
        for i in range(10):
            z = (i + scroll) / 10 % 1.0
            y = horizon + 1 + int(z * z * max(1, floor_rows - 2))
            if horizon < y < rows:
                for x in range(cols):
                    dots[(x, y)] = 0

        # Wave: the spectrum, cosine-interpolated, rising from the horizon.
        n = len(bands)
        max_wave = horizon * 0.85
        prev = None
        for x in range(cols):
            if n:
                f = x / max(1, cols - 1) * (n - 1)
                i = int(f)
                mu = (1 - math.cos((f - i) * math.pi)) / 2
                level = bands[i] if i >= n - 1 else bands[i] * (1 - mu) + bands[i + 1] * mu
                level = _clamp01(level / 100)
            else:
                level = 0.0
            y = max(0, min(rows - 1, horizon - int(max(0.03, level) * max_wave)))
            prev = y if prev is None else prev
            for yy in range(min(y, prev), max(y, prev) + 1):
                dots[(x, yy)] = 2
            prev = y
        _paint_dots(img, dots, pal, grid)
        return img


def _star_hash(i: int, slot: int) -> float:
    """Deterministic pseudo-random value in [0, 1) for star `i`, one per slot.

    splitmix64's finaliser: merely scaling i would put the stars on a few
    diagonals a fixed step apart, so the index is avalanched instead.
    """
    mask = (1 << 64) - 1
    x = (i * 0x9E3779B97F4A7C15 + (slot + 1) * 0xD1B54A32D192ED03) & mask
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & mask
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & mask
    x ^= x >> 31
    return (x >> 11) / (1 << 53)


class RedSector:
    """cliamp's "redsector": a wireframe equalizer after the Red Sector Inc.
    RSI Megademo (Amiga, 1989).

    Five hollow bars stand on a common ground line, each driven by two
    spectrum groups, and the whole set tumbles as a rigid body in front of a
    drifting starfield. Hidden edges are dropped by per-face backface culling,
    so a bar shows its front, one flank and its cap.

    Each bar tracks its own ceiling and floor and shows where the level sits
    between them: every band rests at its own level and only moves a little
    around it, so absolute heights would draw the shape of the mix, not the
    beat. Bars are coloured by height (cool/warm/hot); stars are dimmed
    versions of the same colours and always sit behind the bars.
    """

    BARS = 5
    HALF_W = HALF_D = 0.26
    PITCH = 0.78  # centre-to-centre distance between bars
    GROUND_Y = -1.05
    MIN_H, MAX_H = 0.40, 2.30
    FOCAL, CAMERA_Z, ZOOM_AMP = 3.2, 6.0, 1.5
    MAX_STRETCH, COMFORT_ROWS = 2.0, 40
    # Radians per frame; the object tumbles about two axes at unrelated rates.
    SPIN_Y, SPIN_X, ZOOM_RATE = 0.105, 0.073, 0.011
    RELAX, MIN_ENVELOPE = 0.001, 0.06
    DOTS_PER_STAR, MIN_STARS, MAX_STARS = 130, 6, 90
    STAR_DIM = 0.6
    # The motion constants are per frame at the original's ~20 fps; scale by
    # elapsed time so the speed does not depend on the panel's frame rate.
    REF_FPS = 20.0

    CORNER_X = (-1, 1, 1, -1, -1, 1, 1, -1)
    CORNER_Z = (-1, -1, -1, -1, 1, 1, 1, 1)
    CORNER_TOP = (False, False, True, True, False, False, True, True)
    # Wound so a face is visible when its normal points away from the viewer.
    FACES = ((0, 1, 2, 3), (5, 4, 7, 6), (0, 3, 7, 4), (1, 5, 6, 2), (3, 2, 6, 7), (0, 4, 5, 1))

    def __init__(self):
        self.ceiling = [0.0] * self.BARS
        self.floor = [1.0] * self.BARS  # start wide open; the first frames close in
        self.heights = [self.MIN_H] * self.BARS
        self.frame = 0.0
        self.last: float | None = None

    @staticmethod
    def _groups(bands, count: int = 10) -> list[float]:
        """Fold the spectrum into `count` log-spaced groups, as 0..1 levels.

        The FFT bands are linear in frequency, so an even split would give
        the bass a single narrow group; a geometric split matches what the
        original's ten bands cover.
        """
        n = len(bands)
        if not n:
            return [0.0] * count
        out, start = [], 0
        for k in range(1, count + 1):
            end = n if k == count else min(n, max(start + 1, round(n ** (k / count))))
            out.append(_clamp01(max(bands[start:end] or [0.0]) / 100))
            start = min(end, n - 1)
        return out

    def _advance(self, bands, k: float) -> None:
        levels = self._groups(bands)
        for i in range(self.BARS):
            # The louder of the pair: averaging would let a silent band halve
            # its partner, and the top band is empty on most material.
            level = max(levels[i * 2], levels[i * 2 + 1])
            if level > self.ceiling[i]:
                self.ceiling[i] = level
            else:
                self.ceiling[i] -= self.RELAX * k
            if level < self.floor[i]:
                self.floor[i] = level
            else:
                self.floor[i] += self.RELAX * k
            if self.ceiling[i] < self.floor[i] + self.MIN_ENVELOPE:
                self.ceiling[i] = self.floor[i] + self.MIN_ENVELOPE
            norm = _clamp01((level - self.floor[i]) / (self.ceiling[i] - self.floor[i]))
            target = self.MIN_H + norm * (self.MAX_H - self.MIN_H)
            # Rise fast, fall slow, so a transient reads as a hit not a flicker.
            rate = 0.75 if target > self.heights[i] else 0.28
            self.heights[i] += (target - self.heights[i]) * (1 - (1 - rate) ** k)

    def _place(self, x, y, z, cx, sx, cy, sy, cam_z):
        """Rotate about Y then X and project: (rx, ry, rz, px, py)."""
        x1 = x * cy + z * sy
        z1 = -x * sy + z * cy
        y1 = y * cx - z1 * sx
        z2 = y * sx + z1 * cx
        f = self.FOCAL / max(0.35, z2 + cam_z)
        return x1, y1, z2, x1 * f, y1 * f

    def _fit(self, rows, cols, cx, sx, cy, sy, cam_z):
        """Scale against a hull the object can never exceed, not the live
        shape, which would blow a quiet picture up and look motionless."""
        import math

        hull_x = (self.BARS - 1) / 2 * self.PITCH + self.HALF_W
        pts = [self._place(a * hull_x, y, b * self.HALF_D, cx, sx, cy, sy, cam_z)
               for a in (-1, 1) for y in (self.GROUND_Y, self.GROUND_Y + self.MAX_H)
               for b in (-1, 1)]
        xs, ys = [p[3] for p in pts], [p[4] for p in pts]
        span_x = max(max(xs) - min(xs), 0.001)
        span_y = max(max(ys) - min(ys), 0.001)
        # Breathe in and out, as the demo pulls the object toward the viewer.
        zoom = 0.55 + 0.30 * (0.5 + 0.5 * math.sin(self.frame * self.ZOOM_RATE))
        fit_y = (rows - 1) / span_y * zoom
        # A short panel starves the object of height, so widen to keep the bars apart.
        stretch = min(self.MAX_STRETCH, max(1.0, self.COMFORT_ROWS / rows))
        fit_x = min(fit_y * stretch, (cols - 1) / span_x)
        return (fit_x, fit_y, cols / 2 - (min(xs) + max(xs)) / 2 * fit_x,
                rows / 2 + (min(ys) + max(ys)) / 2 * fit_y)

    @staticmethod
    def _visible(p1, p2, p3, cam_z) -> bool:
        ux, uy, uz = (p2[i] - p1[i] for i in range(3))
        vx, vy, vz = (p3[i] - p2[i] for i in range(3))
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        return nx * p1[0] + ny * p1[1] + nz * (p1[2] + cam_z) > 0

    def draw(self, bands, size, theme, transparent=False):
        import math
        import time

        pal = Palette(theme)
        img = _canvas(size, pal, transparent)
        grid = _dot_grid(size, 80)
        _, cols, rows, _, _ = grid
        if cols < 16 or rows < 4:
            return img

        now = time.monotonic()
        k = min(4.0, (now - self.last) * self.REF_FPS) if self.last else 1.0
        self.last = now
        self.frame += k
        self._advance(bands, k)

        dots: dict = {}

        def put(x: int, y: int, tag: int) -> None:
            if 0 <= x < cols and 0 <= y < rows and tag > dots.get((x, y), -1):
                dots[(x, y)] = tag

        # Tags 0-3 are the stars, 4-6 the bars; a bar always wins a shared dot.
        count = min(self.MAX_STARS, max(self.MIN_STARS, rows * cols // self.DOTS_PER_STAR))
        for i in range(1, count + 1):
            speed = 0.10 + (i % 3) * 0.09  # three speed lanes: a shallow sense of depth
            x = (_star_hash(i, 0) * cols - self.frame * speed) % cols
            put(int(x), int(_star_hash(i, 1) * rows), int(_star_hash(i, 2) * 4))

        cy_, sy_ = math.cos(self.frame * self.SPIN_Y), math.sin(self.frame * self.SPIN_Y)
        cx_, sx_ = math.cos(self.frame * self.SPIN_X), math.sin(self.frame * self.SPIN_X)
        cam_z = self.CAMERA_Z + math.sin(self.frame * self.ZOOM_RATE) * self.ZOOM_AMP
        fit_x, fit_y, ctr_x, ctr_y = self._fit(rows, cols, cx_, sx_, cy_, sy_, cam_z)

        for bar in range(self.BARS):
            base_x = (bar - (self.BARS - 1) / 2) * self.PITCH
            top_y = self.GROUND_Y + self.heights[bar]
            shown = (self.heights[bar] - self.MIN_H) / (self.MAX_H - self.MIN_H)
            tag = 6 if shown >= 0.6 else 5 if shown >= 0.3 else 4
            r3, p2 = [], []
            for c in range(8):
                y = top_y if self.CORNER_TOP[c] else self.GROUND_Y
                rx, ry, rz, sx, sy = self._place(
                    base_x + self.CORNER_X[c] * self.HALF_W, y, self.CORNER_Z[c] * self.HALF_D,
                    cx_, sx_, cy_, sy_, cam_z)
                r3.append((rx, ry, rz))
                p2.append((ctr_x + sx * fit_x, ctr_y - sy * fit_y))
            for face in self.FACES:
                if not self._visible(r3[face[0]], r3[face[1]], r3[face[2]], cam_z):
                    continue
                for e in range(4):
                    (x0, y0), (x1, y1) = p2[face[e]], p2[face[(e + 1) % 4]]
                    steps = min(512, int(max(abs(x1 - x0), abs(y1 - y0))) + 1)
                    for t in range(steps + 1):
                        f = t / steps
                        put(math.floor(x0 + (x1 - x0) * f + 0.5),
                            math.floor(y0 + (y1 - y0) * f + 0.5), tag)

        dim = lambda c: lerp(pal.bg, c, self.STAR_DIM)  # noqa: E731
        colours = (pal.dim, dim(pal.cool), dim(pal.warm), dim(pal.hot),
                   pal.cool, pal.warm, pal.hot)
        _paint_dots(img, dots, pal, grid, colours)
        return img


class _Bars:
    def draw(self, bands, size, theme, transparent=False):
        return render_bars(bands, size, theme, transparent)


class _Scope:
    def draw(self, samples, size, theme, transparent=False):
        return render_scope(samples, size, theme, transparent)


# name -> factory for an object with draw(data, size, theme, transparent).
VISUALIZERS = {
    "bars": _Bars,
    "peaks": Peaks,
    "mirror": Mirror,
    "radial": Radial,
    "geyser": Geyser,
    "matrix": Matrix,
    "flame": Flame,
    "heartbeat": Heartbeat,
    "wave": Wave,
    "lissajous": Lissajous,
    "retro": Retro,
    "redsector": RedSector,
    "scope": _Scope,
}

# Which audio tap feeds each visualizer; anything not listed uses the spectrum.
WAVEFORM_VISUALIZERS = {"scope", "wave", "lissajous"}


def shadow_for(layer: Image.Image, radius: int = 5, opacity: float = 0.85) -> Image.Image:
    """A soft dark halo matching the layer's shape.

    A scrim alone cannot guarantee contrast — a light background swallows the
    muted text, a dark one swallows dark elements. A blurred copy of the layer's
    own alpha, painted black underneath it, keeps every element readable on any
    background without having to know anything about that background.
    """
    from PIL import ImageFilter

    alpha = layer.getchannel("A").filter(ImageFilter.GaussianBlur(radius))
    alpha = alpha.point(lambda v: int(v * opacity))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(alpha)
    return shadow


def compose(background: Image.Image, layer: Image.Image, scrim: float,
            shadow: Image.Image | None = None,
            auto_contrast: bool = False) -> Image.Image:
    """Dim the background, then stack the shadow and stats layer on top."""
    base = apply_scrim(background, scrim, auto_contrast)
    if shadow is not None:
        base.paste(shadow, (0, 0), shadow)
    base.paste(layer, (0, 0), layer)
    return base


def render(stats: Stats, size: tuple[int, int], fonts: Fonts, theme: Theme,
           layout: str = "gauges", background: Image.Image | None = None) -> Image.Image:
    """Draw the stats layout, optionally over a background image."""
    fn = LAYOUTS.get(layout, render_gauges)
    if background is None:
        return fn(stats, size, fonts, theme)

    base = background.convert("RGB")
    if base.size != size:
        base = fit_image(base, size, "cover")
    layer = fn(stats, size, fonts, theme, transparent=True)
    return compose(base, layer, theme.scrim, shadow_for(layer), theme.auto_contrast)


# Mean luminance (0-255) a background is pushed below when auto-contrast is on.
# The stats layer is light-on-dark, so a bright background must be dimmed
# further than a dark one to keep the muted text readable.
TARGET_LUMA = 78


def mean_luma(img: Image.Image) -> float:
    """Average luminance, measured on a thumbnail because precision is not needed."""
    from PIL import ImageStat

    return ImageStat.Stat(img.convert("L").resize((32, 32), Image.BILINEAR)).mean[0]


def apply_scrim(img: Image.Image, amount: float, auto_contrast: bool = False) -> Image.Image:
    """Darken an image toward black by `amount` (0..1).

    With `auto_contrast`, `amount` becomes a floor: a background brighter than
    TARGET_LUMA is dimmed further until it reaches it, so a white wallpaper does
    not wash out the overlay.
    """
    amount = max(0.0, min(1.0, float(amount)))
    if amount >= 1:
        return Image.new("RGB", img.size, (0, 0, 0))

    factor = 1.0 - amount
    if auto_contrast:
        luma = mean_luma(img)
        if luma * factor > TARGET_LUMA:
            factor = TARGET_LUMA / max(luma, 1.0)

    if factor >= 1.0:
        return img
    from PIL import ImageEnhance

    return ImageEnhance.Brightness(img).enhance(factor)


_CW_TRANSPOSE = {90: Image.ROTATE_270, 180: Image.ROTATE_180, 270: Image.ROTATE_90}


def rotate_cw(img: Image.Image, degrees: int) -> Image.Image:
    """Rotate clockwise by 0/90/180/270 degrees, to correct for panel mounting."""
    op = _CW_TRANSPOSE.get(degrees % 360)
    return img.transpose(op) if op is not None else img


def logical_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Canvas size to render at before `rotate_cw(rotation)` reaches (width, height)."""
    return (height, width) if rotation % 360 in (90, 270) else (width, height)


def fit_image(img: Image.Image, size: tuple[int, int], mode: str = "cover") -> Image.Image:
    """Resize to the panel, either cropping to fill or letterboxing."""
    target_w, target_h = size
    img = img.convert("RGB")
    if img.size == size:
        return img

    if mode == "stretch":
        return img.resize(size, Image.LANCZOS)

    scale = max(target_w / img.width, target_h / img.height) if mode == "cover" \
        else min(target_w / img.width, target_h / img.height)
    new = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                     Image.LANCZOS)

    if mode == "cover":
        left = (new.width - target_w) // 2
        top = (new.height - target_h) // 2
        return new.crop((left, top, left + target_w, top + target_h))

    canvas = Image.new("RGB", size, (0, 0, 0))
    canvas.paste(new, ((target_w - new.width) // 2, (target_h - new.height) // 2))
    return canvas


def encode(img: Image.Image, max_kb: int, start_quality: int = 92) -> bytes:
    """JPEG-encode, stepping quality down until the frame fits the budget.

    Mirrors the vendor app's getSizeBt: quality walks down until the encoded
    size is under the per-model cap. Exceeding the cap wedges the display.
    """
    import io

    quality = start_quality
    while True:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, subsampling=2, optimize=False)
        data = buf.getvalue()
        if len(data) / 1024 <= max_kb or quality < 25:
            return data
        quality -= 6


def encode_rgb565(img: Image.Image) -> bytes:
    """Big-endian RGB565, for SPI-class panels."""
    from .protocol import to_rgb565

    rgb = img.convert("RGB")
    return to_rgb565(rgb.tobytes(), rgb.width * rgb.height)
