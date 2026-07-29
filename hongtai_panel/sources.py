"""Frame sources: system monitor, video/GIF playback, and desktop mirroring.

Every source is an iterator of JPEG-encoded bytes already sized for the panel.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from PIL import Image

from . import render
from .protocol import PanelInfo
from .sysinfo import Collector

log = logging.getLogger(__name__)

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def sysmon_frames(info: PanelInfo, layout: str, fps: int, theme=None,
                  sample_interval: float = 1.0) -> Iterator[bytes]:
    """Render the system-monitor dashboard at a fixed cadence.

    Frames are produced at `fps`, but telemetry only re-samples every
    `sample_interval` seconds — the two are independent knobs.
    """
    from .config import Theme

    theme = theme or Theme()
    fonts = render.Fonts()
    collector = Collector(sample_interval)
    size = (info.width, info.height)
    interval = 1.0 / max(1, fps)
    spi = info.is_spi

    while True:
        started = time.monotonic()
        img = render.render(collector.read(), size, fonts, theme, layout)
        yield render.encode_rgb565(img) if spi else render.encode(img, info.max_frame_kb)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def _encode_still(path: Path, info: PanelInfo, fit: str) -> bytes:
    with Image.open(path) as im:
        fitted = render.fit_image(im, (info.width, info.height), fit)
    return (render.encode_rgb565(fitted) if info.is_spi
            else render.encode(fitted, info.max_frame_kb))


def image_frames(paths: list[Path], info: PanelInfo, interval: float, fit: str) -> Iterator[bytes]:
    """Cycle through still images, re-sending each to hold it on screen."""
    encoded = [_encode_still(p, info, fit) for p in paths]
    if not encoded:
        raise ValueError("no images to display")

    while True:
        for frame in encoded:
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                yield frame
                time.sleep(1.0)


def media_frames(paths: list[Path], info: PanelInfo, fps: int, image_interval: float,
                 fit: str, loop: bool = True, quality: int = 6) -> Iterator[bytes]:
    """Play an ordered playlist of stills and clips.

    Stills are held for `image_interval`; clips play through once so the
    playlist can advance. The one exception is a playlist of a single video with
    `loop` set, which repeats inside ffmpeg rather than restarting the process
    every few seconds.
    """
    if not paths:
        raise ValueError("no media to display")

    from .config import is_video

    videos = [p for p in paths if is_video(p)]
    single_video = len(paths) == 1 and len(videos) == 1

    if single_video:
        yield from video_frames(paths[0], info, fps, fit, loop, quality)
        return

    # Stills are decoded once up front; clips stream on demand.
    stills = {p: _encode_still(p, info, fit) for p in paths if not is_video(p)}

    while True:
        for path in paths:
            if is_video(path):
                yield from video_frames(path, info, fps, fit, loop=False, quality=quality)
            else:
                deadline = time.monotonic() + image_interval
                while time.monotonic() < deadline:
                    yield stills[path]
                    time.sleep(1.0)
        if not loop:
            return


def _split_mjpeg(stream, chunk_size: int = 65536) -> Iterator[bytes]:
    """Yield complete JPEGs from a concatenated MJPEG byte stream."""
    buf = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            if buf.startswith(SOI) and buf.endswith(EOI):
                yield buf
            return
        buf += chunk
        while True:
            start = buf.find(SOI)
            if start < 0:
                buf = b""
                break
            end = buf.find(EOI, start + 2)
            if end < 0:
                buf = buf[start:]
                break
            yield buf[start : end + 2]
            buf = buf[end + 2 :]


def _ffmpeg_scale_filter(info: PanelInfo, fit: str) -> str:
    w, h = info.width, info.height
    if fit == "contain":
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:-1:-1:color=black"
    if fit == "stretch":
        return f"scale={w}:{h}"
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def video_frames(
    path: Path, info: PanelInfo, fps: int, fit: str, loop: bool, quality: int = 6
) -> Iterator[bytes]:
    """Decode video/GIF through ffmpeg straight into panel-sized MJPEG.

    ffmpeg does the scaling and JPEG encoding, so no per-frame Pillow work.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found")

    vf = _ffmpeg_scale_filter(info, fit) + f",fps={fps}"

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += [
        "-re", "-i", str(path),
        "-an", "-vf", vf,
        "-q:v", str(quality),
        "-f", "mjpeg", "-",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=1024 * 1024)
    try:
        for frame in _split_mjpeg(proc.stdout):
            if info.is_spi:
                import io

                with Image.open(io.BytesIO(frame)) as im:
                    yield render.encode_rgb565(im.convert("RGB"))
            else:
                yield frame
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _video_raw_frames(path: Path, info: PanelInfo, fps: int, fit: str,
                      loop: bool) -> Iterator[Image.Image]:
    """Decode a clip to panel-sized RGB frames.

    Raw RGB rather than MJPEG: the frames are about to be composited and
    re-encoded anyway, so decoding a JPEG here would be wasted work.
    """
    w, h = info.width, info.height
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-re", "-i", str(path), "-an",
            "-vf", _ffmpeg_scale_filter(info, fit) + f",fps={fps}",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]

    frame_bytes = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=frame_bytes * 4)
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                return
            yield Image.frombytes("RGB", (w, h), buf)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def background_images(paths: list[Path], info: PanelInfo, fps: int, image_interval: float,
                      fit: str, loop: bool) -> Iterator[Image.Image]:
    """Yield background frames from the playlist, paced for `fps`."""
    from .config import is_video

    size = (info.width, info.height)
    stills: dict[Path, Image.Image] = {}
    for p in paths:
        if not is_video(p):
            with Image.open(p) as im:
                stills[p] = render.fit_image(im, size, fit)

    interval = 1.0 / max(1, fps)
    while True:
        for path in paths:
            if is_video(path):
                single = len(paths) == 1
                yield from _video_raw_frames(path, info, fps, fit, loop and single)
            else:
                still = stills[path]
                deadline = time.monotonic() + image_interval
                while time.monotonic() < deadline:
                    started = time.monotonic()
                    yield still
                    time.sleep(max(0.0, interval - (time.monotonic() - started)))
        if not loop:
            return


