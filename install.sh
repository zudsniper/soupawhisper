#!/bin/bash
# Install SoupaWhisper on Linux (X11 or Wayland)
# Supports: Ubuntu, Pop!_OS, Debian, Fedora, Arch, openSUSE
#
#   ./install.sh              full install with a short setup guide
#   ./install.sh --configure  only re-run the setup guide (hotkey, model, ...)
#   ./install.sh --yes        full install with defaults, no questions

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/soupawhisper"
CONFIG="$CONFIG_DIR/config.ini"
SERVICE_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$DESKTOP_DIR/io.github.zudsniper.SoupaWhisper.Settings.desktop"
UDEV_RULE="/etc/udev/rules.d/80-soupawhisper-uinput.rules"
SYS_PYTHON=/usr/bin/python3

ASSUME_YES=false
CONFIGURE_ONLY=false
for arg in "$@"; do
    case $arg in
        -y|--yes) ASSUME_YES=true ;;
        -c|--configure) CONFIGURE_ONLY=true ;;
        -h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
    esac
done

if [ -t 1 ]; then
    BOLD=$'\e[1m'; DIM=$'\e[2m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RESET=$'\e[0m'
else
    BOLD=; DIM=; GREEN=; YELLOW=; RESET=
fi

step() { echo ""; echo "${BOLD}==> $*${RESET}"; }
note() { echo "${DIM}    $*${RESET}"; }
warn() { echo "${YELLOW}    $*${RESET}"; }

abort() { echo ""; echo "Aborted."; exit 1; }

# ask "Question" default -> answer in $REPLY
ask() {
    if $ASSUME_YES; then REPLY=$2; return; fi
    read -r -p "    $1 [${2}]: " REPLY || abort
    REPLY=${REPLY:-$2}
}

# confirm "Question" y|n -> exit status
confirm() {
    local hint="y/N"; [ "$2" = y ] && hint="Y/n"
    if $ASSUME_YES; then [ "$2" = y ]; return; fi
    read -r -p "    $1 [$hint]: " REPLY || abort
    REPLY=${REPLY:-$2}
    [[ $REPLY =~ ^[Yy] ]]
}

# choose "Question" default option... -> chosen option in $REPLY
choose() {
    local question=$1 default=$2; shift 2
    local i=1 opt
    echo "    $question"
    for opt in "$@"; do
        echo "      $i) $opt"
        i=$((i + 1))
    done
    while true; do
        ask "Choice" "$default"
        if [[ $REPLY =~ ^[0-9]+$ ]] && [ "$REPLY" -ge 1 ] && [ "$REPLY" -le $# ]; then
            REPLY=${!REPLY}
            return
        fi
        echo "    Enter a number from 1 to $#."
    done
}

has_nvidia() {
    command -v nvidia-smi &> /dev/null && nvidia-smi -L &> /dev/null
}

in_input_group_now() {
    id -nG | grep -qw input
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
    local pm
    pm=$(detect_package_manager)
    step "Installing system packages (needs sudo; package manager: $pm)"

    case $pm in
        apt)
            sudo apt update || warn "apt update had errors, continuing with cached package lists"
            sudo apt install -y alsa-utils libnotify-bin python3-gi python3-gi-cairo \
                gir1.2-gtk-3.0 python3-evdev curl
            ;;
        dnf)
            sudo dnf install -y alsa-utils libnotify python3-gobject gtk3 python3-evdev curl
            ;;
        pacman)
            sudo pacman -S --needed --noconfirm alsa-utils libnotify python-gobject python-cairo \
                gtk3 python-evdev curl
            ;;
        zypper)
            sudo zypper install -y alsa-utils libnotify-tools python3-gobject python3-gobject-cairo \
                typelib-1_0-Gtk-3_0 python3-evdev curl
            ;;
        *)
            warn "Unknown package manager. Please install manually:"
            warn "  alsa-utils libnotify PyGObject (GTK 3) pycairo python-evdev"
            ;;
    esac
}

# Let the user read keyboards and create a virtual keyboard
setup_input_permissions() {
    step "Setting up keyboard access (input group + /dev/uinput)"

    echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
        | sudo tee "$UDEV_RULE" > /dev/null
    echo uinput | sudo tee /etc/modules-load.d/soupawhisper-uinput.conf > /dev/null
    sudo modprobe uinput
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=uinput

    if ! id -nG "$USER" | grep -qw input; then
        sudo usermod -aG input "$USER"
        note "Added $USER to the 'input' group."
    fi
    note "Programs running as $USER can now read keyboard input and inject"
    note "keystrokes. That access is what push-to-talk and auto-typing need."
}

