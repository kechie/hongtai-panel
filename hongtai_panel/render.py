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
