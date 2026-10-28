"""Frame sources: system monitor, video/GIF playback, desktop mirroring, and
the music visualizer.

Every source is an iterator of JPEG-encoded bytes already sized for the panel.
"""

from __future__ import annotations

import array
import logging
import queue
import re
import shutil
import subprocess
import threading
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
                  sample_interval: float = 1.0, rotation: int = 0) -> Iterator[bytes]:
    """Render the system-monitor dashboard at a fixed cadence.

    Frames are produced at `fps`, but telemetry only re-samples every
    `sample_interval` seconds — the two are independent knobs.
    """
    from .config import Theme

    theme = theme or Theme()
    fonts = render.Fonts()
    collector = Collector(sample_interval)
    size = render.logical_size(info.width, info.height, rotation)
    interval = 1.0 / max(1, fps)
    spi = info.is_spi

    while True:
        started = time.monotonic()
        img = render.render(collector.read(), size, fonts, theme, layout)
        if rotation:
            img = render.rotate_cw(img, rotation)
        yield render.encode_rgb565(img) if spi else render.encode(img, info.max_frame_kb)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def _encode_still(path: Path, info: PanelInfo, fit: str, rotation: int = 0) -> bytes:
    with Image.open(path) as im:
        fitted = render.fit_image(im, render.logical_size(info.width, info.height, rotation), fit)
    if rotation:
        fitted = render.rotate_cw(fitted, rotation)
    return (render.encode_rgb565(fitted) if info.is_spi
            else render.encode(fitted, info.max_frame_kb))


def image_frames(paths: list[Path], info: PanelInfo, interval: float, fit: str,
                 rotation: int = 0) -> Iterator[bytes]:
    """Cycle through still images, re-sending each to hold it on screen."""
    encoded = [_encode_still(p, info, fit, rotation) for p in paths]
    if not encoded:
        raise ValueError("no images to display")

    while True:
        for frame in encoded:
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline:
                yield frame
                time.sleep(1.0)


def media_frames(paths: list[Path], info: PanelInfo, fps: int, image_interval: float,
                 fit: str, loop: bool = True, quality: int = 6,
                 rotation: int = 0) -> Iterator[bytes]:
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
        yield from video_frames(paths[0], info, fps, fit, loop, quality, rotation)
        return

    # Stills are decoded once up front; clips stream on demand.
    stills = {p: _encode_still(p, info, fit, rotation) for p in paths if not is_video(p)}
    broken: set[Path] = set()

    while True:
        for path in paths:
            if path in broken:
                continue
            if is_video(path):
                got_frame = False
                for frame in video_frames(path, info, fps, fit, loop=False,
                                          quality=quality, rotation=rotation):
                    got_frame = True
                    yield frame
                if not got_frame:
                    _drop_broken_clip(path, paths, broken)
            else:
                deadline = time.monotonic() + image_interval
                while time.monotonic() < deadline:
                    yield stills[path]
                    time.sleep(1.0)
        if not loop:
            return


def _drop_broken_clip(path: Path, paths: list[Path], broken: set[Path]) -> None:
    """Stop retrying a clip ffmpeg could not decode.

    A clip that yields nothing returns immediately, so without this the
    playlist loop would restart ffmpeg as fast as the process can fork. If
    nothing playable is left, give up so the caller exits and the service
    restarts on its own schedule, rather than spinning.
    """
    broken.add(path)
    log.warning("skipping %s: ffmpeg produced no frames (unreadable or unsupported)", path)
    if all(p in broken for p in paths):
        raise RuntimeError("none of the playlist's clips could be decoded")


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


# ffmpeg's transpose is defined counter-clockwise-ish; these give clockwise
# rotation to match rotate_cw's convention. 180 has no dedicated transpose, so
# it is composed from two flips instead.
_FFMPEG_ROTATE = {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}


def _ffmpeg_scale_filter(w: int, h: int, fit: str) -> str:
    if fit == "contain":
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:-1:-1:color=black"
    if fit == "stretch":
        return f"scale={w}:{h}"
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"


