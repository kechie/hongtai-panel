"""Hardware telemetry collection.

Everything here degrades to None rather than raising: a missing sensor should
blank one tile, not kill the render loop.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

HWMON = Path("/sys/class/hwmon")

# Metrics a theme can place in an arc, meter, or grid cell. Each entry maps a
# stable key to (display label, percentage, sub-caption).
METRICS: dict[str, tuple[str, callable, callable]] = {}


def _register(key: str, label: str, pct, sub) -> None:
    METRICS[key] = (label, pct, sub)


def _fmt_temp(v: float | None) -> str:
    return f"{v:.0f}°C" if v is not None else "--"


@dataclass
class Stats:
    cpu_percent: float = 0.0
    cpu_temp: float | None = None
    cpu_freq: float | None = None
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    gpu_percent: float | None = None
    gpu_temp: float | None = None
    gpu_mem_percent: float | None = None
    gpu_power: float | None = None
    net_up_mbps: float = 0.0
    net_down_mbps: float = 0.0
    disk_percent: float = 0.0
    uptime_hours: float = 0.0


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _find_hwmon(*names: str) -> Path | None:
    """Return the hwmon directory whose `name` matches any of `names`."""
    wanted = {n.lower() for n in names}
    try:
        for d in sorted(HWMON.iterdir()):
            try:
                if (d / "name").read_text().strip().lower() in wanted:
                    return d
            except OSError:
                continue
    except OSError:
        pass
    return None


class _NvidiaPoller:
    """Polls nvidia-smi on a background thread.

    nvidia-smi costs ~100 ms per invocation, which would stall a 30 fps render
    loop. Polling out-of-band keeps the loop free-running and slightly stale,
    which is the right trade for a status display.
    """

    FIELDS = "utilization.gpu,temperature.gpu,memory.used,memory.total,power.draw"

    def __init__(self, interval: float = 2.0):
        self.interval = interval
        self.values: dict[str, float] = {}
        self._available = shutil.which("nvidia-smi") is not None
        if self._available:
            threading.Thread(target=self._loop, daemon=True, name="nvidia-poll").start()

    def _loop(self) -> None:
        while True:
            try:
                out = subprocess.run(
                    ["nvidia-smi", f"--query-gpu={self.FIELDS}", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip().splitlines()
                if out:
                    parts = [p.strip() for p in out[0].split(",")]
                    util, temp, used, total, power = (float(p) for p in parts)
                    self.values = {
                        "util": util,
                        "temp": temp,
                        "mem": (used / total * 100) if total else 0.0,
                        "power": power,
                    }
            except Exception:
                self.values = {}
            time.sleep(self.interval)


class _SysfsGpu:
    """Reads AMD (amdgpu) or Intel (i915/xe) GPU stats straight from sysfs.

    No subprocess, so unlike the nvidia path this is cheap enough to read
    inline. Any field the driver does not expose is simply omitted.
    """

    DRIVERS = ("amdgpu", "i915", "xe")

    def __init__(self, drm_root: Path | str = "/sys/class/drm"):
        self.device: Path | None = None
        self.hwmon: Path | None = None
        for card in sorted(Path(drm_root).glob("card[0-9]*")):
            dev = card / "device"
            driver = ""
            try:
                driver = (dev / "uevent").read_text()
            except OSError:
                continue
            if not any(f"DRIVER={d}" in driver for d in self.DRIVERS):
                continue
            # A card with no busy counter tells us nothing useful.
            if not (dev / "gpu_busy_percent").exists():
                continue
            self.device = dev
            self.hwmon = next(iter((dev / "hwmon").glob("hwmon*")), None)
            break

    @property
    def available(self) -> bool:
        return self.device is not None

    @property
    def values(self) -> dict[str, float]:
        if not self.device:
            return {}
        out: dict[str, float] = {}
        busy = _read_int(self.device / "gpu_busy_percent")
        if busy is not None:
            out["util"] = float(busy)
        used = _read_int(self.device / "mem_info_vram_used")
        total = _read_int(self.device / "mem_info_vram_total")
        if used is not None and total:
            out["mem"] = used / total * 100
        if self.hwmon:
            temp = _read_int(self.hwmon / "temp1_input")
            if temp is not None:
                out["temp"] = temp / 1000.0
            for name in ("power1_average", "power1_input"):
                power = _read_int(self.hwmon / name)
                if power is not None:
                    out["power"] = power / 1_000_000.0  # microwatts
                    break
        return out


class Collector:
    """Samples system state.

    `read()` is safe to call once per rendered frame: it returns a cached
    snapshot and only re-samples once `interval` has elapsed. Sampling is
    deliberately decoupled from the frame rate, because CPU and network
    figures are deltas over the sampling window — reading them 15 times a
    second yields noise, not detail.
    """

    def __init__(self, interval: float = 1.0):
        self.interval = max(0.1, float(interval))
        self._cpu_hwmon = _find_hwmon("coretemp", "k10temp", "zenpower")
        # Prefer sysfs (AMD/Intel): no subprocess, so it reads inline. Fall back
        # to nvidia-smi, which costs ~100 ms per call and so is polled on a
        # background thread, never faster than 1 s.
        self._sysfs_gpu = _SysfsGpu()
        self._nvidia = None if self._sysfs_gpu.available else _NvidiaPoller(max(1.0, self.interval))
        self._last_net = psutil.net_io_counters()
        self._last_net_at = time.monotonic()
        self._cached: Stats | None = None
        self._cached_at = 0.0
        psutil.cpu_percent(interval=None)  # prime the delta

    def _cpu_temp(self) -> float | None:
        if self._cpu_hwmon:
            # temp1_input is "Package id 0" on coretemp, "Tctl" on k10temp.
            v = _read_int(self._cpu_hwmon / "temp1_input")
            if v is not None:
                return v / 1000.0
        try:
            for entries in psutil.sensors_temperatures().values():
                for e in entries:
                    if e.current:
                        return e.current
        except Exception:
            pass
        return None

    def _disk_percent(self) -> float:
        """Usage of the filesystem holding $HOME.

        On image-based distros (Bazzite, Silverblue) `/` is a read-only ostree
        deployment that always reports 100% full, which is true but useless.
        """
        for path in (str(Path.home()), "/var/home", "/home", "/"):
            try:
                return psutil.disk_usage(path).percent
            except Exception:
                continue
        return 0.0

    def _net_rates(self) -> tuple[float, float]:
        now = time.monotonic()
        counters = psutil.net_io_counters()
        elapsed = now - self._last_net_at
        if elapsed <= 0:
            return 0.0, 0.0
        up = (counters.bytes_sent - self._last_net.bytes_sent) * 8 / elapsed / 1e6
        down = (counters.bytes_recv - self._last_net.bytes_recv) * 8 / elapsed / 1e6
        self._last_net, self._last_net_at = counters, now
        return max(0.0, up), max(0.0, down)

    def read(self) -> Stats:
        """Return the current snapshot, re-sampling only when it has aged out."""
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self.interval:
            return self._cached
        self._cached = self._sample()
        self._cached_at = now
        return self._cached

    def _sample(self) -> Stats:
        mem = psutil.virtual_memory()
        freq = psutil.cpu_freq()
        up, down = self._net_rates()
        gpu = self._sysfs_gpu.values if self._sysfs_gpu.available else self._nvidia.values
        disk = self._disk_percent()

        return Stats(
            cpu_percent=psutil.cpu_percent(interval=None),
            cpu_temp=self._cpu_temp(),
            cpu_freq=(freq.current / 1000.0) if freq else None,
            ram_percent=mem.percent,
            ram_used_gb=mem.used / 1024**3,
            ram_total_gb=mem.total / 1024**3,
            gpu_percent=gpu.get("util"),
            gpu_temp=gpu.get("temp"),
            gpu_mem_percent=gpu.get("mem"),
            gpu_power=gpu.get("power"),
            net_up_mbps=up,
            net_down_mbps=down,
            disk_percent=disk,
            uptime_hours=(time.time() - psutil.boot_time()) / 3600.0,
        )


# -- metric registry ---------------------------------------------------------
# Registered after Stats so the lambdas can reference its fields.

_register("cpu", "CPU", lambda s: s.cpu_percent, lambda s: _fmt_temp(s.cpu_temp))
_register("gpu", "GPU", lambda s: s.gpu_percent, lambda s: _fmt_temp(s.gpu_temp))
_register("ram", "RAM", lambda s: s.ram_percent,
          lambda s: f"{s.ram_used_gb:.1f} / {s.ram_total_gb:.0f} GB")
_register("vram", "VRAM", lambda s: s.gpu_mem_percent,
          lambda s: f"{s.gpu_mem_percent:.0f}%" if s.gpu_mem_percent is not None else "--")
_register("disk", "DISK", lambda s: s.disk_percent, lambda s: f"{s.disk_percent:.0f}%")
_register("cpu_temp", "CPU TEMP", lambda s: s.cpu_temp, lambda s: _fmt_temp(s.cpu_temp))
_register("gpu_temp", "GPU TEMP", lambda s: s.gpu_temp, lambda s: _fmt_temp(s.gpu_temp))
_register("gpu_power", "GPU PWR", lambda s: s.gpu_power,
          lambda s: f"{s.gpu_power:.0f} W" if s.gpu_power is not None else "--")
_register("net_down", "DOWN", lambda s: min(100.0, s.net_down_mbps),
          lambda s: f"{s.net_down_mbps:.1f} Mb/s")
_register("net_up", "UP", lambda s: min(100.0, s.net_up_mbps),
          lambda s: f"{s.net_up_mbps:.1f} Mb/s")


def metric(key: str, stats: Stats) -> tuple[str, float | None, str]:
    """Resolve a metric key to (label, percent, sub-caption).

    Temperature metrics report degrees, not a percentage; they are scaled
    against a 100 degree ceiling so the same gauge code can draw them.
    """
    label, pct_fn, sub_fn = METRICS.get(key, ("--", lambda s: None, lambda s: ""))
    try:
        return label, pct_fn(stats), sub_fn(stats)
    except Exception:
        return label, None, "--"
