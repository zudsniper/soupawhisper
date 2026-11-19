import io
import json
import math
import subprocess
import types

import numpy as np
import pytest

import settings_backend as sb

MIC1 = "alsa_input.usb-Focusrite_Scarlett_2i2_USB_Y8A0FB526E8A41-00.HiFi__Mic1__source"
MIC2 = "alsa_input.usb-Focusrite_Scarlett_2i2_USB_Y8A0FB526E8A41-00.HiFi__Mic2__source"


def node(obj_id, media_class, name, description=None, nick=None, **extra):
    props = {"media.class": media_class, "node.name": name, "object.id": obj_id, **extra}
    if description:
        props["node.description"] = description
    if nick:
        props["node.nick"] = nick
    return {"id": obj_id, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def default_metadata(name, key="default.audio.source"):
    return {
        "id": 40,
        "type": "PipeWire:Interface:Metadata",
        "props": {"metadata.name": "default"},
        "metadata": [{"subject": 0, "key": key, "type": "Spa:String:JSON", "value": {"name": name}}],
    }


# Trimmed from a real `pw-dump` on a Scarlett 2i2 3rd Gen (pro-audio split profile)
PW_DUMP = [
    {"id": 0, "type": "PipeWire:Interface:Core", "info": {"props": {}}},
    {
        "id": 34,
        "type": "PipeWire:Interface:Metadata",
        "props": {"metadata.name": "schema-sm-settings"},
        "metadata": [{"subject": 0, "key": "device.routes.default-source-volume", "value": {"default": 1.0}}],
    },
    default_metadata(MIC1),
    node(66, "Audio/Source/Internal", "alsa_input.hw_USB_0", "Scarlett 2i2 3rd Gen 0", "Scarlett 2i2 USB"),
    node(68, "Audio/Source", MIC2, "Scarlett 2i2 3rd Gen Input 2 Mic/Inst/Line", "Scarlett 2i2 USB"),
    node(67, "Stream/Input/Audio/Internal", MIC2 + ".split", "Split Scarlett 2i2 3rd Gen Input 2 Mic/Inst/Line"),
    node(70, "Audio/Source", MIC1, "Scarlett 2i2 3rd Gen Input 1 Mic/Inst/Line", "Scarlett 2i2 USB"),
    node(52, "Stream/Input/Audio/Internal", MIC1 + ".split", "Split Scarlett 2i2 3rd Gen Input 1 Mic/Inst/Line"),
    node(80, "Audio/Sink", "alsa_output.usb-Focusrite.HiFi__Line1__sink", "Scarlett Line 1"),
    node(81, "Audio/Source", "alsa_input.usb-Focusrite.raw", "Scarlett 2i2 3rd Gen 0"),
    node(82, "Audio/Source", "alsa_output.pci-0000.analog-stereo.monitor", "Monitor of Built-in"),
    node(83, "Audio/Source", "nick_only_source", nick="Webcam Mic"),
]


def fake_run(outputs):
    """subprocess.run replacement returning canned stdout keyed by argv[0]."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs.get("timeout"), "every subprocess call needs a timeout"
        out = outputs.get(cmd[0])
        if out is None:
            raise FileNotFoundError(cmd[0])
        if isinstance(out, Exception):
            raise out
        code, stdout = out if isinstance(out, tuple) else (0, out)
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr="")

    run.calls = calls
    return run


@pytest.fixture
def which_all(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: f"/usr/bin/{name}")


# ---------------------------------------------------------------- sources

def test_parse_pw_dump_filters_split_monitor_internal_and_sinks():
    names = [s.name for s in sb.parse_pw_dump(PW_DUMP)]
    assert names == [MIC2, MIC1, "alsa_input.usb-Focusrite.raw", "nick_only_source"]


def test_parse_pw_dump_default_and_labels():
    by_name = {s.name: s for s in sb.parse_pw_dump(PW_DUMP)}
    assert by_name[MIC1].is_default and not by_name[MIC2].is_default
    assert by_name[MIC1].description == "Scarlett 2i2 3rd Gen Input 1 Mic/Inst/Line"
    assert by_name["nick_only_source"].description == "Webcam Mic"


def test_parse_pw_dump_configured_default_fallback_and_string_value():
    objs = [node(1, "Audio/Source", MIC2, "two"), default_metadata(MIC2, "default.configured.audio.source")]
    assert sb.parse_pw_dump(objs)[0].is_default
    meta = default_metadata("x")
    meta["metadata"][0]["value"] = json.dumps({"name": MIC2})
    assert sb.parse_pw_dump([node(1, "Audio/Source", MIC2, "two"), meta])[0].is_default


def test_list_sources_uses_pw_dump(monkeypatch, which_all):
    monkeypatch.setattr(sb.subprocess, "run", fake_run({"pw-dump": json.dumps(PW_DUMP)}))
    assert MIC1 in [s.name for s in sb.list_sources()]
    assert sb.default_source().name == MIC1


def test_list_sources_falls_back_to_pactl(monkeypatch, which_all):
    pactl_sources = [
        {"name": MIC1, "description": "Scarlett Input 1", "monitor_of_sink": None},
        {"name": "sink.monitor", "description": "Monitor", "monitor_of_sink": "sink"},
    ]

    def run(cmd, **kwargs):
        assert kwargs.get("timeout")
        if cmd[0] == "pw-dump":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no daemon")
        if cmd[:2] == ["pactl", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pactl_sources), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=MIC1 + "\n", stderr="")

    monkeypatch.setattr(sb.subprocess, "run", run)
    assert sb.list_sources() == [sb.AudioSource(MIC1, "Scarlett Input 1", True)]


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired("pw-dump", 5),
    FileNotFoundError("pw-dump"),
    (0, "not json"),
    (0, "{}"),
])
def test_list_sources_never_raises(monkeypatch, which_all, failure):
    monkeypatch.setattr(sb.subprocess, "run", fake_run({"pw-dump": failure, "pactl": failure}))
    assert sb.list_sources() == []
    assert sb.default_source() is None


def test_list_sources_no_tools(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    assert sb.list_sources() == []


# ---------------------------------------------------------------- suggestion / levels

A = sb.AudioSource("a", "A", False)
B = sb.AudioSource("b", "B", True)


def test_suggest_source_rules():
    assert sb.suggest_source([(A, -30.0), (B, -68.0)]) == A
    assert sb.suggest_source([(B, -68.0), (A, -30.0)]) == A
    assert sb.suggest_source([(A, -30.0)]) == A
    assert sb.suggest_source([(A, -30.0), (B, -35.0)]) is None  # too close
    assert sb.suggest_source([(A, -50.0), (B, -90.0)]) is None  # too quiet
    assert sb.suggest_source([]) is None


def test_chunk_dbfs_and_normalize():
    assert sb.chunk_dbfs(b"") is None
    assert sb.chunk_dbfs(np.zeros(512, dtype=np.int16).tobytes()) <= -100
    full = np.full(512, 32767, dtype=np.int16).tobytes()
    assert sb.chunk_dbfs(full) == pytest.approx(0.0, abs=0.01)
    assert sb.normalize_level(-55) == 0.0
    assert sb.normalize_level(-15) == 1.0
    assert sb.normalize_level(-35) == pytest.approx(0.5)


class FakeProc:
    def __init__(self, payload):
        self.stdout = io.BytesIO(payload)
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_level_monitor_reports_levels_and_env(monkeypatch):
    loud = np.full(1024, 3276, dtype=np.int16).tobytes()  # ~ -20 dBFS
    procs, envs = [], []

    def popen(cmd, **kwargs):
        envs.append(kwargs["env"])
        procs.append(FakeProc(loud))
        return procs[-1]

    monkeypatch.setattr(sb.subprocess, "Popen", popen)
    levels = []
    monitor = sb.LevelMonitor(lambda n, d: levels.append((n, d)))
    assert monitor.peak_dbfs <= -120
    monitor.start(MIC1)
    monitor._thread.join(timeout=2)
    assert envs[0]["PIPEWIRE_NODE"] == MIC1 and envs[0]["PULSE_SOURCE"] == MIC1
    assert levels and levels[0][1] == pytest.approx(-20, abs=0.5)
    assert levels[0][0] == pytest.approx(sb.normalize_level(levels[0][1]))
    assert monitor.peak_dbfs == pytest.approx(-20, abs=0.5)
    monitor.stop()
    monitor.stop()  # idempotent
    assert procs[0].terminated


def test_level_monitor_restart_stops_previous(monkeypatch):
    procs = []
    monkeypatch.setattr(sb.subprocess, "Popen", lambda cmd, **kw: procs.append(FakeProc(b"")) or procs[-1])
    monitor = sb.LevelMonitor(lambda n, d: None)
    monitor.start("a")
    monitor.start("b")
    assert procs[0].terminated and not procs[1].terminated
    monitor.stop()


def test_level_monitor_default_source_has_no_pin(monkeypatch):
    envs = []
    monkeypatch.delenv("PIPEWIRE_NODE", raising=False)
    monkeypatch.delenv("PULSE_SOURCE", raising=False)
    monkeypatch.setattr(sb.subprocess, "Popen", lambda cmd, **kw: envs.append(kw["env"]) or FakeProc(b""))
    monitor = sb.LevelMonitor(lambda n, d: None)
    monitor.start(None)
    monitor.stop()
    assert "PIPEWIRE_NODE" not in envs[0] and "PULSE_SOURCE" not in envs[0]


def test_level_monitor_missing_arecord(monkeypatch):
    def popen(cmd, **kwargs):
        raise FileNotFoundError("arecord")

    monkeypatch.setattr(sb.subprocess, "Popen", popen)
    monitor = sb.LevelMonitor(lambda n, d: None)
    monitor.start(None)
    monitor.stop()
    assert monitor.peak_dbfs <= -120


def test_measure_sources_sorted_loudest_first(monkeypatch):
    payloads = {
        "a": np.full(1024, 300, dtype=np.int16).tobytes(),
        "b": np.full(1024, 10000, dtype=np.int16).tobytes(),
    }
    monkeypatch.setattr(
        sb.subprocess, "Popen", lambda cmd, **kw: FakeProc(payloads[kw["env"]["PIPEWIRE_NODE"]])
    )
    results = sb.measure_sources([A, B], seconds=0.2)
    assert [s for s, _ in results] == [B, A]
    assert results[0][1] > results[1][1]


# ---------------------------------------------------------------- config

SAMPLE = """\
# SoupaWhisper config
[whisper]
# Model comment
model = large-v3-turbo
device=cuda
# language = de
language = en

[hotkey]
key = rightctrl
custom_unknown = keep me

[behavior]
auto_type = true

[extra]
foo = bar
"""


def test_read_config_fills_defaults(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(SAMPLE)
    values = sb.read_config(cfg)
    for section, keys in sb.SCHEMA.items():
        assert set(keys) <= set(values[section])
    assert values["whisper"]["device"] == "cuda"
    assert values["hotkey"]["settings_key"] == "super+comma"
    assert values["audio"]["device"] == ""
    assert values["extra"]["foo"] == "bar"


def test_read_config_missing_file(tmp_path):
    assert sb.read_config(tmp_path / "nope.ini") == sb.SCHEMA


def test_read_config_broken_file(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text("no section header\nkey = value\n")
    assert sb.read_config(cfg) == sb.SCHEMA


def test_schema_matches_dictate_fallbacks():
    assert sb.SCHEMA["whisper"] == {
        "model": "large-v3-turbo", "device": "cuda", "compute_type": "float16",
        "language": "en", "beam_size": "5",
    }
    assert sb.SCHEMA["hotkey"] == {
        "key": "rightctrl", "min_hold": "0.25", "mode": "hold", "settings_key": "super+comma",
    }
    assert sb.SCHEMA["behavior"]["long_press"] == "0.4"
    assert sb.SCHEMA["overlay"] == {"enabled": "true", "position": "bottom", "offset": "72"}
    assert sb.SCHEMA["audio"] == {"device": ""}
    assert "large-v3-turbo" in sb.MODELS and len(sb.MODELS) == 8


def test_write_config_roundtrip_unchanged_is_noop(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(SAMPLE)
    sb.write_config({"whisper": {"device": "cuda", "model": "large-v3-turbo"}}, cfg)
    assert cfg.read_text() == SAMPLE
    assert not (tmp_path / "config.ini.bak").exists()


def test_write_config_preserves_comments_and_unknowns(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(SAMPLE)
    values = sb.read_config(cfg)
    values["whisper"]["device"] = "cpu"
    values["whisper"]["language"] = ""
    values["hotkey"]["key"] = "f12"
    values["audio"]["device"] = MIC1
    sb.write_config(values, cfg)
    lines = cfg.read_text().splitlines()

    for kept in ("# SoupaWhisper config", "# Model comment", "# language = de",
                 "custom_unknown = keep me", "[extra]", "foo = bar"):
        assert kept in lines
    assert "device = cpu" in lines and "device=cuda" not in lines
    assert "language =" in lines
    assert "key = f12" in lines
    # missing key appended at the end of its section, before the blank line
    hotkey_block = lines[lines.index("[hotkey]"):lines.index("[behavior]")]
    assert hotkey_block[-1] == ""
    assert hotkey_block.index("custom_unknown = keep me") < hotkey_block.index("settings_key = super+comma")
    # whisper keys keep their position
    w = lines.index("[whisper]")
    assert lines[w + 2] == "model = large-v3-turbo" and lines[w + 3] == "device = cpu"
    # missing section appended at the end
    assert lines.index("[audio]") > lines.index("[extra]")
    assert lines[lines.index("[audio]") + 1] == f"device = {MIC1}"
    # backup holds the previous content and the result reads back
    assert (tmp_path / "config.ini.bak").read_text() == SAMPLE
    again = sb.read_config(cfg)
    assert again["whisper"]["device"] == "cpu" and again["audio"]["device"] == MIC1
    assert again["whisper"]["language"] == ""


def test_write_config_commented_key_is_not_a_value(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text("[whisper]\n# language = de\n; beam_size = 1\n\n[hotkey]\nkey = f9\n")
    sb.write_config({"whisper": {"language": "fr", "beam_size": "3"}}, cfg)
    assert cfg.read_text() == (
        "[whisper]\n# language = de\n; beam_size = 1\nlanguage = fr\nbeam_size = 3\n\n"
        "[hotkey]\nkey = f9\n"
    )


def test_write_config_missing_file_starts_from_example(tmp_path):
    cfg = tmp_path / "nested" / "dir" / "config.ini"
    values = sb.read_config(cfg)
    values["whisper"]["model"] = "small.en"
    sb.write_config(values, cfg)
    text = cfg.read_text()
    assert "# Model:" in text  # example comments carried over
    assert "model = small.en" in text
    assert not (cfg.parent / "config.ini.bak").exists()
    assert sb.read_config(cfg)["whisper"]["model"] == "small.en"
    assert cfg.stat().st_mode & 0o777 == 0o644


def test_write_config_keeps_file_mode_and_leaves_no_temp(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(SAMPLE)
    cfg.chmod(0o600)
    sb.write_config({"whisper": {"device": "cpu"}}, cfg)
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.ini", "config.ini.bak"]


def test_apply_values_empty_text():
    assert sb.apply_values("", {"audio": {"device": ""}}) == "[audio]\ndevice =\n"


# ---------------------------------------------------------------- status / service

def test_system_status_with_mocks(monkeypatch, which_all):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(sb.subprocess, "run", fake_run({
        "nvidia-smi": "NVIDIA GeForce RTX 3080\n",
        "systemctl": "active\n",
        "pw-dump": json.dumps(PW_DUMP),
    }))
    monkeypatch.setattr(sb, "input_group_state", lambda: (False, True))
    status = sb.system_status()
    assert status.gpu == "NVIDIA GeForce RTX 3080"
    assert status.session_type == "wayland"
    assert status.input_group_pending and not status.in_input_group
    assert status.service_state == "active"
    assert status.default_source.name == MIC1


def test_system_status_no_gpu_no_service(monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
    status = sb.system_status()
    assert status.gpu is None
    assert status.session_type == "unknown"
    assert status.service_state == "not installed"
    assert status.default_source is None


def test_service_not_found(monkeypatch, which_all):
    monkeypatch.setattr(sb.subprocess, "run", fake_run({"systemctl": "not-found\n"}))
    assert sb.service_state() == "not installed"
    ok, msg = sb.restart_service()
    assert not ok and "not installed" in msg


def test_restart_service_ok_and_failure(monkeypatch, which_all):
    run = fake_run({"systemctl": "loaded\n"})
    monkeypatch.setattr(sb.subprocess, "run", run)
    assert sb.restart_service()[0]
    assert ["systemctl", "--user", "restart", "soupawhisper"] in run.calls

    def failing(cmd, **kwargs):
        if "restart" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Job failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="loaded\n", stderr="")

    monkeypatch.setattr(sb.subprocess, "run", failing)
    ok, msg = sb.restart_service()
    assert not ok and "Job failed" in msg


def test_input_group_state(monkeypatch):
    group = types.SimpleNamespace(gr_gid=995, gr_mem=["alice"])
    monkeypatch.setattr(sb.grp, "getgrnam", lambda name: group)
    monkeypatch.setattr(sb.grp, "getgrgid", lambda gid: group)
    monkeypatch.setattr(sb.pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_name="alice", pw_gid=1000))
    monkeypatch.setattr(sb.os, "getgid", lambda: 1000)
    monkeypatch.setattr(sb.os, "getgroups", lambda: [1000, 27])
    assert sb.input_group_state() == (False, True)
    monkeypatch.setattr(sb.os, "getgroups", lambda: [1000, 995])
    assert sb.input_group_state() == (True, False)

    def missing(name):
        raise KeyError(name)

    monkeypatch.setattr(sb.grp, "getgrnam", missing)
    assert sb.input_group_state() == (False, False)


# ---------------------------------------------------------------- keys

def test_key_name_from_evdev():
    assert sb.key_name_from_evdev(97) == "rightctrl"
    assert sb.key_name_from_evdev(88) == "f12"
    assert sb.key_name_from_evdev(51) == "comma"
    assert sb.key_name_from_evdev(999999) is None


def test_is_valid_key():
    assert sb.is_valid_key("rightctrl")
    assert sb.is_valid_key("F12")
    assert sb.is_valid_key("ctrl_r")  # inputdev alias
    assert not sb.is_valid_key("notakey")
    assert not sb.is_valid_key("")


@pytest.mark.parametrize("text,expected", [
    ("super+comma", (frozenset({"super"}), "comma")),
    ("Meta + Comma", (frozenset({"super"}), "comma")),
    ("win+f9", (frozenset({"super"}), "f9")),
    ("control+shift+f12", (frozenset({"ctrl", "shift"}), "f12")),
    ("alt+space", (frozenset({"alt"}), "space")),
    ("logo+comma", (frozenset({"super"}), "comma")),
])
def test_parse_combo_valid(text, expected):
    assert sb.parse_combo(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "super+", "+comma", "hyper+comma", "super+notakey", "super+ctrl",
    "comma", "f12", "super+leftctrl", "ctrl+rightshift",
])
def test_parse_combo_invalid(text):
    assert sb.parse_combo(text) is None


def test_constants():
    assert sb.CONFIG_PATH.name == "config.ini" and sb.CONFIG_PATH.parent.name == "soupawhisper"
    assert sb.EXAMPLE_PATH.exists()
    assert math.isfinite(sb.SILENCE_DBFS)