def video_frames(
    path: Path, info: PanelInfo, fps: int, fit: str, loop: bool, quality: int = 6,
    rotation: int = 0,
) -> Iterator[bytes]:
    """Decode video/GIF through ffmpeg straight into panel-sized MJPEG.

    ffmpeg does the scaling, rotation, and JPEG encoding, so no per-frame
    Pillow work.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found")

    lw, lh = render.logical_size(info.width, info.height, rotation)
    vf = _ffmpeg_scale_filter(lw, lh, fit)
    rot = _FFMPEG_ROTATE.get(rotation % 360)
    if rot:
        vf += f",{rot}"
    vf += f",fps={fps}"

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
                      loop: bool, rotation: int = 0) -> Iterator[Image.Image]:
    """Decode a clip to logically-oriented, panel-sized RGB frames.

    Raw RGB rather than MJPEG: the frames are about to be composited and
    re-encoded anyway, so decoding a JPEG here would be wasted work. Not
    rotated here — the caller composites onto these first and rotates the
    finished frame once, rather than rotating every background frame.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found")

    w, h = render.logical_size(info.width, info.height, rotation)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if loop:
        cmd += ["-stream_loop", "-1"]
    cmd += ["-re", "-i", str(path), "-an",
            "-vf", _ffmpeg_scale_filter(w, h, fit) + f",fps={fps}",
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
                      fit: str, loop: bool, rotation: int = 0) -> Iterator[Image.Image]:
    """Yield logically-oriented background frames from the playlist, paced for `fps`."""
    from .config import is_video

    size = render.logical_size(info.width, info.height, rotation)
    stills: dict[Path, Image.Image] = {}
    for p in paths:
        if not is_video(p):
            with Image.open(p) as im:
                stills[p] = render.fit_image(im, size, fit)

    interval = 1.0 / max(1, fps)
    broken: set[Path] = set()
    while True:
        for path in paths:
            if path in broken:
                continue
            if is_video(path):
                single = len(paths) == 1
                got_frame = False
                for frame in _video_raw_frames(path, info, fps, fit, loop and single, rotation):
                    got_frame = True
                    yield frame
                if not got_frame:
                    _drop_broken_clip(path, paths, broken)
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
                   loop: bool = True, quality: int = 6, rotation: int = 0) -> Iterator[bytes]:
    """The unified display source: stats, media, or stats over media."""
    from .config import Theme

    theme = theme or Theme()

    if not paths:
        yield from sysmon_frames(info, layout, fps, theme, sample_interval, rotation)
        return
    if not overlay:
        yield from media_frames(paths, info, fps, image_interval, fit, loop, quality, rotation)
        return

    fonts = render.Fonts()
    collector = Collector(sample_interval)
    size = render.logical_size(info.width, info.height, rotation)

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

    for bg in background_images(paths, info, fps, image_interval, fit, loop, rotation):
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
        if rotation:
            frame = render.rotate_cw(frame, rotation)
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


# gst videoflip's clockwise/counterclockwise match rotate_cw's convention;
# 180 has its own dedicated method.
_GST_FLIP = {90: "clockwise", 180: "rotate-180", 270: "counterclockwise"}


