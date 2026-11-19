"""
Non-GUI backend for the SoupaWhisper settings window.

Reads/writes config.ini without losing comments, discovers PipeWire/Pulse
microphones, measures input levels and reports system readiness. No GTK here.
"""

from __future__ import annotations

import configparser
import grp
import json
import math
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

REPO_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path.home() / ".config" / "soupawhisper" / "config.ini"
EXAMPLE_PATH = REPO_DIR / "config.example.ini"
SERVICE = "soupawhisper"

# section -> key -> default; mirrors dictate.load_config fallbacks
SCHEMA: dict[str, dict[str, str]] = {
    "whisper": {
        "model": "large-v3-turbo",
        "device": "cuda",
        "compute_type": "float16",
        "language": "en",
        "beam_size": "5",
    },
    "hotkey": {
        "key": "rightctrl",
        "min_hold": "0.25",
        "mode": "hold",
        "settings_key": "super+comma",
    },
    "behavior": {
        "auto_type": "true",
        "trailing_space": "true",
        "type_delay_ms": "4",
        "notifications": "false",
        "long_press": "0.4",
    },
    "overlay": {
        "enabled": "true",
        "position": "bottom",
        "offset": "72",
    },
    "audio": {
        "device": "",
    },
}

MODELS: list[str] = [
    "tiny.en", "base.en", "small.en", "medium.en",
    "small", "medium", "large-v3", "large-v3-turbo",
]

SAMPLE_RATE = 16000
CHUNK_BYTES = 2048  # 64 ms of S16_LE mono
SILENCE_DBFS = -120.0
CMD_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# Subprocess helper
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = CMD_TIMEOUT) -> subprocess.CompletedProcess | None:
    """Run a command capturing text output; None if it can't run or times out."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------
# Config read/write
# --------------------------------------------------------------------------

def read_config(path: Path = CONFIG_PATH) -> dict[str, dict[str, str]]:
    """Return every SCHEMA key (defaults filled) plus any extra keys in the file."""
    values = {section: dict(keys) for section, keys in SCHEMA.items()}
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return values
    for section in parser.sections():
        target = values.setdefault(section, {})
        for key, value in parser.items(section):
            target[key] = value.strip()
    return values


_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^\s*([^\s#;=:\[][^=:]*?)\s*[=:](.*)$")


def _parse_key(line: str) -> tuple[str, str] | None:
    """(lowercase key, stripped value) for a `key = value` line, else None."""
    match = _KEY_RE.match(line)
    if not match:
        return None
    return match.group(1).strip().lower(), match.group(2).strip()


def _format_line(key: str, value: str) -> str:
    value = value.strip()
    return f"{key} = {value}" if value else f"{key} ="


def _section_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map section name -> (header index, end index exclusive). First occurrence wins."""
    spans: dict[str, tuple[int, int]] = {}
    current: str | None = None
    start = 0
    for i, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match:
            continue
        if current is not None and current not in spans:
            spans[current] = (start, i)
        current, start = match.group(1).strip(), i
    if current is not None and current not in spans:
        spans[current] = (start, len(lines))
    return spans


def _update_section(lines: list[str], start: int, end: int, wanted: dict[str, str]) -> list[str]:
    """Return the rewritten section (lines[start:end], header included)."""
    body = lines[start:end]
    seen: set[str] = set()
    for i, line in enumerate(body):
        parsed = _parse_key(line)
        if parsed is None or parsed[0] not in wanted:
            continue
        key, current = parsed
        seen.add(key)
        if current != wanted[key].strip():
            body[i] = _format_line(key, wanted[key])
    missing = [_format_line(k, v) for k, v in wanted.items() if k not in seen]
    if missing:
        insert_at = len(body)
        while insert_at > 1 and not body[insert_at - 1].strip():
            insert_at -= 1
        body[insert_at:insert_at] = missing
    return body


def apply_values(text: str, values: dict[str, dict[str, str]]) -> str:
    """Return `text` with `values` applied, preserving everything else."""
    lines = text.splitlines()
    for section, keys in values.items():
        wanted = {k.lower(): str(v) for k, v in keys.items()}
        spans = _section_spans(lines)
        if section in spans:
            start, end = spans[section]
            lines[start:end] = _update_section(lines, start, end, wanted)
        elif wanted:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{section}]")
            lines.extend(_format_line(k, v) for k, v in wanted.items())
    return "\n".join(lines) + "\n" if lines else ""


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _atomic_write(path: Path, text: str, mode: int) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_config(values: dict[str, dict[str, str]], path: Path = CONFIG_PATH) -> None:
    """Apply changed values in place; back up the old file to `<path>.bak`."""
    path = Path(path).resolve()  # keep a symlinked (dotfiles) config a symlink
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_text(path) if path.exists() else None
    base = existing if existing is not None else (_read_text(EXAMPLE_PATH) or "")
    new_text = apply_values(base, values)
    if existing is not None and new_text == existing:
        return
    mode = 0o644
    if existing is not None:
        Path(str(path) + ".bak").write_text(existing, encoding="utf-8")
        mode = path.stat().st_mode & 0o777
    _atomic_write(path, new_text, mode)


