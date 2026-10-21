#!/bin/bash
# Install SoupaWhisper on Linux (X11 or Wayland)
# Supports: Ubuntu, Pop!_OS, Debian, Fedora, Arch

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/soupawhisper"
SERVICE_DIR="$HOME/.config/systemd/user"
UDEV_RULE="/etc/udev/rules.d/80-soupawhisper-uinput.rules"

has_nvidia() {
    command -v nvidia-smi &> /dev/null && nvidia-smi -L &> /dev/null
}

# Detect package manager
detect_package_manager() {
    if command -v apt &> /dev/null; then
        echo "apt"
    elif command -v dnf &> /dev/null; then
        echo "dnf"
    elif command -v pacman &> /dev/null; then
        echo "pacman"
    elif command -v zypper &> /dev/null; then
        echo "zypper"
    else
        echo "unknown"
    fi
}

# Install system dependencies (GTK/evdev bindings come from the distro)
install_deps() {
    local pm=$(detect_package_manager)

    echo "Detected package manager: $pm"
    echo "Installing system dependencies..."

    case $pm in
        apt)
            sudo apt update || echo "apt update had errors, continuing with cached package lists"
            sudo apt install -y alsa-utils libnotify-bin python3-gi python3-gi-cairo \
                gir1.2-gtk-3.0 python3-evdev
            ;;
        dnf)
            sudo dnf install -y alsa-utils libnotify python3-gobject gtk3 python3-evdev
            ;;
        pacman)
            sudo pacman -S --noconfirm alsa-utils libnotify python-gobject python-cairo gtk3 python-evdev
            ;;
        zypper)
            sudo zypper install -y alsa-utils libnotify-tools python3-gobject python3-gobject-cairo \
                typelib-1_0-Gtk-3_0 python3-evdev
            ;;
        *)
            echo "Unknown package manager. Please install manually:"
            echo "  alsa-utils libnotify PyGObject (GTK 3) pycairo python-evdev"
            ;;
    esac
}

# Let the user read keyboards and create a virtual keyboard
setup_input_permissions() {
    echo ""
    echo "Setting up keyboard access (input group + /dev/uinput)..."

    echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
        | sudo tee "$UDEV_RULE" > /dev/null
    echo uinput | sudo tee /etc/modules-load.d/soupawhisper-uinput.conf > /dev/null
    sudo modprobe uinput
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=uinput

    if ! id -nG "$USER" | grep -qw input; then
        sudo usermod -aG input "$USER"
        echo "Added $USER to the 'input' group. Log out and back in for it to apply."
        echo "Note: programs running as $USER can now read all keyboard input and"
        echo "inject keystrokes. That access is what push-to-talk needs."
    fi
}

# Install Python dependencies
install_python() {
    echo ""
    echo "Installing Python dependencies..."

    if ! command -v uv &> /dev/null; then
        echo "uv not found. Please install uv first:"
        echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi

    # System site-packages provide gi (PyGObject) and evdev
    uv venv --allow-existing --python /usr/bin/python3 --system-site-packages "$SCRIPT_DIR/.venv"

    if has_nvidia; then
        uv pip install --python "$SCRIPT_DIR/.venv/bin/python" -r "$SCRIPT_DIR/pyproject.toml" --extra gpu
    else
        uv pip install --python "$SCRIPT_DIR/.venv/bin/python" -r "$SCRIPT_DIR/pyproject.toml"
    fi
}

# Setup config file
setup_config() {
    echo ""
    echo "Setting up config..."
    mkdir -p "$CONFIG_DIR"

    if [ ! -f "$CONFIG_DIR/config.ini" ]; then
        cp "$SCRIPT_DIR/config.example.ini" "$CONFIG_DIR/config.ini"
        if ! has_nvidia; then
            sed -i -e 's/^model = .*/model = small.en/' \
                -e 's/^device = .*/device = cpu/' \
                -e 's/^compute_type = .*/compute_type = int8/' "$CONFIG_DIR/config.ini"
        fi
        echo "Created config at $CONFIG_DIR/config.ini"
    else
        echo "Config already exists at $CONFIG_DIR/config.ini"
    fi
}

# Install systemd service
install_service() {
    echo ""
    echo "Installing systemd user service..."

    mkdir -p "$SERVICE_DIR"

    # No DISPLAY/WAYLAND_DISPLAY here: the desktop session exports them to the
    # user manager (X auth paths are random per login on GNOME Wayland).
    # sg gives the service the input group even before the user logs out and
    # back in, so a keyboard that reconnects can still be reopened.
    cat > "$SERVICE_DIR/soupawhisper.service" << EOF2
[Unit]
Description=SoupaWhisper Voice Dictation
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/sg input -c "exec '$SCRIPT_DIR/.venv/bin/python' -u '$SCRIPT_DIR/dictate.py'"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
EOF2

    echo "Created service at $SERVICE_DIR/soupawhisper.service"

    # Reload and enable
    systemctl --user daemon-reload
    systemctl --user enable soupawhisper

    echo ""
    echo "Service installed! Commands:"
    echo "  systemctl --user start soupawhisper   # Start"
    echo "  systemctl --user stop soupawhisper    # Stop"
    echo "  systemctl --user status soupawhisper  # Status"
    echo "  journalctl --user -u soupawhisper -f  # Logs"
}

# Main
main() {
    echo "==================================="
    echo "  SoupaWhisper Installer"
    echo "==================================="
    echo ""

    install_deps
    setup_input_permissions
    install_python
    setup_config

    echo ""
    read -p "Install as systemd service? [y/N] " -n 1 -r
    echo ""

    if [[ $REPLY =~ ^[Yy]$ ]]; then
        install_service
    fi

    echo ""
    echo "==================================="
    echo "  Installation complete!"
    echo "==================================="
    echo ""
    echo "To run manually:"
    echo "  .venv/bin/python dictate.py"
    echo ""
    echo "Config: $CONFIG_DIR/config.ini"
    echo "Hotkey: Right Ctrl (hold to record)"
    echo "Exit:   Ctrl+C"
}

main "$@"
