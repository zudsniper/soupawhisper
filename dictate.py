#!/usr/bin/env python3
"""
SoupaWhisper - Voice dictation tool using faster-whisper.
Hold the hotkey to record, release to transcribe, copy to clipboard and type it.
Works on X11 and Wayland (hotkey and typing go through evdev/uinput).
"""

import argparse
import configparser
import importlib.util
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

import numpy as np

from overlay import Overlay, set_clipboard  # sets GDK_BACKEND before gi loads
from gi.repository import GLib, Gtk  # noqa: E402

import inputdev  # noqa: E402

__version__ = "0.2.0"

# Load configuration
CONFIG_PATH = Path.home() / ".config" / "soupawhisper" / "config.ini"
SAMPLE_RATE = 16000
MAX_RECORD_SECONDS = 120  # safety net if a key-up is ever lost
FALLBACK_MODEL = ("small.en", "cpu", "int8")


def load_config():
    config = configparser.ConfigParser()

    if CONFIG_PATH.exists():
        config.read(CONFIG_PATH)

    return {
        "model": config.get("whisper", "model", fallback="large-v3-turbo"),
        "device": config.get("whisper", "device", fallback="cuda"),
        "compute_type": config.get("whisper", "compute_type", fallback="float16"),
        "language": config.get("whisper", "language", fallback="en").strip() or None,
        "beam_size": config.getint("whisper", "beam_size", fallback=5),
        "key": config.get("hotkey", "key", fallback="rightctrl"),
        "min_hold": config.getfloat("hotkey", "min_hold", fallback=0.25),
        "mode": config.get("hotkey", "mode", fallback="hold").strip().lower(),
        "long_press": config.getfloat("behavior", "long_press", fallback=0.4),
        "auto_type": config.getboolean("behavior", "auto_type", fallback=True),
        "trailing_space": config.getboolean("behavior", "trailing_space", fallback=True),
        "type_delay_ms": config.getfloat("behavior", "type_delay_ms", fallback=4),
        "notifications": config.getboolean("behavior", "notifications", fallback=False),
        "overlay": config.getboolean("overlay", "enabled", fallback=True),
        "overlay_position": config.get("overlay", "position", fallback="bottom"),
        "overlay_offset": config.getint("overlay", "offset", fallback=72),
    }


CONFIG = load_config()


def ensure_cuda_libs():
    """
    Point the dynamic loader at the pip-installed CUDA wheels (nvidia-cublas-cu12,
    nvidia-cudnn-cu12) so no system cuDNN is needed. LD_LIBRARY_PATH is only read
    at process start, so re-exec once with it set.
    """
    if CONFIG["device"] != "cuda" or os.environ.get("SOUPAWHISPER_CUDA_LIBS"):
        return
    lib_dirs = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            spec = None
        if spec and spec.submodule_search_locations:
            lib_dir = os.path.join(list(spec.submodule_search_locations)[0], "lib")
            if os.path.isdir(lib_dir):
                lib_dirs.append(lib_dir)
    os.environ["SOUPAWHISPER_CUDA_LIBS"] = "1"
    if not lib_dirs:
        return
    existing = os.environ.get("LD_LIBRARY_PATH")
    os.environ["LD_LIBRARY_PATH"] = ":".join(lib_dirs + ([existing] if existing else []))
    os.execv(sys.executable, sys.orig_argv)  # orig_argv keeps flags like -u


def notify(title, message, icon="dialog-information", timeout=2000):
    """Send a desktop notification."""
    if not CONFIG["notifications"]:
        return
    try:
        _notify_send(title, message, icon, timeout)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"notify-send failed: {e}")


def _notify_send(title, message, icon, timeout):
    subprocess.run(
        [
            "notify-send",
            "-a", "SoupaWhisper",
            "-i", icon,
            "-t", str(timeout),
            "-h", "string:x-canonical-private-synchronous:soupawhisper",
            title,
            message
        ],
        capture_output=True,
        timeout=2,
    )