# --------------------------------------------------------------------------
# Audio sources
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AudioSource:
    name: str
    description: str
    is_default: bool


def _is_internal_name(name: str) -> bool:
    return not name or name.endswith(".split") or name.endswith(".monitor")


def _pw_default_name(objects: list[dict]) -> str | None:
    """default.audio.source from the 'default' metadata (configured one as fallback)."""
    found: dict[str, str] = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if (obj.get("props") or {}).get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or []:
            value = entry.get("value")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = {"name": value}
            if isinstance(value, dict) and value.get("name"):
                found[entry.get("key", "")] = value["name"]
    return found.get("default.audio.source") or found.get("default.configured.audio.source")


def parse_pw_dump(objects: list[dict]) -> list[AudioSource]:
    """Extract real capture sources from parsed `pw-dump` output."""
    default = _pw_default_name(objects)
    sources: list[AudioSource] = []
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name", "")
        if props.get("media.class") != "Audio/Source" or _is_internal_name(name):
            continue
        if props.get("stream.monitor") in (True, "true"):
            continue
        label = props.get("node.description") or props.get("node.nick") or name
        sources.append(AudioSource(name, label, name == default))
    return sources


def parse_pactl(entries: list[dict], default: str | None) -> list[AudioSource]:
    """Extract capture sources from `pactl -f json list sources`."""
    sources: list[AudioSource] = []
    for entry in entries:
        name = entry.get("name", "")
        if _is_internal_name(name) or entry.get("monitor_of_sink") not in (None, "", "n/a"):
            continue
        label = entry.get("description") or name
        sources.append(AudioSource(name, label, name == default))
    return sources


def _json_output(cmd: list[str]) -> list | None:
    result = _run(cmd)
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _list_pipewire() -> list[AudioSource] | None:
    if not shutil.which("pw-dump"):
        return None
    data = _json_output(["pw-dump"])
    return None if data is None else parse_pw_dump(data)


def _list_pactl() -> list[AudioSource] | None:
    if not shutil.which("pactl"):
        return None
    data = _json_output(["pactl", "-f", "json", "list", "sources"])
    if data is None:
        return None
    result = _run(["pactl", "get-default-source"])
    default = result.stdout.strip() if result and result.returncode == 0 else None
    return parse_pactl(data, default)


def list_sources() -> list[AudioSource]:
    """Capture sources via pw-dump, then pactl, else []. Never raises."""
    for lister in (_list_pipewire, _list_pactl):
        try:
            sources = lister()
        except Exception:  # noqa: BLE001 - must never raise to the UI
            sources = None
        if sources:
            return sources
    return []


def default_source() -> AudioSource | None:
    return next((s for s in list_sources() if s.is_default), None)


# --------------------------------------------------------------------------
# Level metering
# --------------------------------------------------------------------------

def chunk_dbfs(data: bytes) -> float | None:
    """RMS level of S16_LE samples in dBFS; None for an empty chunk."""
    samples = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16)
    if not samples.size:
        return None
    rms = math.sqrt(float(np.mean(samples.astype(np.float32) ** 2))) / 32768
    return max(SILENCE_DBFS, 20 * math.log10(rms + 1e-7))


def normalize_level(db: float) -> float:
    """Same mapping as dictate.py's overlay meter."""
    return min(1.0, max(0.0, (db + 55) / 40))


def _arecord_env(source_name: str | None) -> dict[str, str]:
    env = os.environ.copy()
    if source_name:
        env["PIPEWIRE_NODE"] = source_name
        env["PULSE_SOURCE"] = source_name
    return env


