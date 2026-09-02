#!/usr/bin/env bash
# Install hongtai-panel for the current user.
#
#   ./install.sh              install everything
#   ./install.sh --no-service skip enabling the autostart service
#
# Re-running is safe: it upgrades in place and leaves your config alone.
set -euo pipefail

cd "$(dirname "$0")"

WANT_SERVICE=1
[[ "${1:-}" == "--no-service" ]] && WANT_SERVICE=0

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }

# -- dependencies ------------------------------------------------------------

say "Checking dependencies"

if ! command -v python3 >/dev/null; then
    echo "python3 is required" >&2
    exit 1
fi
python3 - <<'EOF' || { echo "Python 3.10+ is required" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
ok "python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

missing=()
python3 -c "import PIL" 2>/dev/null || missing+=("Pillow")
python3 -c "import psutil" 2>/dev/null || missing+=("psutil")
python3 -c "import serial" 2>/dev/null || missing+=("pyserial")

if [[ ${#missing[@]} -gt 0 ]]; then
    warn "will install via pip: ${missing[*]}"
else
    ok "Pillow, psutil, pyserial present"
fi

command -v ffmpeg >/dev/null && ok "ffmpeg (video backgrounds)" \
    || warn "ffmpeg missing — video and GIF backgrounds will not work"

python3 -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null && ok "GTK4 (GUI)" \
    || warn "PyGObject/GTK4 missing — the GUI will not start (CLI still works)"

python3 -c "import gi; gi.require_version('Gst','1.0')" 2>/dev/null && ok "GStreamer (mirroring)" \
    || warn "GStreamer missing — screen mirroring will not work"

# -- package -----------------------------------------------------------------

say "Installing the package"
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    pip install --upgrade . >/dev/null
    BIN="$(python3 -c 'import sys,os;print(os.path.join(sys.prefix,"bin"))')"
elif command -v pipx >/dev/null 2>&1; then
    # Distros like Arch/CachyOS mark the system Python as externally managed
    # (PEP 668), so pip refuses even --user installs there. pipx gives an
    # isolated env while still symlinking the entry point into ~/.local/bin,
    # which is where the systemd service and desktop entry expect it.
    # --system-site-packages lets that env still see PyGObject/GTK4, which is
    # a system package (via pacman/apt) rather than something pip can build.
    pipx install --force --system-site-packages . >/dev/null
    BIN="$(pipx environment --value PIPX_BIN_DIR)"
else
    if ! pip_out=$(pip install --user --upgrade . 2>&1); then
        if grep -q externally-managed-environment <<<"$pip_out"; then
            warn "system Python is externally managed; retrying with --break-system-packages"
            pip install --user --upgrade --break-system-packages . >/dev/null
        else
            echo "$pip_out" >&2
            exit 1
        fi
    fi
    BIN="$(python3 -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
fi
ok "installed to $BIN/hongtai-panel"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN is not on your PATH — add it to your shell profile" ;;
esac

# -- device access -----------------------------------------------------------

say "Granting device access"
RULE=/etc/udev/rules.d/99-hongtai-panel.rules
if [[ -f "$RULE" ]]; then
    ok "udev rule already installed"
else
    "$BIN/hongtai-panel" install-udev || warn "could not install the udev rule; run it yourself later"
fi

# -- desktop entry -----------------------------------------------------------

say "Installing the desktop entry"
install -Dm644 hongtai-panel.desktop "$HOME/.local/share/applications/hongtai-panel.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
ok "\"LCD Panel\" added to your application menu"

# -- service -----------------------------------------------------------------

if [[ $WANT_SERVICE -eq 1 ]]; then
    say "Setting up autostart"
    install -Dm644 systemd/hongtai-panel.service \
        "$HOME/.config/systemd/user/hongtai-panel.service"
    systemctl --user daemon-reload
    systemctl --user enable --now hongtai-panel.service
    sleep 3
    if systemctl --user is-active --quiet hongtai-panel.service; then
        ok "service running"
    else
        warn "service did not start — check: journalctl --user -u hongtai-panel -n 20"
        warn "if it is a permissions error, replug the panel so the udev rule applies"
    fi
fi

say "Done"
echo "  Launch the GUI:   hongtai-panel gui"
echo "  Check the panel:  hongtai-panel info"
echo "  Logs:             journalctl --user -u hongtai-panel -f"