def load_model(name, device, compute_type):
    """Load a model and run one dummy pass so CUDA/cuDNN problems surface now."""
    from faster_whisper import WhisperModel

    model = WhisperModel(name, device=device, compute_type=compute_type)
    segments, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")
    list(segments)
    return model


def normalize(audio, target=0.5, max_gain=20.0):
    """Boost quiet mics (e.g. dynamic mics at low gain) so the VAD doesn't drop speech."""
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= 0.0:
        return audio
    return audio * min(max_gain, max(1.0, target / peak))


def transcribe(model, audio):
    audio = normalize(audio)
    segments, _ = model.transcribe(
        audio,
        beam_size=CONFIG["beam_size"],
        language=CONFIG["language"],
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments).strip()


class Recorder:
    """Streams raw 16 kHz mono PCM from arecord and reports the input level."""

    CHUNK_BYTES = 2048  # 64 ms

    def __init__(self, on_level):
        self.on_level = on_level
        self.process = None
        self.thread = None
        self.chunks = []

    def start(self):
        self.chunks = []
        # arecord talks to PipeWire/Pulse through the ALSA plugin
        self.process = subprocess.Popen(
            ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.thread = threading.Thread(
            target=self._read, args=(self.process, self.chunks), daemon=True
        )
        self.thread.start()

    def _read(self, process, chunks):
        while True:
            data = process.stdout.read(self.CHUNK_BYTES)
            if not data:
                break
            chunks.append(data)
            samples = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)
            if samples.size:
                rms = math.sqrt(float(np.mean(samples.astype(np.float32) ** 2))) / 32768
                db = 20 * math.log10(rms + 1e-7)
                self.on_level(min(1.0, max(0.0, (db + 55) / 40)))

    def stop(self, tail=0.0):
        """Stop recording and return float32 audio. `tail` keeps the last word."""
        if self.process is None:
            return np.zeros(0, dtype=np.float32)
        if tail:
            time.sleep(tail)
        process, self.process = self.process, None
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self.thread.join(timeout=1)
        process.stdout.close()
        raw = b"".join(self.chunks)
        raw = raw[: len(raw) // 2 * 2]
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"  # hotkey held
    TOGGLED = "toggled"      # toggle mode: tapped, recording hands-free
    STOPPING = "stopping"    # toggle mode: pressed again, stops on release
    BUSY = "busy"


class Dictation:
    def __init__(self, overlay, keyboard):
        self.overlay = overlay
        self.keyboard = keyboard
        self.recorder = Recorder(overlay.set_level)
        self.model = None
        self.model_loaded = threading.Event()
        self.model_error = None
        self.lock = threading.Lock()
        self.state = State.IDLE
        self.press_time = 0.0
        self.show_timer = None
        self.watchdog = None

        # Load model in background
        threading.Thread(target=self._load_model, daemon=True).start()

    def _load_model(self):
        name, device, compute = CONFIG["model"], CONFIG["device"], CONFIG["compute_type"]
        print(f"Loading Whisper model ({name} on {device}/{compute})...")
        try:
            self.model = load_model(name, device, compute)
        except Exception as e:
            print(f"Failed to load model: {e}")
            if device == "cpu":
                self.model_error = str(e)
                self.model_loaded.set()
                return
            print(f"Falling back to {FALLBACK_MODEL[0]} on CPU...")
            try:
                self.model = load_model(*FALLBACK_MODEL)
            except Exception as e2:
                print(f"Fallback failed too: {e2}")
                self.model_error = str(e2)
                self.model_loaded.set()
                return
        self.model_loaded.set()
        print("Model loaded. Ready for dictation!")
        print(f"Hold [{CONFIG['key']}] to record, release to transcribe.")
        if CONFIG["mode"] == "toggle":
            print(f"Or tap [{CONFIG['key']}] to start, tap again to stop.")

    # ---- hotkey callbacks (listener thread) ------------------------------

    def on_press(self):
        with self.lock:
            if self.state is State.TOGGLED:
                self.state = State.STOPPING
                return
            if self.state is not State.IDLE:
                return
            if self.model_error:
                self.overlay.set_state("error", "model failed to load")
                return
            try:
                self.recorder.start()  # start now so the first word isn't clipped
            except OSError as e:
                print(f"Cannot start arecord: {e}")
                self.overlay.set_state("error", "cannot open microphone")
                return
            self.state = State.RECORDING
            self.press_time = time.monotonic()
            # Only show the overlay once it's clearly a hold, not a tap or Ctrl+X
            self.show_timer = threading.Timer(CONFIG["min_hold"], self._show_listening)
            self.show_timer.start()
            self.watchdog = threading.Timer(MAX_RECORD_SECONDS, self._timeout)
            self.watchdog.start()

    def _show_listening(self):
        with self.lock:
            if self.state not in (State.RECORDING, State.TOGGLED):
                return
            how = "Release" if self.state is State.RECORDING else "Tap"
            print("Recording...")
            self.overlay.set_state("listening")
        notify("Recording...", f"{how} {CONFIG['key']} when done",
               "audio-input-microphone", 30000)

    def _cancel_timers(self):
        for timer in (self.show_timer, self.watchdog):
            if timer is not None:
                timer.cancel()

    def on_chord(self):
        with self.lock:
            if self.state is State.RECORDING:
                self._cancel()
            elif self.state is State.STOPPING:
                self.state = State.TOGGLED  # it was a shortcut, keep recording

    def _timeout(self):
        with self.lock:
            if self.state in (State.RECORDING, State.TOGGLED, State.STOPPING):
                print(f"Recording passed {MAX_RECORD_SECONDS}s, discarding")
                self._cancel()

    def on_release(self):
        with self.lock:
            if self.state is State.RECORDING:
                held = time.monotonic() - self.press_time
                if CONFIG["mode"] == "toggle" and held < CONFIG["long_press"]:
                    self.state = State.TOGGLED  # a tap: keep recording until the next one
                    self.show_timer.cancel()
                elif held < CONFIG["min_hold"]:
                    self._cancel()
                    return
            elif self.state is not State.STOPPING:
                return
            toggled = self.state is State.TOGGLED
            if not toggled:
                self._cancel_timers()
                self.state = State.BUSY
        if toggled:
            self._show_listening()
            return
        print("Transcribing...")
        self.overlay.set_state("transcribing")
        threading.Thread(target=self._finish, daemon=True).start()
        notify("Transcribing...", "Processing your speech", "emblem-synchronizing", 30000)

    def _cancel(self):
        """Discard the current recording. Caller holds self.lock."""
        self._cancel_timers()
        self.recorder.stop()
        self.state = State.IDLE
        self.overlay.set_state("hidden")

    # ---- transcription (worker thread) -----------------------------------

    def _finish(self):
        try:
            audio = self.recorder.stop(tail=0.15)
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            print(f"Captured {audio.size / SAMPLE_RATE:.1f}s, peak {20 * math.log10(peak + 1e-7):.0f} dBFS")

            # Wait for model if not loaded yet
            self.model_loaded.wait()
            if self.model_error:
                print("Cannot transcribe: model failed to load")
                self.overlay.set_state("error", "model failed to load")
                notify("Error", "Model failed to load", "dialog-error", 3000)
                return

            text = transcribe(self.model, audio) if audio.size > SAMPLE_RATE * 0.3 else ""
            if text:
                self._deliver(text)
            else:
                print("No speech detected")
                self.overlay.set_state("nospeech")
                notify("No speech detected", "Try speaking louder", "dialog-warning", 2000)
        except Exception as e:
            print(f"Error: {e}")
            self.overlay.set_state("error", str(e))
            notify("Error", str(e)[:50], "dialog-error", 3000)
        finally:
            with self.lock:
                self.state = State.IDLE

    def _deliver(self, text):
        set_clipboard(text)
        # Type it into the active input field
        if self.keyboard:
            self.keyboard.type(text + (" " if CONFIG["trailing_space"] else ""))
        print(f"Copied {len(text)} characters")  # never log the dictated text itself
        self.overlay.set_state("done", text)
        notify("Copied!", text[:100] + ("..." if len(text) > 100 else ""), "emblem-ok-symbolic", 3000)


def check_dependencies():
    """Check that required system commands are available."""
    if subprocess.run(["which", "arecord"], capture_output=True).returncode != 0:
        print("Missing dependency: arecord - install with: sudo apt install alsa-utils")
        sys.exit(1)


def run_test_file(path):
    """Transcribe an audio file and print the result (no GUI, no hotkey)."""
    name, device, compute = CONFIG["model"], CONFIG["device"], CONFIG["compute_type"]
    start = time.monotonic()
    try:
        model = load_model(name, device, compute)
    except Exception as e:
        print(f"{device} load failed ({e}); using {FALLBACK_MODEL}")
        model = load_model(*FALLBACK_MODEL)
    loaded = time.monotonic()
    from faster_whisper.audio import decode_audio

    text = transcribe(model, decode_audio(path, sampling_rate=SAMPLE_RATE))
    print(f"load {loaded - start:.1f}s, transcribe {time.monotonic() - loaded:.2f}s")
    print(text)


def run_demo(overlay):
    """Cycle through every overlay state, then quit."""
    def script():
        time.sleep(0.5)
        overlay.set_state("listening")
        start = time.monotonic()
        while time.monotonic() - start < 3:
            overlay.set_level(abs(math.sin(time.monotonic() * 3)) * random.uniform(0.5, 1.0))
            time.sleep(0.05)
        overlay.set_state("transcribing")
        time.sleep(2.0)
        overlay.set_state("done", "This is what a finished transcription looks like on screen.")
        time.sleep(2.5)
        overlay.set_state("nospeech")
        time.sleep(1.8)
        overlay.set_state("error", "model failed to load")
        time.sleep(3.8)
        GLib.idle_add(Gtk.main_quit)
    threading.Thread(target=script, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(
        description="SoupaWhisper - Push-to-talk voice dictation"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"SoupaWhisper {__version__}"
    )
    parser.add_argument("--test-file", metavar="AUDIO", help="transcribe a file and exit")
    parser.add_argument("--demo-overlay", action="store_true", help="show every overlay state and exit")
    args = parser.parse_args()

    ensure_cuda_libs()

    if args.test_file:
        run_test_file(args.test_file)
        return

    print(f"SoupaWhisper v{__version__}")
    print(f"Config: {CONFIG_PATH}")

    overlay = Overlay(
        enabled=CONFIG["overlay"] or args.demo_overlay,
        position=CONFIG["overlay_position"],
        offset=CONFIG["overlay_offset"],
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, Gtk.main_quit)

    if args.demo_overlay:
        run_demo(overlay)
        Gtk.main()
        return

    check_dependencies()
    hotkey = inputdev.resolve_key(CONFIG["key"])  # fail fast on a bad key name
    if CONFIG["mode"] not in ("hold", "toggle"):
        sys.exit(f"Unknown hotkey mode: {CONFIG['mode']} (use hold or toggle)")

    keyboard = None
    if CONFIG["auto_type"]:
        try:
            keyboard = inputdev.VirtualKeyboard(CONFIG["type_delay_ms"])
        except OSError as e:
            print(f"Auto-type disabled, cannot open /dev/uinput ({e}).")
            print("Run install.sh (adds a udev rule and the 'input' group), then log in again.")

    dictation = Dictation(overlay, keyboard)
    inputdev.HotkeyListener(
        hotkey,
        on_press=dictation.on_press,
        on_release=dictation.on_release,
        on_chord=dictation.on_chord,
    ).start()

    Gtk.main()
    print("\nExiting...")
    if keyboard:
        keyboard.close()


if __name__ == "__main__":
    main()
