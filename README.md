# hongtai-panel

**Linux support for the LCD screen on LovingCool AIO CPU coolers.**

These coolers ship a small square display on the pump head, and the only
software for it is Windows-only — "Screen Driver Software.exe", an Electron app
called *LOVINGCOOL MONITOR*. There is no Linux build, so on Linux the screen
just sits blank. This project drives it.

<!-- Also sold under other brand names; the panel identifies itself as HONGTAI.
     Searchable terms: LovingCool AIO LCD Linux, HONGTAI MONITOR 33c3:7791,
     33c3:7792, TXW818-ST7701S, AIO cooler screen Linux driver. -->

| | |
| --- | --- |
| Hardware | LovingCool AIO cooler LCD (pump-head screen) |
| USB ID | `33c3:7791` or `33c3:7792` — reports as `HONGTAI MONITOR` |
| Confirmed models | `TXW818-ST7701S-4.0inch` (480×480) and `TXW818-ST7701S-5.5inch-hor` (960×480), both firmware 3.1 |
| Replaces | LOVINGCOOL MONITOR (Windows only) |

Check yours with `lsusb | grep 33c3:`. Matching is by vendor ID only (`33c3`),
not product ID, since the same panel family ships under more than one PID —
other panels in the same family should work too, as the software asks the
device for its own geometry, frame budget, and pixel format rather than
assuming.

What you get: a live CPU/GPU/RAM dashboard, wallpapers and video/GIF playlists,
the dashboard drawn *over* those backgrounds, or a mirror of any monitor or
window — with a GTK4 control panel and a systemd service that starts it at login.

## Why there is no kernel driver here

The panel enumerates as **USB CDC-ACM** (`bDeviceClass 2, Communications /
Abstract modem`). Linux binds it with the in-tree `cdc_acm` driver
automatically, so it already appears as `/dev/ttyACM*` with no work at all. The
Windows "driver" is not a driver — it is a desktop app that opens the device as
a COM port and pushes JPEG frames at it. This project is a port of that app's
protocol, not of a kernel module.

## Install

```bash
git clone https://github.com/GOG1071/hongtai-panel.git
cd hongtai-panel && ./install.sh
```

The script checks dependencies, installs the package, adds the udev rule, adds
the app-menu entry, and enables the autostart service. Pass `--no-service` to
skip autostart. Re-running upgrades in place and leaves your config alone.
**Replug the panel afterwards** so the udev rule takes effect.

The package normally installs with `pip install --user`. On distros that mark
the system Python as externally managed (Arch/CachyOS, Debian 12+, and
others — PEP 668), `pip` refuses that outright, so the script installs with
`pipx` instead if it's available (`--system-site-packages`, so it can still
see GTK4/PyGObject and GStreamer, which come from your distro's packages, not
pip). Either way the binary ends up on your `PATH` at `~/.local/bin`, which is
where the systemd service and app-menu entry expect it. If you're running the
project from inside an activated virtualenv, the script detects that too and
installs into it instead.

### System dependencies

Python 3.10+, plus Pillow / psutil / pyserial (pip installs these for you).
Everything else is optional and only disables one feature if absent — the
install script reports what it finds.

| For | Package |
| --- | --- |
| Video and GIF backgrounds | `ffmpeg` |
| The GUI | GTK4 + PyGObject |
| Screen mirroring | GStreamer with the PipeWire plugin |

```bash
# Fedora / Bazzite / RHEL
sudo dnf install ffmpeg python3-gobject gtk4 \
    gstreamer1-plugin-pipewire gstreamer1-plugins-good

# Debian / Ubuntu
sudo apt install ffmpeg python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
    gstreamer1.0-pipewire gstreamer1.0-plugins-good

# Arch
sudo pacman -S ffmpeg python-gobject gtk4 gst-plugin-pipewire gst-plugins-good
```

### Moving it to another machine

Any of these work; the install script is the same either way.

```bash
git clone https://github.com/GOG1071/hongtai-panel.git
cd hongtai-panel && ./install.sh
```

Your settings live in `~/.config/hongtai-panel/config.json` and are *not* part
of the repo — copy that file across too if you want the same theme and layout.
Nothing in the code is machine-specific: the panel is found by USB ID rather
than device path, and geometry, frame budget, and pixel format all come from the
panel's own `0x06` reply, so a different model configures itself.