def mirror_frames(info: PanelInfo, fps: int, fit: str, rotation: int = 0) -> Iterator[bytes]:
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

    w, h = render.logical_size(info.width, info.height, rotation)
    if fit == "contain":
        scale_caps = f"video/x-raw,width={w},height={h},pixel-aspect-ratio=1/1"
        scaler = f"videoscale add-borders=true ! {scale_caps}"
    else:
        scaler = f"videoscale add-borders=false ! video/x-raw,width={w},height={h}"

    flip_method = _GST_FLIP.get(rotation % 360)
    flip_stage = f"videoflip method={flip_method} ! " if flip_method else ""

    pipeline_desc = (
        f"pipewiresrc path={node} always-copy=true ! "
        f"videorate ! video/x-raw,framerate={fps}/1 ! "
        f"videoconvert ! {scaler} ! {flip_stage}"
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


# -- music visualizer ---------------------------------------------------

# "@DEFAULT_MONITOR@" is PulseAudio's magic device name for the monitor of
# the default sink, i.e. whatever is currently playing — resolved the same
# way under PipeWire's pulse-compatible server. There is deliberately no
# device flag: like mirror_frames' compositor picker, this follows whatever
# the desktop is doing. The name is only resolved when the capture stream
# starts, so _AudioTap restarts the pipeline when the default output changes.
_AUDIO_SOURCE = "pulsesrc device=@DEFAULT_MONITOR@"
_SPECTRUM_MAGNITUDE_RE = re.compile(r"magnitude=\(float\)\{([^}]*)\}")


def _default_sink() -> str | None:
    """Name of the current default output, or None if it cannot be read.

    Uses `pactl`, which PipeWire's pulse server ships too. Without it the tap
    simply cannot notice an output change, and behaves as it did before.
    """
    if not shutil.which("pactl"):
        return None
    try:
        out = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


class _AudioTap:
    """Runs a GStreamer capture pipeline on a background thread, and keeps it alive.

    Subclasses supply the pipeline (`_pipeline`) and how to consume it
    (`_pump`). This base restarts the pipeline when the audio server reports
    an error or end-of-stream, and when the default output device changes, so
    the visualizer does not freeze on stale data after a headphone swap or a
    PipeWire restart. Start-up failures (no server, missing plugins) are logged
    once and retried quietly with a growing delay.

    Published data is reassigned wholesale rather than mutated, so the render
    loop can read it without a lock (the same trick _NvidiaPoller in
    sysinfo.py uses) and always sees a complete, consistent snapshot.
    """

    label = "audio"
    SINK_POLL_SECONDS = 2.0
    RETRY_MAX_SECONDS = 15.0

    def __init__(self):
        self._sink = None
        self._sink_checked = 0.0
        threading.Thread(target=self._supervise, daemon=True,
                         name=f"music-{self.label}").start()

    # -- subclass hooks -------------------------------------------------

    def _pipeline(self) -> str:
        raise NotImplementedError

    def _pump(self, Gst, pipeline) -> None:
        """Consume the running pipeline. Return when it needs restarting."""
        raise NotImplementedError

    def _reset(self) -> None:
        """Blank the published data, so a dead capture does not look live."""

    # -- machinery ------------------------------------------------------

    def _output_changed(self) -> bool:
        """True when the default output differs from the one we started on.

        Rate-limited: it shells out, and callers check once per loop wakeup.
        """
        now = time.monotonic()
        if now - self._sink_checked < self.SINK_POLL_SECONDS:
            return False
        self._sink_checked = now
        current = _default_sink()
        if current is None or self._sink is None:
            self._sink = self._sink or current
            return False
        if current != self._sink:
            log.info("default audio output changed to %s; restarting %s capture",
                     current, self.label)
            return True
        return False

    def _log_failure(self, Gst, msg) -> None:
        if msg.type == Gst.MessageType.ERROR:
            err, _debug = msg.parse_error()
            log.warning("%s capture error: %s", self.label, err.message)
        else:
            log.warning("%s capture ended", self.label)

    def _failed(self, Gst, bus) -> bool:
        """True if the bus holds an error or end-of-stream message."""
        msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is None:
            return False
        self._log_failure(Gst, msg)
        return True

    def _supervise(self) -> None:
        delay = 1.0
        reported = False
        while True:
            pipeline = None
            try:
                import gi

                gi.require_version("Gst", "1.0")
                from gi.repository import Gst

                Gst.init(None)
                pipeline = Gst.parse_launch(self._pipeline())
                self._sink = _default_sink()
                self._sink_checked = time.monotonic()
                pipeline.set_state(Gst.State.PLAYING)
                started = time.monotonic()
                self._pump(Gst, pipeline)
                # A pipeline that ran for a while was healthy; start the
                # backoff over rather than punishing the next restart.
                if time.monotonic() - started > 10:
                    delay = 1.0
                    reported = False
            except Exception:
                if not reported:
                    log.exception("could not start audio capture for the %s visualizer",
                                  self.label)
                    reported = True
            finally:
                if pipeline is not None:
                    pipeline.set_state(Gst.State.NULL)
            self._reset()
            time.sleep(delay)
            delay = min(self.RETRY_MAX_SECONDS, delay * 2)


class _SpectrumTap(_AudioTap):
    """Runs an FFT over desktop audio; `levels` holds the latest bands (0..100)."""

    label = "spectrum"

    def __init__(self, bands: int = 32, floor_db: int = -70):
        self.bands = bands
        self.floor_db = floor_db
        self.levels = [0.0] * bands
        super().__init__()

    def _pipeline(self) -> str:
        return (
            f"{_AUDIO_SOURCE} ! audioconvert ! audioresample ! "
            "audio/x-raw,rate=44100,channels=2 ! "
            f"spectrum bands={self.bands} threshold={self.floor_db} "
            "post-messages=true interval=33000000 ! fakesink sync=false"
        )

    def _reset(self) -> None:
        self.levels = [0.0] * self.bands

    def _pump(self, Gst, pipeline) -> None:
        bus = pipeline.get_bus()
        kinds = Gst.MessageType.ELEMENT | Gst.MessageType.ERROR | Gst.MessageType.EOS
        floor = self.floor_db
        while True:
            # Wake up periodically even when no audio is flowing, so an
            # output change is noticed during silence too.
            msg = bus.timed_pop_filtered(Gst.SECOND, kinds)
            if msg is None:
                if self._output_changed():
                    return
                continue
            if msg.type != Gst.MessageType.ELEMENT:
                self._log_failure(Gst, msg)
                return
            s = msg.get_structure()
            if s is None or s.get_name() != "spectrum":
                continue
            m = _SPECTRUM_MAGNITUDE_RE.search(s.to_string())
            if m:
                self.levels = [
                    max(0.0, min(100.0, (float(v) - floor) / -floor * 100))
                    for v in m.group(1).split(",")
                ]
            if self._output_changed():
                return


class _WaveformTap(_AudioTap):
    """Keeps a rolling window of raw waveform samples from desktop audio."""

    label = "waveform"

    def __init__(self, window: int = 480, rate: int = 22050):
        self.window = window
        self.rate = rate
        self.samples = [0.0] * window
        super().__init__()

    def _pipeline(self) -> str:
        # drop=false: dropping a buffer here would splice two non-contiguous
        # chunks of audio together, which reads as a burst of noise rather
        # than a smooth trace. Backpressure (a blocked upstream) is the better
        # failure mode for a waveform.
        return (
            f"{_AUDIO_SOURCE} ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=F32LE,rate={self.rate},channels=1 ! "
            "appsink name=sink emit-signals=false max-buffers=8 drop=false sync=false"
        )

    def _reset(self) -> None:
        self.samples = [0.0] * self.window

    def _pump(self, Gst, pipeline) -> None:
        sink = pipeline.get_by_name("sink")
        bus = pipeline.get_bus()
        while True:
            # A timeout, not an indefinite wait: a pipeline that died on an
            # error never hands over another sample, so this is how we notice.
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                if self._failed(Gst, bus) or sink.is_eos() or self._output_changed():
                    return
                continue
            buf = sample.get_buffer()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                data = array.array("f")
                data.frombytes(bytes(mapinfo.data))
            finally:
                buf.unmap(mapinfo)
            if data:
                self.samples = (self.samples + list(data))[-self.window:]
            if self._output_changed():
                return


def music_frames(info: PanelInfo, style: str, fps: int = 30, theme=None,
                 rotation: int = 0) -> Iterator[bytes]:
    """Render a live desktop-audio visualizer: spectrum bars (and variants) or an oscilloscope.

    Audio capture and analysis run on a background thread (GStreamer's
    `spectrum` element does the FFT for bars; scope just reads raw samples),
    decoupled from the render loop the same way Collector decouples telemetry
    sampling from frame rate. If capture fails to start — no PulseAudio/
    PipeWire server, missing GStreamer plugins — the visualizer stays blank
    rather than taking the panel down.
    """
    from .config import Theme

    theme = theme or Theme()
    size = render.logical_size(info.width, info.height, rotation)
    spi = info.is_spi
    interval = 1.0 / max(1, fps)

    factory = render.VISUALIZERS.get(style, render.VISUALIZERS["bars"])
    viz = factory()
    if style in render.WAVEFORM_VISUALIZERS:
        tap = _WaveformTap(window=getattr(viz, "window", 480))
        draw = lambda: viz.draw(tap.samples, size, theme)  # noqa: E731
    else:
        tap = _SpectrumTap()
        draw = lambda: viz.draw(tap.levels, size, theme)  # noqa: E731

    while True:
        started = time.monotonic()
        img = draw()
        if rotation:
            img = render.rotate_cw(img, rotation)
        yield render.encode_rgb565(img) if spi else render.encode(img, info.max_frame_kb)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))