# Install Python dependencies
install_python() {
    step "Installing Python dependencies"

    if ! command -v uv &> /dev/null && [ -x "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
    if ! command -v uv &> /dev/null; then
        if confirm "uv (Python package manager) is not installed. Install it now?" y; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
            export PATH="$HOME/.local/bin:$PATH"
        else
            echo "uv is required: https://docs.astral.sh/uv/"
            exit 1
        fi
    fi

    # System site-packages provide gi (PyGObject) and evdev
    uv venv --allow-existing --python "$SYS_PYTHON" --system-site-packages "$SCRIPT_DIR/.venv"

    if has_nvidia; then
        note "NVIDIA GPU found: installing CUDA libraries too."
        uv pip install --python "$SCRIPT_DIR/.venv/bin/python" -r "$SCRIPT_DIR/pyproject.toml" --extra gpu
    else
        uv pip install --python "$SCRIPT_DIR/.venv/bin/python" -r "$SCRIPT_DIR/pyproject.toml"
    fi
}

# Wait for one key press on any keyboard and print its evdev name (e.g. rightctrl)
DETECT_KEY_PY='
import selectors, evdev
from evdev import ecodes
sel = selectors.DefaultSelector()
for path in evdev.list_devices():
    try:
        dev = evdev.InputDevice(path)
    except OSError:
        continue
    if ecodes.KEY_A in dev.capabilities().get(ecodes.EV_KEY, []):
        sel.register(dev, selectors.EVENT_READ)
if not sel.get_map():
    raise SystemExit(1)
while True:
    for key, _ in sel.select():
        for e in key.fileobj.read():
            if e.type == ecodes.EV_KEY and e.value == 1:
                name = ecodes.KEY.get(e.code)
                name = name[0] if isinstance(name, list) else name
                if name and name.startswith("KEY_"):
                    print(name[4:].lower())
                    raise SystemExit(0)
'

detect_key() {
    if in_input_group_now; then
        timeout 15 "$SYS_PYTHON" -c "$DETECT_KEY_PY"
    else
        # New group membership only applies after re-login; sg grants it now
        DETECT_KEY_PY="$DETECT_KEY_PY" timeout 15 sg input -c "$SYS_PYTHON -c \"\$DETECT_KEY_PY\""
    fi
}

valid_key() {
    "$SYS_PYTHON" -c "import sys; from evdev import ecodes; sys.exit(0 if 'KEY_' + sys.argv[1].upper() in ecodes.ecodes else 1)" "$1" 2> /dev/null
}

# set_option section key value: replace "key = ..." inside [section] of $CONFIG
set_option() {
    "$SYS_PYTHON" - "$CONFIG" "$1" "$2" "$3" << 'PY'
import re, sys
path, section, key, value = sys.argv[1:]
lines = open(path).read().splitlines(keepends=True)
current, done = None, False
for i, line in enumerate(lines):
    m = re.match(r"\s*\[(.+)\]", line)
    if m:
        if current == section:
            lines.insert(i, f"{key} = {value}\n")
            done = True
            break
        current = m.group(1).strip()
    elif current == section and re.match(rf"\s*{re.escape(key)}\s*=", line):
        lines[i] = f"{key} = {value}\n"
        done = True
        break
if not done:
    if current != section:
        lines.append(f"\n[{section}]\n")
    lines.append(f"{key} = {value}\n")
open(path, "w").writelines(lines)
PY
}

choose_hotkey() {
    echo ""
    echo "  ${BOLD}1. Hotkey${RESET}: hold it while you speak, release to transcribe."
    note "Pressing another key while holding it (e.g. Ctrl+C) cancels, so a"
    note "modifier like Right Ctrl keeps working for shortcuts. Fn can't be used:"
    note "on most keyboards it never reaches the computer."
    local key
    while true; do
        choose "Which key?" 1 "Right Ctrl (recommended)" "Right Alt" "F12" "Scroll Lock" "Pause" \
            "Press the key I want" "Type a key name"
        case $REPLY in
            Right\ Ctrl*) key=rightctrl ;;
            Right\ Alt) key=rightalt ;;
            F12) key=f12 ;;
            Scroll\ Lock) key=scrolllock ;;
            Pause) key=pause ;;
            Press*)
                echo "    Press the key now (15 s)..."
                if key=$(detect_key) && [ -n "$key" ]; then
                    echo "    Got: ${GREEN}$key${RESET}"
                else
                    warn "No key detected. Pick another option."
                    continue
                fi
                ;;
            Type*)
                ask "evdev key name (e.g. rightmeta, f13, capslock)" "rightctrl"
                key=$(echo "$REPLY" | tr '[:upper:]' '[:lower:]' | sed 's/^key_//')
                ;;
        esac
        if valid_key "$key"; then
            break
        fi
        warn "'$key' is not a known key name. Try again."
    done
    set_option hotkey key "$key"
}