## Use

The GUI is the easy path — "LCD Panel" in your application menu, or:

```bash
hongtai-panel gui
```

It edits the config file and restarts the service; it never opens the serial
port itself, so there is exactly one writer and the panel keeps running after
you close the window. When the stats overlay is on and the background is a
still, the preview renders locally so theme edits show up instantly before you
press Apply; for video backgrounds and mirroring it displays the frame the
service last published, which is what is genuinely on the panel.

Everything is also available from the command line:

```bash
hongtai-panel info                        # geometry, firmware, frame budget
hongtai-panel monitor                     # live CPU/GPU/RAM dashboard
hongtai-panel monitor --theme compact --fps 10
hongtai-panel run                         # whatever the config says
hongtai-panel play ~/Pictures/wall.png    # a still image
hongtai-panel play ~/Videos/loop.mp4      # video or animated GIF
hongtai-panel play ~/Pictures/            # everything in a folder, in order
hongtai-panel play a.png clip.mp4 b.jpg --interval 30   # mixed playlist
hongtai-panel mirror                      # mirror a monitor or window
hongtai-panel brightness 60
hongtai-panel clear
hongtai-panel --rotation 90 monitor       # correct for how the panel is mounted
```

`--rotation` (0/90/180/270, clockwise) goes before the subcommand and applies to
`run`, `monitor`, `play`, and `mirror`. It defaults to the `rotation` setting in
the config (180 out of the box), which assumes the panel ends up upside-down
relative to its native orientation — true for these panels mounted in a
chassis rather than on the cooler pump head they were designed for. Set it to
`0` if your mount doesn't need correcting.

`play` takes images, videos, GIFs, directories, or any mix, and plays them in
the order given. Stills are held for `--interval` seconds; clips play to their
end. A lone video repeats inside ffmpeg rather than relaunching it every few
seconds. `show` is a deprecated alias for the same command.

Add `--overlay` to draw the system stats on top of whatever is playing:

```bash
hongtai-panel play ~/Pictures/wallpaper.jpg --overlay
hongtai-panel play ~/Videos/loop.mp4 --overlay --theme compact --scrim 0.6
```

`mirror` opens your compositor's normal screen-share picker, so you choose the
monitor or single window there. It works on Wayland and X11 through
xdg-desktop-portal.

## Run it at login

```bash
mkdir -p ~/.config/systemd/user
cp systemd/hongtai-panel.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hongtai-panel
```

The service runs `hongtai-panel run`, which takes everything — mode, layout,
frame rate, brightness, theme — from `~/.config/hongtai-panel/config.json`.
Change settings in the GUI rather than editing `ExecStart`. Logs:
`journalctl --user -u hongtai-panel -f`

## Configuration

`~/.config/hongtai-panel/config.json` is the single source of truth, shared by
the GUI, the CLI's `run` command, and the service. It is written atomically, so
a reader never sees a half-written file. Editing it by hand is fine; restart the
service afterwards.

Two modes: `display` and `mirror`. `display` covers everything the panel can
show locally, as two independent switches:

| `media_paths` | `overlay` | Result |
| --- | --- | --- |
| empty | on | Plain stats dashboard |
| set | off | Wallpaper or video playlist |
| set | on | Stats drawn over the playlist |
| empty | off | Nothing — rejected at startup |

Configs from earlier versions are migrated on load, in two folds: the separate
`image` and `video` modes became one `media` playlist, and `monitor` + `media`
became `display` + `overlay`. Each fold preserves what was on screen — a
`monitor` config carrying a stale playlist has it dropped rather than promoted
to a background it never had.

### Keeping the overlay readable

Text has to stay legible over an arbitrary photo, which needs more than one
trick:

- **Scrim** (`theme.scrim`, default 0.45) dims the background before the stats
  are drawn.
- **Auto-contrast** (`theme.auto_contrast`, default on) treats the scrim as a
  floor and darkens pale wallpapers further until mean luminance reaches 78, so
  a white image cannot wash out light-on-dark text.
- **Shadow** — a blurred copy of the stats layer's own alpha, painted black
  underneath it. This is what actually guarantees contrast, because it adapts to
  the layer's shape rather than assuming anything about the background.

