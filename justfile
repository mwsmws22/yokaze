# Yokaze — one-button document scanning (Fujitsu ScanSnap today)

# List available recipes
default:
    @just --list

# Full install: editable Python package, desktop entry, systemd, udev
install:
    #!/usr/bin/env bash
    set -e

    GREEN='\033[0;32m'
    NC='\033[0m'
    ok() { echo -e "  ${GREEN}✓${NC} $1"; }

    SCRIPT_DIR="{{justfile_directory()}}"
    BIN="$HOME/.local/bin"
    ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
    PNG_ICON_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"
    DESKTOP_DIR="$HOME/.local/share/applications"

    echo "Installing Yokaze..."
    echo

    mkdir -p "$BIN"

    # Drop leftover public bash CLIs from pre-cutover installs
    rm -f "$BIN/scan" "$BIN/scan-doc" "$BIN/scan-button-poll"

    write_launcher() {
        local name="$1" module="$2" func="$3"
        printf '%s\n' \
            '#!/usr/bin/env bash' \
            "export YOKAZE_REPO_DIR=\"$SCRIPT_DIR\"" \
            "export YOKAZE_IDENTITY=user" \
            "export PYTHONPATH=\"$SCRIPT_DIR/src\${PYTHONPATH:+:\$PYTHONPATH}\"" \
            "exec python3 -c \"import sys; from ${module} import ${func}; sys.argv[0] = '${name}'; raise SystemExit(${func}())\" \"\$@\"" \
            > "$BIN/$name"
        chmod +x "$BIN/$name"
    }

    install_entrypoints() {
        # Prefer editable pip install (console scripts: yokaze, yokaze-scan, yokaze-button).
        if python3 -m pip --version &>/dev/null; then
            if python3 -m pip install --user -e "$SCRIPT_DIR"; then
                return 0
            fi
            # Arch and other PEP 668 distros need an explicit override for --user installs.
            if python3 -m pip install --user --break-system-packages -e "$SCRIPT_DIR"; then
                return 0
            fi
            echo "  ! pip install failed; falling back to repo launchers" >&2
        else
            echo "  ! python3 -m pip not found; installing repo launchers" >&2
            echo "    (optional: pacman -S python-pip for a real editable install)" >&2
        fi

        # Equivalent launchers matching [project.scripts] in pyproject.toml
        write_launcher yokaze yokaze.ui.__main__ main
        write_launcher yokaze-scan yokaze.headless.scan main
        write_launcher yokaze-button yokaze.headless.button main
    }

    install_entrypoints
    ok "Python entrypoints installed to ~/.local/bin/ (yokaze, yokaze-scan, yokaze-button)"

    DESKTOP_ID="com.github.yokaze.Yokaze"
    DESKTOP_FILE="$DESKTOP_DIR/$DESKTOP_ID.desktop"

    HICOLOR="$HOME/.local/share/icons/hicolor"
    mkdir -p "$DESKTOP_DIR"
    for size in 48 64 128 256 512; do
        mkdir -p "$HICOLOR/${size}x${size}/apps"
        cp "$SCRIPT_DIR/src/yokaze/ui/assets/icons/hicolor/${size}x${size}/apps/yokaze.png" \
           "$HICOLOR/${size}x${size}/apps/yokaze.png"
    done
    rm -f "$HICOLOR/scalable/apps/yokaze.svg"
    sed "s|^Exec=yokaze|Exec=$BIN/yokaze|" \
        "$SCRIPT_DIR/$DESKTOP_ID.desktop" \
        > "$DESKTOP_FILE"
    gtk-update-icon-cache -f "$HICOLOR" 2>/dev/null || true
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
    kbuildsycoca6 --noincremental 2>/dev/null || kbuildsycoca5 --noincremental 2>/dev/null || true
    ok "Desktop launcher and icon installed"

    mkdir -p "$HOME/.config/systemd/user"
    cp "$SCRIPT_DIR/systemd/yokaze-button.service" "$HOME/.config/systemd/user/"
    ok "Systemd service installed"

    echo
    echo "Installing udev rules (requires sudo)..."
    sudo cp "$SCRIPT_DIR/udev/99-scansnap.rules" /etc/udev/rules.d/
    ok "Udev rules installed"

    echo
    echo "Activating..."
    systemctl --user daemon-reload
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    ok "Systemd and udev reloaded"

    systemctl --user restart yokaze-button.service 2>/dev/null && \
        ok "Service started" || \
        echo "  ! Service not started (connect scanner to activate)"

    echo
    echo "Installation complete."
    echo "Open Yokaze from your app menu, then use Settings to pick a scanner and set up Paperless."
    echo "CLIs: yokaze (GUI), yokaze-scan (headless), yokaze-button (hardware poller)."