class LevelMonitor:
    """Streams arecord from one source and reports (normalized, dBFS) per chunk."""

    def __init__(self, on_level: Callable[[float, float], None]):
        self.on_level = on_level
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._peak = SILENCE_DBFS

    @property
    def peak_dbfs(self) -> float:
        return self._peak

    def start(self, source_name: str | None) -> None:
        """Start (or restart) metering `source_name`; None = system default."""
        self.stop()
        self._peak = SILENCE_DBFS
        try:
            process = subprocess.Popen(
                ["arecord", "-q", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_arecord_env(source_name),
            )
        except OSError:
            return
        thread = threading.Thread(target=self._read, args=(process,), daemon=True)
        with self._lock:
            self._process, self._thread = process, thread
        thread.start()

    def _read(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        while stream is not None:
            try:
                data = stream.read(CHUNK_BYTES)
            except (OSError, ValueError):
                break
            if not data:
                break
            db = chunk_dbfs(data)
            if db is None:
                continue
            self._peak = max(self._peak, db)
            try:
                self.on_level(normalize_level(db), db)
            except Exception:  # noqa: BLE001 - a UI callback must not kill the reader
                pass

    def stop(self) -> None:
        """Stop arecord and reap it. Safe to call repeatedly."""
        with self._lock:
            process, thread = self._process, self._thread
            self._process = self._thread = None
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if process.stdout is not None:
            process.stdout.close()


def measure_sources(
    sources: list[AudioSource], seconds: float = 3.0
) -> list[tuple[AudioSource, float]]:
    """Record all sources at once for `seconds`; (source, peak dBFS), loudest first."""
    monitors = [(source, LevelMonitor(lambda _n, _d: None)) for source in sources]
    for source, monitor in monitors:
        monitor.start(source.name)
    try:
        time.sleep(max(0.0, seconds))
    finally:
        for _, monitor in monitors:
            monitor.stop()
    results = [(source, monitor.peak_dbfs) for source, monitor in monitors]
    return sorted(results, key=lambda item: item[1], reverse=True)


def suggest_source(results: Iterable[tuple[AudioSource, float]]) -> AudioSource | None:
    """Loudest source if clearly active (> -45 dBFS and >= 10 dB above the next)."""
    ranked = sorted(results, key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] <= -45:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 10:
        return None
    return ranked[0][0]


# --------------------------------------------------------------------------
# System status and service control
# --------------------------------------------------------------------------

@dataclass
class SystemStatus:
    gpu: str | None
    session_type: str
    in_input_group: bool
    input_group_pending: bool
    uinput_writable: bool
    service_state: str
    default_source: AudioSource | None


def detect_gpu() -> str | None:
    if not shutil.which("nvidia-smi"):
        return None
    result = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if result is None or result.returncode != 0:
        return None
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return names[0] if names else None


def _input_gid() -> int | None:
    try:
        return grp.getgrnam("input").gr_gid
    except KeyError:
        return None


def _user_listed_in_group(gid: int) -> bool:
    try:
        user = pwd.getpwuid(os.getuid())
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    return user.pw_gid == gid or user.pw_name in group.gr_mem


def input_group_state() -> tuple[bool, bool]:
    """(process has the input group, user is in it but must re-login)."""
    gid = _input_gid()
    if gid is None:
        return False, False
    active = gid in os.getgroups() or gid == os.getgid()
    return active, (not active and _user_listed_in_group(gid))


def _service_installed() -> bool:
    result = _run(["systemctl", "--user", "show", "-p", "LoadState", "--value", SERVICE])
    if result is None or result.returncode != 0:
        return False
    return result.stdout.strip() not in ("", "not-found")


def service_state() -> str:
    if not shutil.which("systemctl") or not _service_installed():
        return "not installed"
    result = _run(["systemctl", "--user", "is-active", SERVICE])
    return (result.stdout.strip() if result else "") or "unknown"


def system_status() -> SystemStatus:
    in_group, pending = input_group_state()
    return SystemStatus(
        gpu=detect_gpu(),
        session_type=os.environ.get("XDG_SESSION_TYPE") or "unknown",
        in_input_group=in_group,
        input_group_pending=pending,
        uinput_writable=os.access("/dev/uinput", os.W_OK),
        service_state=service_state(),
        default_source=default_source(),
    )


def restart_service() -> tuple[bool, str]:
    """Restart the user service; (ok, human-readable message)."""
    if not shutil.which("systemctl") or not _service_installed():
        return False, "The soupawhisper service is not installed. Run ./install.sh to set it up."
    result = _run(["systemctl", "--user", "restart", SERVICE], timeout=20.0)
    if result is None:
        return False, "Restarting the service timed out or systemctl could not run."
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
        return False, f"Service restart failed: {detail}"
    return True, "SoupaWhisper restarted with the new settings."


# --------------------------------------------------------------------------
# Keys and shortcuts
# --------------------------------------------------------------------------

def key_name_from_evdev(code: int) -> str | None:
    """Config-style key name for an evdev keycode, e.g. 97 -> 'rightctrl'."""
    try:
        from evdev import ecodes
    except ImportError:
        return None
    names = ecodes.KEY.get(code)
    if isinstance(names, (list, tuple)):
        names = names[0] if names else None
    if not isinstance(names, str) or not names.startswith("KEY_"):
        return None
    return names[4:].lower()


def is_valid_key(name: str) -> bool:
    """True if inputdev.resolve_key accepts `name`."""
    if not name or not name.strip():
        return False
    try:
        from inputdev import resolve_key

        resolve_key(name)
    except Exception:  # noqa: BLE001 - ValueError, or evdev/inputdev unavailable
        return False
    return True


def parse_combo(text: str) -> tuple[frozenset[str], str] | None:
    """'super+comma' -> (frozenset({'super'}), 'comma'); None if empty or invalid.

    Validation is delegated to inputdev.parse_combo so the GUI accepts exactly
    what the daemon accepts (at least one modifier, a non-modifier key).
    """
    try:
        from inputdev import parse_combo as daemon_parse_combo

        parsed = daemon_parse_combo(text)
    except Exception:  # noqa: BLE001 - evdev/inputdev unavailable
        return None
    if parsed is None:
        return None
    return parsed[0], text.split("+")[-1].strip().lower()