The shadow and the stats layer are rebuilt only when telemetry re-samples, and
the dimmed background is cached per still, so compositing costs about 1.6 ms per
frame rather than being redone from scratch.

### Frame rate vs sensor refresh

These are separate knobs and it is worth understanding why.

`fps` is how often a frame is drawn and sent. `sample_interval` is how often
telemetry is re-read; between samples the renderer reuses the last snapshot.

CPU utilisation and network throughput are *deltas measured over the sampling
window*, so a short interval does not give more detail — it gives noise.
Measured on an idle-ish desktop:

| `sample_interval` | mean CPU% | stdev | observed range |
| --- | --- | --- | --- |
| 0.066s (per frame at 15 fps) | 25.6 | 4.97 | 20–32 |
| 0.5s | 22.4 | 1.58 | 20–25 |
| 1.0s (default) | 22.6 | 0.80 | 21–24 |

The default of 1.0s reads as a steady number rather than a flickering one. Drop
it toward 0.2s if you specifically want to catch brief spikes and don't mind the
jitter; raise it past 3s for a calm, glanceable display.

It is not primarily a performance setting: rendering and JPEG encoding dominate
at roughly 3.1 ms/frame, so raising the interval saves only about 6% CPU. Lower
`fps` if you want to save power — that is the knob with real leverage.

## Protocol

Reverse-engineered from `main/_baseClass/device.js` in the vendor app's
`app.asar`. Two framings share one serial link at 2 Mbaud (the rate is
cosmetic — CDC-ACM ignores it).

**Control frames**

```
55 AA │ len_lo len_hi │ key │ payload │ ck_lo ck_hi
        len = len(payload) + 7
        ck  = sum(all preceding bytes) & 0xFFFF
```

Replies carry UTF-8 JSON between the 5-byte head and the 2-byte checksum, with
the useful fields nested one level down under `data`:

```json
{"status":200,"cmd":"info","data":{"uid":"XXXXXXXXXXXX","width":480,"height":480,
 "model":"TXW818-ST7701S-4.0inch","version":"3.1","angle":270,"brightness":80,
 "shape":"0BB1","region":"","diplay_on":true}}
```

A non-JSON payload is a one-byte error code: `01` failed, `02` out of memory,
`03` storage full, `04` SD full, `05` no such file, `06` open failed,
`07` write failed.

| Opcode | Meaning |
| --- | --- |
| `0x01` | restart |
| `0x03` | set brightness, payload `[0–100]` |
| `0x06` | get device info → JSON `{width,height,angle,version,uid,model,region}` |
| `0x0C` | begin OTA firmware write |
| `0x11` | live-mode keepalive |
| `0x14` | motion timeout before screen-off |
| `0x15` | motion timeout (fw ≥ 2.8) |
| `0x20` | set region |
| `0x21` | close (fw ≥ 3.1) |
| `0x23` | set serial number |
| `0x25` | motor on/off |
| `0x26` | realtime-play timeout (fw ≥ 4.1) |

**Image frames** are *not* wrapped in `55 AA`.

```
len_le32 │ jpeg │ ck_lo ck_hi          firmware > 2.8
jpeg                                    firmware ≤ 2.8
rgb565_be                               SPI-class panels
```

Three behaviours matter for a working implementation:

- **Keepalive is mandatory.** The firmware leaves live mode if it stops seeing
  opcode `0x11`; the vendor app resends it every 1500 ms. Frames sent after
  that are silently dropped.
- **`FF D9 FF D9` then `00 00 00 00`** aborts a partially-sent frame and blanks
  the panel. Send it on connect, or a half-written frame from a previous run
  desynchronises the parser.