# Check dependencies for all modes
check:
    #!/usr/bin/env bash
    set -e

    GREEN='\033[0;32m'
    RED='\033[0;31m'
    NC='\033[0m'

    ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
    fail() { echo -e "  ${RED}✗${NC} $1"; }
    ALL_OK=true
    HAVE_MISSING=false
    HAVE_BROKEN=false

    check_cmd() {
        if command -v "$1" &>/dev/null; then
            ok "$1 found: $(command -v "$1")"
        else
            fail "$1 not found"
            ALL_OK=false
            HAVE_MISSING=true
        fi
    }

    echo "Core dependencies:"
    check_cmd scanimage
    check_cmd tesseract

    echo
    echo "Notification dependencies:"
    check_cmd notify-send
    check_cmd xdg-open
    check_cmd xdg-terminal-exec

    echo
    echo "GUI dependencies:"
    check_cmd python3
    if python3 -c "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk" &>/dev/null; then
        ok "python GTK 4 bindings found"
    else
        fail "python GTK 4 bindings missing"
        ALL_OK=false
        HAVE_MISSING=true
    fi
    set +e
    cv2_err=$(python3 -c "import cv2" 2>&1)
    cv2_rc=$?
    set -e
    if [ "$cv2_rc" -eq 0 ]; then
        ok "python OpenCV (cv2) found"
    elif printf '%s' "$cv2_err" | grep -q 'ModuleNotFoundError'; then
        fail "python OpenCV (cv2) not installed"
        echo "    Fix: sudo pacman -S python-opencv"
        ALL_OK=false
        HAVE_MISSING=true
    else
        fail "python OpenCV (cv2) broken"
        if command -v pacman &>/dev/null; then
            opencv_ver=$(pacman -Q opencv 2>/dev/null || echo 'opencv not installed')
            py_opencv_ver=$(pacman -Q python-opencv 2>/dev/null || echo 'python-opencv not installed')
            echo "    Packages: $opencv_ver | $py_opencv_ver"
        fi
        missing_so=$(printf '%s\n' "$cv2_err" | sed -n 's/.*: \(lib[^:]*\.so[^:]*\): cannot open shared object file.*/\1/p' | tail -n1)
        if [ -n "$missing_so" ]; then
            echo "    Missing library: $missing_so"
            echo "    Fix: sudo pacman -Syu"
        else
            echo "    Fix: sudo pacman -S opencv python-opencv"
        fi
        ALL_OK=false
        HAVE_BROKEN=true
    fi
    if python3 -c "from PIL import Image" &>/dev/null; then
        ok "python Pillow found"
    else
        fail "python Pillow missing"
        echo "    Fix: sudo pacman -S python-pillow"
        ALL_OK=false
        HAVE_MISSING=true
    fi
    check_cmd naps2

    echo
    echo "Paperless API mode: urllib (stdlib; no curl required)"

    echo
    if [ "$ALL_OK" = true ]; then
        echo "All dependencies found."
    elif [ "$HAVE_BROKEN" = true ] && [ "$HAVE_MISSING" = true ]; then
        echo "Some dependencies are missing or broken."
    elif [ "$HAVE_BROKEN" = true ]; then
        echo "Some dependencies are installed but broken (see Fix lines above)."
    else
        echo "Some dependencies are missing (not all may be needed for your mode)."
    fi