choose_model() {
    echo ""
    echo "  ${BOLD}3. Speech model${RESET}"
    if has_nvidia; then
        note "NVIDIA GPU found: large-v3-turbo is fast and accurate on it."
        choose "Which model?" 1 \
            "large-v3-turbo on GPU (recommended)" \
            "small.en on GPU (less VRAM, English only)" \
            "small.en on CPU (no GPU use)"
    else
        note "No NVIDIA GPU found, so the model runs on the CPU."
        choose "Which model?" 2 \
            "base.en on CPU (fastest, English only)" \
            "small.en on CPU (recommended, English only)" \
            "medium.en on CPU (most accurate, slow)"
    fi
    local model=${REPLY%% *} device=cpu compute=int8
    if [[ $REPLY == *"on GPU"* ]]; then
        device=cuda
        compute=float16
    fi
    set_option whisper model "$model"
    set_option whisper device "$device"
    set_option whisper compute_type "$compute"

    if [[ $model == *.en ]]; then
        set_option whisper language en
    else
        echo ""
        echo "  ${BOLD}4. Language${RESET}"
        ask "Spoken language code (en, de, fr, ...; 'auto' to detect)" "en"
        [ "$REPLY" = auto ] && REPLY=""
        set_option whisper language "$REPLY"
    fi
}

# Print "node.name<TAB>description" for each PipeWire microphone (no monitors)
LIST_SOURCES_PY='
import json, sys
try:
    objects = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
for obj in objects:
    props = (obj.get("info") or {}).get("props") or {}
    name = props.get("node.name") or ""
    if props.get("media.class") != "Audio/Source" or not name:
        continue
    if name.endswith(".split") or ".monitor" in name or props.get("stream.monitor"):
        continue
    label = props.get("node.description") or props.get("node.nick") or name
    print(name + "\t" + label.replace("\t", " "))
'

list_sources() {
    command -v pw-dump &> /dev/null || return 0
    timeout 5 pw-dump 2> /dev/null | "$SYS_PYTHON" -c "$LIST_SOURCES_PY" 2> /dev/null || true
}

choose_microphone() {
    $ASSUME_YES && return  # keep the system default
    local names=() labels=() name label
    while IFS=$'\t' read -r name label; do
        [ -n "$name" ] || continue
        # Labels are matched back to names, so make duplicates unique
        local base=$label n=2
        while printf '%s\n' "${labels[@]}" | grep -Fxq -- "$label"; do
            label="$base ($n)"; n=$((n + 1))
        done
        names+=("$name")
        labels+=("$label")
    done < <(list_sources)
    [ ${#names[@]} -gt 0 ] || return 0  # nothing detected: keep the default

    echo ""
    echo "  ${BOLD}7. Microphone${RESET}"
    note "The system default follows your sound settings. Pick one input to pin"
    note "it, e.g. if the default is a webcam or a silent interface channel."
    choose "Which input?" 1 "System default (recommended)" "${labels[@]}"
    local i
    for i in "${!labels[@]}"; do
        if [ "$REPLY" = "${labels[$i]}" ]; then
            set_option audio device "${names[$i]}"
            return
        fi
    done
    set_option audio device ""
}

# Interactive setup guide; writes $CONFIG
setup_config() {
    step "Setup guide"
    mkdir -p "$CONFIG_DIR"

    if [ -f "$CONFIG" ]; then
        if ! confirm "A config already exists at $CONFIG. Reconfigure it?" n; then
            note "Keeping the existing config."
            return
        fi
        cp "$CONFIG" "$CONFIG.bak"
        note "Backed up the old config to $CONFIG.bak"
    fi
    cp "$SCRIPT_DIR/config.example.ini" "$CONFIG"

    choose_hotkey

    echo ""
    echo "  ${BOLD}2. Recording mode${RESET}"
    choose "How do you want to record?" 1 \
        "Hold to talk (release to transcribe)" \
        "Hold to talk, or tap once to start and tap again to stop"
    case $REPLY in
        *"tap once"*) set_option hotkey mode toggle ;;
        *) set_option hotkey mode hold ;;
    esac

    choose_model

    echo ""
    echo "  ${BOLD}5. Output${RESET}: the text always goes to the clipboard."
    if confirm "Also type it into the focused text field?" y; then
        set_option behavior auto_type true
    else
        set_option behavior auto_type false
    fi

    echo ""
    echo "  ${BOLD}6. On-screen indicator${RESET} (shows listening / transcribing)"
    choose "Where should it appear?" 1 "Bottom of the screen" "Top of the screen" "Don't show it"
    case $REPLY in
        Bottom*) set_option overlay enabled true; set_option overlay position bottom ;;
        Top*) set_option overlay enabled true; set_option overlay position top ;;
        *) set_option overlay enabled false ;;
    esac

    choose_microphone

    echo ""
    echo "    ${GREEN}Saved $CONFIG${RESET}"
    note "Change it any time in the settings window (Super+Comma),"
    note "edit the file, or re-run: ./install.sh --configure"
}