- **Frames have a per-model size budget** (80 KB typical, 260 KB on 6.67",
  350 KB on 10.26"). The vendor app steps JPEG quality down until the frame
  fits; exceeding the cap wedges the display.

Writes are serialised behind a lock, because the keepalive thread shares the
port with the frame writer and interleaving corrupts both streams.

## Layout

| Path | Purpose |
| --- | --- |
| `hongtai_panel/protocol.py` | framing, opcodes, device handshake, live mode |
| `hongtai_panel/config.py` | config + theme dataclasses, atomic load/save |
| `hongtai_panel/sysinfo.py` | telemetry and the metric registry themes draw from |
| `hongtai_panel/render.py` | theme-driven layouts, fit + JPEG encoding |
| `hongtai_panel/sources.py` | frame sources: monitor, images, video, mirror |
| `hongtai_panel/cli.py` | argument parsing and the frame pump |
| `hongtai_panel/gui.py` | GTK4 control panel |

Adding a metric means one `_register(...)` line in `sysinfo.py`; it then appears
in every theme dropdown in the GUI automatically.

## Tested on

| | |
| --- | --- |
| Panel | `TXW818-ST7701S-4.0inch`, firmware 3.1, 480×480, uid `XXXXXXXXXXXX` |
| Host | Bazzite 44, KDE Plasma on Wayland, i5-13400F + NVIDIA |
| Verified | `info`, stats dashboard (both layouts), media playlists, stats-over-background, 23.6 fps sustained at 25 requested |
| Unverified | `mirror`; RGB565 output (no SPI panel to hand); AMD/Intel GPU readings (parsing tested against a synthetic sysfs tree, not real hardware) |

Also confirmed working: `33c3:7792` / `TXW818-ST7701S-5.5inch-hor` (960×480,
firmware 3.1), chassis-mounted rather than on a pump head, on CachyOS —
`info` and the stats dashboard, with `rotation` at the 180° default.

## Hardware support

GPU telemetry picks a backend automatically:

| Vendor | Source | Notes |
| --- | --- | --- |
| AMD | `amdgpu` sysfs | utilisation, temperature, VRAM, power |
| Intel | `i915` / `xe` sysfs | utilisation; other fields depend on the driver |
| NVIDIA | `nvidia-smi` | utilisation, temperature, VRAM, power |

sysfs is preferred where available because it reads inline with no subprocess.
`nvidia-smi` costs about 100 ms per call, so it is polled on a background thread
and never faster than once a second. A machine with no recognised GPU simply
leaves those tiles blank rather than failing.

CPU temperature comes from `coretemp`, `k10temp`, or `zenpower`, falling back to
whatever psutil exposes.

## Other panels

The vendor app drives a family of these screens. Anything that answers opcode
`0x06` should work; `hongtai-panel info` reports what it found.

SPI-class models take raw RGB565 instead of JPEG. The test is the vendor's own
`checkIsSPI`: the model string contains `2.99`, `TXW813-ST7789-2.8inch`, or
`qspi`. It is **not** the controller name — `TXW818-ST7701S-4.0inch` is a JPEG
panel despite the ST7701S controller, which is an easy and silent mistake to
make.

If frames tear or the panel wedges, lower the size budget with `--max-kb`. The
vendor caps one known-problematic combination (`TXW818-ST7701S-4.0inch` on
firmware 3.1, when the encrypted `region.json` marks it a LovinCool device) at
30 KB and 25 fps rather than the usual 80 KB and 30 fps.

## kechie patches

specific for jungle leopard display ac 02 chassis display hence the 180 degree rotation by default.
device id from lsusb is 33c3:7792. hardcoded much like  `GOG1071`'s device id. I have no other devices to test this on.

```
hongtai-panel info
00:00:00 INFO    connected: /dev/ttyACM0  960x480  fw=3.1  model=TXW818-ST7701S-5.5inch-hor  JPEG<=80KB
port          /dev/ttyACM0
resolution    960x480
firmware      3.1
model         TXW818-ST7701S-5.5inch-hor
uid           8965810B2B10
device angle  0deg (reported by the panel itself)
rotation      180deg clockwise (configured; --rotation to override)
pixel format  JPEG
frame budget  80 KB @ 30 fps
raw           {'uid': '8965810B2B10', 'width': 960, 'height': 480, 'diplay_on': True, 'brightness': 80, 'i_blocks': 4096, 'i_block_size': 2048, 'i_block_free': 65135, 'i_path': '', 'model': 'TXW818-ST7701S-5.5inch-hor', 'version': '3.1', 'shape': '0BB1', 'region': '', 'angle': 0}
```

## app running in jungle leopard ac02 display

![Jungle Leopard AC02 Display](https://amian.tech/hongtai-panel.jpeg)
