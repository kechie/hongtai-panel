"""Persistent configuration, shared by the CLI, the GUI, and the systemd service.

The config file is the single source of truth: the GUI edits it and restarts
the service, and the service reads it on start. Nothing is passed between them
except this file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hongtai-panel"
CONFIG_PATH = CONFIG_DIR / "config.json"

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "hongtai-panel"
PREVIEW_PATH = RUNTIME_DIR / "preview.jpg"

# "display" covers stats, media, and stats-over-media; the background playlist
# and the stats overlay are independent switches rather than separate modes.
MODES = ("display", "mirror")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".avif"}
VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".gif", ".m4v", ".apng"}


@dataclass
class Theme:
    """Colours and slot assignments for the dashboard renderers."""

    background: str = "#080a10"
    foreground: str = "#ecf0f8"
    dim: str = "#788498"
    track: str = "#222836"
    cool: str = "#40c8ff"
    warm: str = "#ffc440"
    hot: str = "#ff5454"

    # Darkening laid over the background before the stats are drawn, so text
    # stays legible on a busy photo. 0 = untouched, 1 = black.
    scrim: float = 0.45
    # Dim bright backgrounds further than `scrim` asks, so a white
    # wallpaper cannot wash out the light-on-dark stats layer.
    auto_contrast: bool = True

    # gauges layout: two arcs on top, two meters below
    arcs: list[str] = field(default_factory=lambda: ["cpu", "gpu"])
    bars: list[str] = field(default_factory=lambda: ["ram", "vram"])
    show_network: bool = True
    show_footer: bool = True

    # compact layout: a 2x2 grid
    cells: list[str] = field(default_factory=lambda: ["cpu", "gpu", "ram", "disk"])


def _migrate(raw: dict) -> dict:
    """Fold the old separate image and video modes into the unified media mode.

    Earlier versions had mode="image" with image_paths, and mode="video" with a
    single video_path. Both become one ordered playlist.
    """
    if "media_paths" not in raw:
        images = list(raw.get("image_paths") or [])
        video = raw.get("video_path") or ""
        # Whichever mode was active supplies the head of the playlist, so the
        # thing that was on screen stays on screen.
        raw["media_paths"] = ([video] + images) if raw.get("mode") == "video" and video \
            else images + ([video] if video else [])
    if raw.get("mode") in ("image", "video"):
        raw["mode"] = "media"
    if "loop" not in raw and "video_loop" in raw:
        raw["loop"] = raw["video_loop"]

    # Second fold: monitor and media become one display mode, distinguished by
    # whether the stats overlay is drawn.
    mode = raw.get("mode")
    if mode in ("monitor", "media"):
        if "overlay" not in raw:
            raw["overlay"] = mode == "monitor"
        # A monitor-mode config may carry a stale playlist from earlier use.
        # Those files were not being displayed, and promoting them to a
        # background would change what is on screen, so drop them.
        if mode == "monitor":
            raw["media_paths"] = []
        raw["mode"] = "display"
    return raw


def is_video(path) -> bool:
    from pathlib import Path

    return Path(path).suffix.lower() in VIDEO_SUFFIXES


@dataclass
class Config:
    mode: str = "display"
    # Draw the stats layer. With no background this is the classic dashboard;
    # over a background it becomes an overlay.
    overlay: bool = True
    layout: str = "gauges"  # gauges | compact
    fps: int = 15
    # How often telemetry is re-sampled, independent of the frame rate. Short
    # intervals make CPU and network readings jumpy (they are deltas over the
    # sampling window) and cost CPU; long ones feel sluggish.
    sample_interval: float = 1.0
    brightness: int = 80
    max_kb: int = 0
    port: str = ""

    # Background playlist of stills and clips, played in order. Images are held
    # for image_interval seconds; videos play through once, unless the playlist
    # is a single video and loop is set, in which case it repeats. Empty means
    # a plain background in the theme's background colour.
    media_paths: list[str] = field(default_factory=list)
    image_interval: float = 10.0
    loop: bool = True
    fit: str = "cover"

    preview: bool = True
    theme: Theme = field(default_factory=Theme)

    # Clockwise degrees to rotate the output before it is sent to the panel,
    # to correct for how the panel is physically mounted. Defaults to 180
    # because these panels ship designed for a cooler pump-head mount; a
    # chassis mount is typically flipped relative to that.
    rotation: int = 180

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        raw = _migrate(raw)
        theme_raw = raw.pop("theme", {}) or {}
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in raw.items() if k in known})
        theme_known = {f.name for f in fields(Theme)}
        cfg.theme = Theme(**{k: v for k, v in theme_raw.items() if k in theme_known})
        return cfg

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        tmp.replace(path)  # atomic, so a reader never sees a half-written file