# Install systemd service
install_service() {
    step "Installing the background service"

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

    systemctl --user daemon-reload
    systemctl --user enable soupawhisper
    systemctl --user restart soupawhisper
    note "Started. It also starts automatically when you log in."
    note "The first start downloads the speech model, which can take a few minutes."
}

# App-grid entry for the settings window
install_desktop_entry() {
    local python="$SCRIPT_DIR/.venv/bin/python" settings="$SCRIPT_DIR/settings.py" entry
    if [ ! -x "$python" ]; then
        warn "No virtualenv at $SCRIPT_DIR/.venv yet; skipping the settings app entry."
        return 0
    fi
    case $SCRIPT_DIR in
        *[\"\`\$\\]*)
            warn "The install path contains \", \`, \$ or \\, which a desktop entry can't quote; skipping it."
            return 0 ;;
    esac
    mkdir -p "$DESKTOP_DIR"
    # Quote both paths for the Exec line ('%' is a field code there, so double it);
    # the quoted replacement keeps '&' literal
    local exec_python="\"${python//%/%%}\"" exec_settings="\"${settings//%/%%}\""
    entry=$(< "$SCRIPT_DIR/io.github.zudsniper.SoupaWhisper.Settings.desktop")
    entry=${entry//@PYTHON@/"$exec_python"}
    entry=${entry//@SETTINGS@/"$exec_settings"}
    printf '%s\n' "$entry" > "$DESKTOP_FILE"
    if command -v update-desktop-database &> /dev/null; then
        update-desktop-database "$DESKTOP_DIR" &> /dev/null || true
    fi
    note "Added 'SoupaWhisper Settings' to the app grid ($DESKTOP_FILE)."
}

restart_if_running() {
    if systemctl --user is-active --quiet soupawhisper 2> /dev/null; then
        systemctl --user restart soupawhisper
        note "Restarted the service with the new settings."
    fi
}

main() {
    echo "${BOLD}==================================="
    echo "  SoupaWhisper Installer"
    echo "===================================${RESET}"

    if $CONFIGURE_ONLY; then
        setup_config
        install_desktop_entry
        restart_if_running
        exit 0
    fi

    install_deps
    setup_input_permissions
    install_python
    setup_config
    install_desktop_entry

    local service=false
    step "Background service"
    note "Runs SoupaWhisper in the background and starts it at login."
    if confirm "Install and start it now?" y; then
        install_service
        service=true
    fi

    echo ""
    echo "${BOLD}==================================="
    echo "  Installation complete!"
    echo "===================================${RESET}"
    echo ""
    echo "  Hotkey: hold ${BOLD}$(sed -n 's/^key = //p' "$CONFIG" | head -1)${RESET}, speak, release."
    if $service; then
        echo "  Logs:   journalctl --user -u soupawhisper -f"
    else
        echo "  Run:    $SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/dictate.py"
    fi
    echo "  Config: $CONFIG (or ./install.sh --configure)"
    echo "  Open settings: Super+Comma, or search 'SoupaWhisper' in Activities"
    echo ""
    echo "  Log out and back in once so the 'input' group applies everywhere."
}

main "$@"