# Reinstall Python entrypoints + desktop/service and restart the button poller.
# Use after code changes; does not touch config, udev, or dependencies.
reload:
    #!/usr/bin/env bash
    set -e
    SCRIPT_DIR="{{justfile_directory()}}"
    BIN="$HOME/.local/bin"

    mkdir -p "$BIN"
    rm -f "$BIN/scan" "$BIN/scan-doc" "$BIN/scan-button-poll"

    write_launcher() {
        local name="$1" module="$2" func="$3"
        printf '%s\n' \
            '#!/usr/bin/env bash' \
            "export YOKAZE_REPO_DIR=\"$SCRIPT_DIR\"" \
            "export YOKAZE_IDENTITY=user" \
            "export PYTHONPATH=\"$SCRIPT_DIR/src\${PYTHONPATH:+:\$PYTHONPATH}\"" \
            "exec python3 -c \"import sys; from ${module} import ${func}; sys.argv[0] = '${name}'; raise SystemExit(${func}())\" \"\$@\"" \
            > "$BIN/$name"
        chmod +x "$BIN/$name"
    }

    install_entrypoints() {
        if python3 -m pip --version &>/dev/null; then
            if python3 -m pip install --user -e "$SCRIPT_DIR"; then
                return 0
            fi
            if python3 -m pip install --user --break-system-packages -e "$SCRIPT_DIR"; then
                return 0
            fi
            echo "pip install failed; falling back to repo launchers" >&2
        fi

        write_launcher yokaze yokaze.ui.__main__ main
        write_launcher yokaze-scan yokaze.headless.scan main
        write_launcher yokaze-button yokaze.headless.button main
    }

    install_entrypoints
    echo "Python entrypoints updated in $BIN"

    DESKTOP_ID="com.github.yokaze.Yokaze"
    HICOLOR="$HOME/.local/share/icons/hicolor"
    for size in 48 64 128 256 512; do
        mkdir -p "$HICOLOR/${size}x${size}/apps"
        cp "$SCRIPT_DIR/src/yokaze/ui/assets/icons/hicolor/${size}x${size}/apps/yokaze.png" \
           "$HICOLOR/${size}x${size}/apps/yokaze.png"
    done
    rm -f "$HICOLOR/scalable/apps/yokaze.svg"
    sed "s|^Exec=yokaze|Exec=$BIN/yokaze|" \
        "$SCRIPT_DIR/$DESKTOP_ID.desktop" \
        > "$HOME/.local/share/applications/$DESKTOP_ID.desktop"
    gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    echo "Desktop launcher and icon updated"

    if [ -f "$SCRIPT_DIR/systemd/yokaze-button.service" ]; then
        mkdir -p "$HOME/.config/systemd/user"
        cp "$SCRIPT_DIR/systemd/yokaze-button.service" "$HOME/.config/systemd/user/"
        systemctl --user daemon-reload
        echo "systemd service updated"
    fi

    systemctl --user restart yokaze-button.service 2>/dev/null && \
        echo "yokaze-button.service restarted" || \
        echo "yokaze-button.service not running (connect scanner to activate)"

# Launch the unified Yokaze app
gui *args:
    ./yokaze {{args}}

# Show service status
status:
    systemctl --user status yokaze-button.service

# Follow service logs
logs:
    journalctl --user -u yokaze-button.service -f

# Restart the service
restart:
    systemctl --user restart yokaze-button.service

# Remove installed files, service, and udev rules
uninstall:
    #!/usr/bin/env bash
    set -e

    GREEN='\033[0;32m'
    NC='\033[0m'
    ok() { echo -e "  ${GREEN}✓${NC} $1"; }

    echo "Uninstalling Yokaze..."
    echo

    systemctl --user stop yokaze-button.service 2>/dev/null && ok "Service stopped" || true
    systemctl --user disable yokaze-button.service 2>/dev/null && ok "Service disabled" || true

    # Python entrypoints + any leftover pre-cutover bash CLIs
    rm -f "$HOME/.local/bin/yokaze" \
          "$HOME/.local/bin/yokaze-scan" \
          "$HOME/.local/bin/yokaze-button" \
          "$HOME/.local/bin/scan" \
          "$HOME/.local/bin/scan-doc" \
          "$HOME/.local/bin/scan-button-poll"
    if python3 -m pip --version &>/dev/null; then
        python3 -m pip uninstall -y yokaze 2>/dev/null || \
            python3 -m pip uninstall -y --break-system-packages yokaze 2>/dev/null || true
    fi
    ok "Scripts removed from ~/.local/bin/"

    rm -f "$HOME/.local/share/applications/com.github.yokaze.Yokaze.desktop"
    rm -f "$HOME/.local/share/icons/hicolor"/*/apps/yokaze.png \
          "$HOME/.local/share/icons/hicolor/scalable/apps/yokaze.svg"
    ok "Desktop launcher and icon removed"

    rm -f "$HOME/.config/systemd/user/yokaze-button.service"
    ok "Systemd service removed"

    echo
    echo "Removing udev rules (requires sudo)..."
    sudo rm -f /etc/udev/rules.d/99-scansnap.rules /etc/udev/rules.d/99-scansnap-ix500.rules
    ok "Udev rules removed"

    systemctl --user daemon-reload
    sudo udevadm control --reload-rules
    ok "Systemd and udev reloaded"

    echo
    echo "Uninstalled. Config file ~/.config/environment.d/yokaze.conf was kept (delete manually if desired)."