def display_frames(info: PanelInfo, paths: list[Path], overlay: bool, layout: str,
                   fps: int, theme=None, sample_interval: float = 1.0,
                   image_interval: float = 10.0, fit: str = "cover",
                   loop: bool = True, quality: int = 6) -> Iterator[bytes]:
    """The unified display source: stats, media, or stats over media."""
    from .config import Theme

    theme = theme or Theme()

    if not paths:
        yield from sysmon_frames(info, layout, fps, theme, sample_interval)
        return
    if not overlay:
        yield from media_frames(paths, info, fps, image_interval, fit, loop, quality)
        return

    fonts = render.Fonts()
    collector = Collector(sample_interval)
    size = (info.width, info.height)

    # The stats layer only changes when the collector re-samples, so it is
    # rendered once per sample and composited onto many background frames.
    # The layer and its shadow only change when the collector re-samples, so
    # both are built once per sample and reused across many background frames.
    layer: Image.Image | None = None
    shadow: Image.Image | None = None
    layer_for: object = None
    # Stills yield the same object every frame, so the dimmed copy is reusable.
    scrimmed: Image.Image | None = None
    scrimmed_for: object = None

    for bg in background_images(paths, info, fps, image_interval, fit, loop):
        stats = collector.read()
        if stats is not layer_for:
            layer = render.LAYOUTS.get(layout, render.render_gauges)(
                stats, size, fonts, theme, transparent=True)
            shadow = render.shadow_for(layer)
            layer_for = stats

        if bg is not scrimmed_for:
            scrimmed = render.apply_scrim(bg, theme.scrim, theme.auto_contrast)
            scrimmed_for = bg

        frame = scrimmed.copy()
        frame.paste(shadow, (0, 0), shadow)
        frame.paste(layer, (0, 0), layer)
        yield render.encode_rgb565(frame) if info.is_spi \
            else render.encode(frame, info.max_frame_kb)


# -- desktop mirroring -------------------------------------------------------


