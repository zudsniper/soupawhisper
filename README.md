# SoupaWhisper

A simple push-to-talk voice dictation tool for Linux using faster-whisper. Hold a key to record, release to transcribe, and it automatically copies to clipboard and types into the active input.

<p align="center"><img src="docs/overlay-flow.gif" alt="Overlay: listening with a live mic meter, transcribing, then the result" width="520"></p>

This fork adds:
- **Wayland support** (tested on GNOME 49): the hotkey is read with evdev and text is typed through a uinput virtual keyboard, so it works in every app on X11 and Wayland, terminals included.
- **On-screen overlay**: a small pill at the bottom of the screen that pulses with a live mic meter while you hold the key, spins while transcribing, then flashes the result. It never takes focus away from the field you're typing in.
- **Hold-only Right Ctrl** by default, with a chord guard: Right Ctrl+C and other shortcuts don't trigger dictation, and quick taps are ignored.
- GPU by default (`large-v3-turbo`) using pip-installed CUDA/cuDNN wheels, no system cuDNN needed. Falls back to `small.en` on CPU.

### Overlay states

| State | Looks like | When |
|-------|-----------|------|
| Listening | <img src="docs/overlay-listening.gif" width="300" alt="Listening"> | While the hotkey is held (after `min_hold`). The dot and bars follow your mic level. |
| Transcribing | <img src="docs/overlay-transcribing.gif" width="300" alt="Transcribing"> | After release, while Whisper runs. |
| Done | <img src="docs/overlay-done.gif" width="300" alt="Done"> | Text copied and typed; shows the start of it, then fades. |
| No speech | <img src="docs/overlay-nospeech.png" width="300" alt="No speech detected"> | Recording was silent or too short. |
| Error | <img src="docs/overlay-error.png" width="300" alt="Error"> | Something failed (details in the service log). |

Try the overlay without a microphone: `.venv/bin/python dictate.py --demo-overlay`. Disable it with `enabled = false` under `[overlay]`. The images above are rendered from the real drawing code by [`docs/render_overlay.py`](docs/render_overlay.py).

## Requirements

- Python 3.10+ (the system `/usr/bin/python3`, for PyGObject and evdev)
- [uv](https://docs.astral.sh/uv/)
- Linux with X11 or Wayland (ALSA/PipeWire audio)
- Membership in the `input` group, plus `/dev/uinput` access (the installer sets both up)
  - Note: this lets any program running as you read keyboard input and inject keystrokes. Wayland offers no portable alternative for global hotkeys + typing.

## Supported Distros

- Ubuntu / Pop!_OS / Debian (apt)
- Fedora (dnf)
- Arch Linux (pacman)
- openSUSE (zypper)

## Installation

```bash
git clone https://github.com/zudsniper/soupawhisper.git
cd soupawhisper
chmod +x install.sh
./install.sh
```

The installer will:
1. Detect your package manager
2. Install system dependencies
3. Add a udev rule for `/dev/uinput` and add you to the `input` group (**log out and back in afterwards**)
4. Create `.venv` with uv and install Python dependencies (CUDA wheels if an NVIDIA GPU is found)
5. Set up the config file
6. Optionally install as a systemd service

### Manual Installation

```bash
# Ubuntu/Debian
sudo apt install alsa-utils libnotify-bin python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-evdev

# Python deps (drop --extra gpu on machines without an NVIDIA GPU)
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python -r pyproject.toml --extra gpu
```

### GPU Support

With `--extra gpu`, `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` are installed into the venv and `dictate.py` points the loader at them on startup. Without a GPU, set `device = cpu` and `compute_type = int8` in the config.

## Usage

```bash
.venv/bin/python dictate.py
```

- Hold **Right Ctrl** to record (the overlay shows *Listening* with a level meter)
- Release to transcribe (*Transcribing* spinner) → copies to clipboard and types into active input
- Pressing another key while holding (e.g. Ctrl+C), or a tap shorter than `min_hold`, cancels
- Prefer hands-free? Set `mode = toggle` under `[hotkey]`: a quick tap starts recording, another tap stops it (holding still works)
- Press **Ctrl+C** to quit (when running manually)

## Run as a systemd Service

The installer can set this up automatically. If you skipped it, run:

```bash
./install.sh  # Select 'y' when prompted for systemd
```

### Service Commands

```bash
systemctl --user start soupawhisper     # Start
systemctl --user stop soupawhisper      # Stop
systemctl --user restart soupawhisper   # Restart
systemctl --user status soupawhisper    # Status
journalctl --user -u soupawhisper -f    # View logs
```

## Configuration

Edit `~/.config/soupawhisper/config.ini`. See [`config.example.ini`](config.example.ini) for every option: model, device, language, hotkey, `min_hold`, auto-type, overlay position.

Create the config directory and file if it doesn't exist:
```bash
mkdir -p ~/.config/soupawhisper
cp /path/to/soupawhisper/config.example.ini ~/.config/soupawhisper/config.ini
```

## Troubleshooting

**No audio recording:**
```bash
# Check your input device
arecord -l

# Test recording
arecord -d 3 test.wav && aplay test.wav
```

**Hotkey does nothing / "No readable keyboard found":**
```bash
id -nG | grep -w input   # must list 'input'; log out and in after install.sh
```

**Auto-type disabled, cannot open /dev/uinput:**
```bash
ls -l /dev/uinput        # should be group 'input', mode crw-rw----
```

**"fn" can't be used as the hotkey:** on most keyboards fn is handled by the keyboard firmware and never reaches the OS.

**Typed characters are wrong:** auto-type assumes a US keyboard layout. The clipboard always has the exact text.

**cuDNN errors with GPU:**
```
Unable to load any of {libcudnn_ops.so.9...}
```
Install the venv with `--extra gpu` (see GPU Support section above) or switch to CPU mode.

## Model Sizes

| Model | Size | Speed | Accuracy |
|-------|------|-------|----------|
| tiny.en | ~75MB | Fastest | Basic |
| base.en | ~150MB | Fast | Good |
| small.en | ~500MB | Medium | Better |
| medium.en | ~1.5GB | Slower | Great |
| large-v3 | ~3GB | Slowest | Best |
| large-v3-turbo | ~1.6GB | Fast on GPU | Near large-v3 |

On CPU, `base.en` or `small.en` is usually the sweet spot. With an NVIDIA GPU, `large-v3-turbo` (the default) transcribes a few seconds of speech in well under a second.
