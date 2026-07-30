"""GTK4 control panel.

The GUI never touches the serial port. It edits the config file and restarts
the systemd user service, which keeps sole ownership of the device — so there
is exactly one writer, and the panel keeps running after the window closes.

The preview renders locally when the stats overlay is on and any background is
a still, giving instant feedback while editing a theme. For video backgrounds
and mirroring it shows the frame the service last published to tmpfs, which is
what is genuinely on the panel.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from .cli import UDEV_PATH, cmd_install_udev  # noqa: E402
from .config import (  # noqa: E402
    IMAGE_SUFFIXES, MODES, PREVIEW_PATH, VIDEO_SUFFIXES, Config, Theme, is_video,
)
from .render import Fonts, LAYOUTS, fit_image, hex_to_rgb, render  # noqa: E402
from .sysinfo import METRICS, Collector  # noqa: E402

SERVICE = "hongtai-panel.service"
METRIC_KEYS = list(METRICS)
METRIC_LABELS = [METRICS[k][0] for k in METRIC_KEYS]
FITS = ["cover", "contain", "stretch"]

COLOR_FIELDS = [
    ("background", "Background"),
    ("foreground", "Text"),
    ("dim", "Muted text"),
    ("track", "Gauge track"),
    ("cool", "Low load"),
    ("warm", "Medium load"),
    ("hot", "High load"),
]


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=15
    )


def service_active() -> bool:
    return systemctl("is-active", SERVICE).stdout.strip() == "active"


def rgba_to_hex(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)
    )


def hex_to_rgba(value: str) -> Gdk.RGBA:
    r, g, b = hex_to_rgb(value)
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = r / 255, g / 255, b / 255, 1.0
    return rgba


def dropdown(items: list[str], selected: int = 0) -> Gtk.DropDown:
    d = Gtk.DropDown.new_from_strings(items)
    d.set_selected(max(0, selected))
    return d


def row(label: str, widget: Gtk.Widget, tooltip: str = "") -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    lbl = Gtk.Label(label=label, xalign=0)
    lbl.set_size_request(130, -1)
    box.append(lbl)
    widget.set_hexpand(True)
    box.append(widget)
    if tooltip:
        box.set_tooltip_text(tooltip)
    return box


def section(title: str) -> Gtk.Label:
    lbl = Gtk.Label(label=title, xalign=0)
    lbl.add_css_class("heading")
    lbl.set_margin_top(10)
    return lbl


class Window(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="LCD Panel")
        self.set_default_size(940, 620)

        self.config = Config.load()
        self.fonts = Fonts()
        self.collector = Collector(self.config.sample_interval)
        self._preview_mtime = 0.0
        self._dirty = False
        self._syncing = False  # True while the poll writes widget state
        self._bg_cache = None
        self._bg_cache_for = None

        self._build_header()

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        outer.set_margin_top(18)
        outer.set_margin_bottom(18)
        outer.set_margin_start(18)
        outer.set_margin_end(18)
        self.set_child(outer)

        outer.append(self._build_preview())

        notebook = Gtk.Notebook()
        notebook.set_hexpand(True)
        notebook.append_page(self._build_display_tab(), Gtk.Label(label="Display"))
        notebook.append_page(self._build_theme_tab(), Gtk.Label(label="Theme"))
        outer.append(notebook)

        GLib.timeout_add(500, self._tick)
        self._refresh_mode_sensitivity()

    # -- chrome ---------------------------------------------------------

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        self.switch = Gtk.Switch()
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.set_tooltip_text("Start or stop the panel service")
        self.switch.set_active(service_active())
        self.switch.connect("state-set", self._on_switch)
        header.pack_start(Gtk.Label(label="Panel"))
        header.pack_start(self.switch)

        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.set_tooltip_text("Save settings and restart the panel service")
        self.apply_btn.connect("clicked", self._on_apply)
        header.pack_end(self.apply_btn)

        self.status = Gtk.Label(label="")
        self.status.add_css_class("dim-label")
        header.pack_end(self.status)

    def _build_preview(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        frame = Gtk.Frame()
        frame.set_size_request(420, 420)
        frame.set_valign(Gtk.Align.START)
        self.picture = Gtk.Picture()
        self.picture.set_size_request(420, 420)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        frame.set_child(self.picture)
        box.append(frame)

        self.preview_note = Gtk.Label(label="")
        self.preview_note.add_css_class("dim-label")
        self.preview_note.set_wrap(True)
        box.append(self.preview_note)
        return box

    # -- display tab ----------------------------------------------------

    def _build_display_tab(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(14)
        page.set_margin_bottom(14)
        page.set_margin_start(14)
        page.set_margin_end(14)

        cfg = self.config
        self.mode_dd = dropdown(
            ["Panel display", "Mirror a screen"],
            MODES.index(cfg.mode) if cfg.mode in MODES else 0,
        )
        self.mode_dd.connect("notify::selected", lambda *_: self._refresh_mode_sensitivity())
        page.append(row("Mode", self.mode_dd))

        self.overlay = Gtk.CheckButton(label="Show system stats")
        self.overlay.set_active(cfg.overlay)
        self.overlay.set_tooltip_text(
            "Draw CPU/GPU/RAM over the background. Turn off for background only."
        )
        self.overlay.connect("toggled", lambda *_: self._refresh_mode_sensitivity())
        page.append(self.overlay)

        self.layout_dd = dropdown(
            [k.title() for k in LAYOUTS],
            list(LAYOUTS).index(cfg.layout) if cfg.layout in LAYOUTS else 0,
        )
        self.layout_dd.connect("notify::selected", lambda *_: self._touch())
        layout_row = row("Layout", self.layout_dd)
        page.append(layout_row)

        self.fps = Gtk.SpinButton.new_with_range(1, 60, 1)
        self.fps.set_value(cfg.fps)
        page.append(row("Frame rate", self.fps, "Higher costs more CPU; 15 is plenty for stats"))

        self.sample = Gtk.SpinButton.new_with_range(0.1, 10.0, 0.1)
        self.sample.set_digits(1)
        self.sample.set_value(cfg.sample_interval)
        self.sample.connect("value-changed", lambda *_: self._on_sample_changed())
        sample_row = row(
            "Sensor refresh", self.sample,
            "Seconds between readings, independent of frame rate. Below about "
            "0.5s, CPU and network figures get jumpy because they are measured "
            "over the interval.",
        )
        page.append(sample_row)
        self.sample_note = Gtk.Label(xalign=0, wrap=True)
        self.sample_note.add_css_class("dim-label")
        page.append(self.sample_note)
        self._monitor_widgets = [layout_row, sample_row, self.sample_note]
        self._on_sample_changed()

        self.brightness = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.brightness.set_value(cfg.brightness)
        self.brightness.set_draw_value(True)
        page.append(row("Brightness", self.brightness))

        self.fit_dd = dropdown([f.title() for f in FITS],
                               FITS.index(cfg.fit) if cfg.fit in FITS else 0)
        fit_row = row("Scaling", self.fit_dd, "How images and video fill a square panel")
        page.append(fit_row)
        self._scaling_widgets = [fit_row]

        page.append(section("Background"))

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        pick = Gtk.Button(label="Choose files…")
        pick.connect("clicked", self._on_pick_media)
        buttons.append(pick)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", self._on_clear_media)
        buttons.append(clear)
        playlist_row = row("Playlist", buttons,
                           "Images and videos play in order behind the stats. Leave empty for a plain background.")
        page.append(playlist_row)

        self.media_label = Gtk.Label(label=self._summarise_media(), xalign=0, wrap=True)
        page.append(self.media_label)

        self.interval = Gtk.SpinButton.new_with_range(1, 3600, 1)
        self.interval.set_value(cfg.image_interval)
        interval_row = row("Seconds per image", self.interval,
                           "How long each still is held. Videos play to their end.")
        page.append(interval_row)

        self.loop = Gtk.CheckButton(label="Repeat playlist")
        self.loop.set_active(cfg.loop)
        page.append(self.loop)
        self._media_widgets = [playlist_row, self.media_label, interval_row, self.loop]

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(page)
        return scroll

    # -- theme tab ------------------------------------------------------

    def _build_theme_tab(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_margin_top(14)
        page.set_margin_bottom(14)
        page.set_margin_start(14)
        page.set_margin_end(14)

        theme = self.config.theme
        page.append(section("Over a background"))

        self.scrim = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scrim.set_value(theme.scrim * 100)
        self.scrim.set_draw_value(True)
        self.scrim.connect("value-changed", lambda *_: self._touch())
        page.append(row("Dim background", self.scrim,
                        "How much to darken the background so the stats stay readable."))

        self.auto_contrast = Gtk.CheckButton(label="Dim bright backgrounds automatically")
        self.auto_contrast.set_active(theme.auto_contrast)
        self.auto_contrast.set_tooltip_text(
            "Treat the slider as a minimum and darken pale wallpapers further, "
            "so light images cannot wash out the text."
        )
        self.auto_contrast.connect("toggled", lambda *_: self._touch())
        page.append(self.auto_contrast)

        page.append(section("Colours"))
        self.color_buttons: dict[str, Gtk.ColorDialogButton] = {}
        for key, label in COLOR_FIELDS:
            btn = Gtk.ColorDialogButton.new(Gtk.ColorDialog())
            btn.set_rgba(hex_to_rgba(getattr(theme, key)))
            btn.connect("notify::rgba", lambda *_: self._touch())
            btn.set_halign(Gtk.Align.START)
            self.color_buttons[key] = btn
            page.append(row(label, btn))

        page.append(section("Gauges layout"))
        self.arc_dds: list[Gtk.DropDown] = []
        for i in range(3):
            current = theme.arcs[i] if i < len(theme.arcs) else None
            items = ["(none)"] + METRIC_LABELS
            idx = (METRIC_KEYS.index(current) + 1) if current in METRIC_KEYS else 0
            dd = dropdown(items, idx)
            dd.connect("notify::selected", lambda *_: self._touch())
            self.arc_dds.append(dd)
            page.append(row(f"Arc {i + 1}", dd))

        self.bar_dds: list[Gtk.DropDown] = []
        for i in range(3):
            current = theme.bars[i] if i < len(theme.bars) else None
            items = ["(none)"] + METRIC_LABELS
            idx = (METRIC_KEYS.index(current) + 1) if current in METRIC_KEYS else 0
            dd = dropdown(items, idx)
            dd.connect("notify::selected", lambda *_: self._touch())
            self.bar_dds.append(dd)
            page.append(row(f"Meter {i + 1}", dd))

        self.show_net = Gtk.CheckButton(label="Show network rates")
        self.show_net.set_active(theme.show_network)
        self.show_net.connect("toggled", lambda *_: self._touch())
        page.append(self.show_net)

        self.show_footer = Gtk.CheckButton(label="Show clock speed and uptime")
        self.show_footer.set_active(theme.show_footer)
        self.show_footer.connect("toggled", lambda *_: self._touch())
        page.append(self.show_footer)

        page.append(section("Compact layout"))
        self.cell_dds: list[Gtk.DropDown] = []
        for i in range(4):
            current = theme.cells[i] if i < len(theme.cells) else "cpu"
            idx = METRIC_KEYS.index(current) if current in METRIC_KEYS else 0
            dd = dropdown(METRIC_LABELS, idx)
            dd.connect("notify::selected", lambda *_: self._touch())
            self.cell_dds.append(dd)
            page.append(row(f"Cell {i + 1}", dd))

        reset = Gtk.Button(label="Reset theme to defaults")
        reset.set_margin_top(12)
        reset.connect("clicked", self._on_reset_theme)
        page.append(reset)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(page)
        return scroll

    # -- state ----------------------------------------------------------

    def _summarise_media(self) -> str:
        paths = self.config.media_paths
        if not paths:
            return "Nothing selected."
        if len(paths) == 1:
            return Path(paths[0]).name
        videos = sum(1 for p in paths if is_video(p))
        stills = len(paths) - videos
        parts = []
        if stills:
            parts.append(f"{stills} image{'s' if stills != 1 else ''}")
        if videos:
            parts.append(f"{videos} video{'s' if videos != 1 else ''}")
        return " + ".join(parts) + f" — first: {Path(paths[0]).name}"

    def _touch(self) -> None:
        """Mark settings as changed so the preview and Apply button update."""
        self._dirty = True

    def _on_sample_changed(self) -> None:
        value = self.sample.get_value()
        if value < 0.5:
            note = "Very fast — CPU and network readings will look jumpy."
        elif value <= 2.0:
            note = "Smooth and responsive."
        elif value <= 5.0:
            note = "Calm; brief spikes may be missed."
        else:
            note = "Very slow — good for a glanceable display, poor for spotting spikes."
        self.sample_note.set_text(note)
        # Rebuild the preview collector so the change is visible immediately.
        self.collector = Collector(value)
        self._touch()

    def _refresh_mode_sensitivity(self) -> None:
        """Grey out controls that do not apply to the selected mode."""
        mode = MODES[self.mode_dd.get_selected()]
        display = mode == "display"
        overlay = display and self.overlay.get_active()
        self.overlay.set_sensitive(display)
        for w in getattr(self, "_monitor_widgets", []):
            w.set_sensitive(overlay)
        for w in getattr(self, "_media_widgets", []):
            w.set_sensitive(display)
        for w in getattr(self, "_scaling_widgets", []):
            w.set_sensitive(True)
        self._touch()

    def collect(self) -> Config:
        """Read every widget back into a Config."""
        cfg = self.config
        cfg.mode = MODES[self.mode_dd.get_selected()]
        cfg.overlay = self.overlay.get_active()
        cfg.layout = list(LAYOUTS)[self.layout_dd.get_selected()]
        cfg.fps = int(self.fps.get_value())
        cfg.sample_interval = round(self.sample.get_value(), 1)
        cfg.brightness = int(self.brightness.get_value())
        cfg.fit = FITS[self.fit_dd.get_selected()]
        cfg.image_interval = float(self.interval.get_value())
        cfg.loop = self.loop.get_active()

        theme = Theme()
        for key, _ in COLOR_FIELDS:
            setattr(theme, key, rgba_to_hex(self.color_buttons[key].get_rgba()))
        theme.arcs = [METRIC_KEYS[d.get_selected() - 1] for d in self.arc_dds
                      if d.get_selected() > 0]
        theme.bars = [METRIC_KEYS[d.get_selected() - 1] for d in self.bar_dds
                      if d.get_selected() > 0]
        theme.cells = [METRIC_KEYS[d.get_selected()] for d in self.cell_dds]
        theme.scrim = round(self.scrim.get_value() / 100, 2)
        theme.auto_contrast = self.auto_contrast.get_active()
        theme.show_network = self.show_net.get_active()
        theme.show_footer = self.show_footer.get_active()
        cfg.theme = theme
        return cfg

    # -- actions --------------------------------------------------------

    def _on_switch(self, _switch, state: bool) -> bool:
        # Ignore programmatic updates from the status poll; acting on those
        # would issue a spurious start/stop on every tick and fight any
        # restart already in flight.
        if self._syncing:
            return False
        if state and not UDEV_PATH.exists():
            # Without the rule the service starts and immediately dies on
            # EACCES, which reads as "the switch is broken". Ask for the rule
            # instead. The window is unresponsive while the auth dialog is up.
            self._flash("Installing udev rule…")
            while GLib.MainContext.default().iteration(False):
                pass  # paint the label before blocking on the password prompt
            if cmd_install_udev(None) != 0:
                self._flash("Could not install the udev rule — panel needs device access")
                self._set_switch(False)
                return True
        result = systemctl("start" if state else "stop", SERVICE)
        if result.returncode != 0:
            self._flash(f"Service {'start' if state else 'stop'} failed")
        return False

    def _on_apply(self, _btn) -> None:
        cfg = self.collect()
        cfg.save()
        self._dirty = False
        if service_active():
            result = systemctl("restart", SERVICE)
            self._flash("Applied" if result.returncode == 0 else "Restart failed")
        else:
            self._flash("Saved (service is stopped)")

    def _on_reset_theme(self, _btn) -> None:
        default = Theme()
        for key, _ in COLOR_FIELDS:
            self.color_buttons[key].set_rgba(hex_to_rgba(getattr(default, key)))
        for i, dd in enumerate(self.arc_dds):
            key = default.arcs[i] if i < len(default.arcs) else None
            dd.set_selected((METRIC_KEYS.index(key) + 1) if key in METRIC_KEYS else 0)
        for i, dd in enumerate(self.bar_dds):
            key = default.bars[i] if i < len(default.bars) else None
            dd.set_selected((METRIC_KEYS.index(key) + 1) if key in METRIC_KEYS else 0)
        for i, dd in enumerate(self.cell_dds):
            dd.set_selected(METRIC_KEYS.index(default.cells[i]))
        self.scrim.set_value(default.scrim * 100)
        self.auto_contrast.set_active(default.auto_contrast)
        self.show_net.set_active(default.show_network)
        self.show_footer.set_active(default.show_footer)
        self._touch()

    def _on_pick_media(self, _btn) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose images and videos")

        media_filter = Gtk.FileFilter()
        media_filter.set_name("Images and videos")
        for suffix in sorted(IMAGE_SUFFIXES | VIDEO_SUFFIXES):
            media_filter.add_suffix(suffix.lstrip("."))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(media_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(media_filter)

        def done(dlg, result):
            try:
                files = dlg.open_multiple_finish(result)
            except GLib.Error:
                return  # user cancelled
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self.config.media_paths = [p for p in paths if p]
            self.media_label.set_text(self._summarise_media())
            self._touch()

        dialog.open_multiple(self, None, done)

    def _on_clear_media(self, _btn) -> None:
        self.config.media_paths = []
        self.media_label.set_text(self._summarise_media())
        self._touch()

    def _set_switch(self, state: bool) -> None:
        """Move the switch without re-entering the start/stop handler."""
        self._syncing = True
        try:
            self.switch.set_active(state)
        finally:
            self._syncing = False

    def _flash(self, text: str) -> None:
        self.status.set_text(text)
        GLib.timeout_add_seconds(4, lambda: (self.status.set_text(""), False)[1])

    # -- preview loop ---------------------------------------------------

    def _tick(self) -> bool:
        self._set_switch(service_active())
        try:
            self._update_preview()
        except Exception as exc:  # a broken preview must not kill the GUI
            self.preview_note.set_text(f"Preview unavailable: {exc}")
        return True

    def _preview_background(self, cfg) -> "Image.Image | None":
        """The still to preview behind the stats, if one can be shown locally.

        A video background cannot be reproduced here, so those fall through to
        the frame the service published instead.
        """
        first = next((p for p in cfg.media_paths if not is_video(p)), None)
        if first is None:
            return None
        if first == self._bg_cache_for:
            return self._bg_cache
        try:
            from PIL import Image as PILImage

            with PILImage.open(first) as im:
                self._bg_cache = fit_image(im, (480, 480), cfg.fit)
            self._bg_cache_for = first
            return self._bg_cache
        except Exception:
            return None

    def _update_preview(self) -> None:
        cfg = self.collect()

        # Render locally when we can, so theme edits show up before Apply.
        local = cfg.mode == "display" and cfg.overlay
        has_video_bg = any(is_video(p) for p in cfg.media_paths)
        if local and not (has_video_bg and cfg.media_paths):
            background = self._preview_background(cfg)
            img = render(self.collector.read(), (480, 480), self.fonts, cfg.theme,
                         cfg.layout, background=background)
            self._show_pil(img)
            self.preview_note.set_text(
                "Live preview of your current settings. Press Apply to send it to the panel."
                if self._dirty else "Live preview of your current settings."
            )
            return

        # Other modes are not reproducible here, so show what the service sent.
        if PREVIEW_PATH.exists():
            mtime = PREVIEW_PATH.stat().st_mtime
            if mtime != self._preview_mtime:
                self._preview_mtime = mtime
                try:
                    self.picture.set_filename(str(PREVIEW_PATH))
                except Exception:
                    pass
            age = time.time() - mtime
            self.preview_note.set_text(
                "Showing the panel's current frame."
                if age < 5 else f"Last frame was {age:.0f}s ago — is the service running?"
            )
        else:
            self.picture.set_paintable(None)
            self.preview_note.set_text(
                "No preview yet. Start the panel to see what it is showing."
            )

    def _show_pil(self, img) -> None:
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(buf.getvalue()))
        self.picture.set_paintable(texture)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.local.HongtaiPanel")

    def do_activate(self) -> None:
        win = self.props.active_window or Window(self)
        win.present()


def run_gui() -> int:
    return App().run([])