def _portal_screencast_node() -> int:
    """Negotiate an xdg-desktop-portal ScreenCast session and return its PipeWire node id.

    Shows the compositor's own picker dialog; on KDE that is the standard
    "Screen Sharing" prompt. Returns the node id to hand to pipewiresrc.
    """
    from gi.repository import Gio, GLib

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    unique = bus.get_unique_name()[1:].replace(".", "_")
    token = f"hongtai{int(time.time())}"
    loop = GLib.MainLoop()
    result: dict = {}

    def call(method: str, args: GLib.Variant) -> str:
        return bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            method, args, None, Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]

    def on_response(handle: str, callback) -> None:
        def handler(_conn, _sender, _path, _iface, _signal, params):
            callback(*params.unpack())

        bus.signal_subscribe(
            "org.freedesktop.portal.Desktop", "org.freedesktop.portal.Request",
            "Response", handle, None, Gio.DBusSignalFlags.NO_MATCH_RULE, handler,
        )

    def session_started(code: int, results: dict) -> None:
        if code != 0:
            result["error"] = "screen-share request was cancelled"
        else:
            streams = results.get("streams") or []
            if streams:
                result["node"] = streams[0][0]
            else:
                result["error"] = "portal returned no streams"
        loop.quit()

    def sources_selected(code: int, _results: dict) -> None:
        if code != 0:
            result["error"] = "source selection failed"
            loop.quit()
            return
        h = call("Start", GLib.Variant("(osa{sv})", (
            result["session"], "",
            {"handle_token": GLib.Variant("s", token + "start")},
        )))
        on_response(h, session_started)

    def session_created(code: int, results: dict) -> None:
        if code != 0:
            result["error"] = "could not create portal session"
            loop.quit()
            return
        result["session"] = results["session_handle"]
        h = call("SelectSources", GLib.Variant("(oa{sv})", (
            result["session"],
            {
                "types": GLib.Variant("u", 1 | 2),   # monitors + windows
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", 2),  # embed the cursor
                "handle_token": GLib.Variant("s", token + "sel"),
            },
        )))
        on_response(h, sources_selected)

    handle = call("CreateSession", GLib.Variant("(a{sv})", ({
        "handle_token": GLib.Variant("s", token),
        "session_handle_token": GLib.Variant("s", token + "sess"),
    },)))
    on_response(handle, session_created)

    GLib.timeout_add_seconds(120, lambda: (result.setdefault("error", "timed out"), loop.quit())[1])
    loop.run()

    if "error" in result:
        raise RuntimeError(result["error"])
    return int(result["node"])


def mirror_frames(info: PanelInfo, fps: int, fit: str) -> Iterator[bytes]:
    """Mirror the desktop via PipeWire, encoding to panel-sized JPEG in GStreamer.

    Which monitor or window gets mirrored is chosen in the compositor's own
    picker dialog when the session starts, so there is no region flag here.
    """
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    node = _portal_screencast_node()
    log.info("mirroring PipeWire node %s", node)

    w, h = info.width, info.height
    if fit == "contain":
        scale_caps = f"video/x-raw,width={w},height={h},pixel-aspect-ratio=1/1"
        scaler = f"videoscale add-borders=true ! {scale_caps}"
    else:
        scaler = f"videoscale add-borders=false ! video/x-raw,width={w},height={h}"

    pipeline_desc = (
        f"pipewiresrc path={node} always-copy=true ! "
        f"videorate ! video/x-raw,framerate={fps}/1 ! "
        f"videoconvert ! {scaler} ! "
        f"jpegenc quality=80 ! "
        f"appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
    )
    pipeline = Gst.parse_launch(pipeline_desc)
    sink = pipeline.get_by_name("sink")
    frames: queue.Queue[bytes] = queue.Queue(maxsize=3)

    def on_sample(appsink) -> int:
        sample = appsink.emit("pull-sample")
        if sample:
            ok, mapinfo = sample.get_buffer().map(Gst.MapFlags.READ)
            if ok:
                try:
                    frames.put_nowait(bytes(mapinfo.data))
                except queue.Full:
                    pass
                finally:
                    sample.get_buffer().unmap(mapinfo)
        return Gst.FlowReturn.OK

    sink.connect("new-sample", on_sample)
    pipeline.set_state(Gst.State.PLAYING)

    try:
        while True:
            try:
                frame = frames.get(timeout=5.0)
            except queue.Empty:
                log.warning("no frames from compositor for 5s")
                continue
            if info.is_spi:
                import io

                with Image.open(io.BytesIO(frame)) as im:
                    yield render.encode_rgb565(im.convert("RGB"))
            else:
                yield frame
    finally:
        pipeline.set_state(Gst.State.NULL)
