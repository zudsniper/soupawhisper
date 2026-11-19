"""Combo shortcut logic in inputdev.HotkeyListener, fed synthetic evdev events."""

import sys
from pathlib import Path

import pytest
from evdev import InputEvent, ecodes

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inputdev  # noqa: E402

DOWN, UP, REPEAT = 1, 0, 2
SUPER = frozenset({"super"})


def key(code, value):
    return InputEvent(0, 0, ecodes.EV_KEY, code, value)


def syn_dropped():
    return InputEvent(0, 0, ecodes.EV_SYN, ecodes.SYN_DROPPED, 0)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, name):
        return lambda: self.calls.append(name)


@pytest.fixture
def rec():
    return Recorder()


def make(rec, hotkey=ecodes.KEY_RIGHTCTRL, combos=None):
    if combos is None:
        combos = {(SUPER, ecodes.KEY_COMMA): rec("settings")}
    return inputdev.HotkeyListener(
        hotkey,
        on_press=rec("press"),
        on_release=rec("release"),
        on_chord=rec("chord"),
        combos=combos,
    )


def feed(listener, *events):
    for event in events:
        listener._handle(event)


# ---- parse_combo -----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("super+comma", (SUPER, ecodes.KEY_COMMA)),
    ("Super + Comma", (SUPER, ecodes.KEY_COMMA)),
    ("meta+comma", (SUPER, ecodes.KEY_COMMA)),
    ("win+comma", (SUPER, ecodes.KEY_COMMA)),
    ("control+shift+f5", (frozenset({"ctrl", "shift"}), ecodes.KEY_F5)),
    ("alt+f12", (frozenset({"alt"}), ecodes.KEY_F12)),
])
def test_parse_combo_valid(text, expected):
    assert inputdev.parse_combo(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", None, "comma", "super+", "+comma", "super+notakey",
    "hyper+comma", "super+ctrl", "super+leftshift", "super++comma",
])
def test_parse_combo_invalid(text):
    assert inputdev.parse_combo(text) is None


# ---- combos ----------------------------------------------------------------

def test_combo_fires_with_exact_modifiers(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_COMMA, DOWN),
         key(ecodes.KEY_COMMA, UP), key(ecodes.KEY_LEFTMETA, UP))
    assert rec.calls == ["settings"]


def test_right_meta_also_counts_as_super(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_RIGHTMETA, DOWN), key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == ["settings"]


def test_combo_needs_modifier_held(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_COMMA, DOWN), key(ecodes.KEY_COMMA, UP))
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_LEFTMETA, UP),
         key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == []


def test_extra_modifier_blocks_combo(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_LEFTSHIFT, DOWN),
         key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == []


def test_autorepeat_does_not_refire(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_LEFTMETA, REPEAT),
         key(ecodes.KEY_COMMA, DOWN), key(ecodes.KEY_COMMA, REPEAT),
         key(ecodes.KEY_COMMA, REPEAT))
    assert rec.calls == ["settings"]


def test_modifier_held_on_two_keyboards(rec):
    """Left and right super down; releasing one keeps super held."""
    lis = make(rec)
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_RIGHTMETA, DOWN),
         key(ecodes.KEY_LEFTMETA, UP), key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == ["settings"]


def test_syn_dropped_clears_modifiers(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), syn_dropped(), key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == []


def test_device_drop_clears_modifiers(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN))
    lis._lost_release()  # what _drop() calls after an unplug
    feed(lis, key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == []


def test_combo_error_does_not_break_listener(rec):
    def boom():
        raise RuntimeError("boom")

    lis = make(rec, combos={(SUPER, ecodes.KEY_COMMA): boom})
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_COMMA, DOWN),
         key(ecodes.KEY_LEFTMETA, UP), key(ecodes.KEY_COMMA, UP),
         key(ecodes.KEY_RIGHTCTRL, DOWN), key(ecodes.KEY_RIGHTCTRL, UP))
    assert rec.calls == ["press", "release"]


def test_no_combos_is_fine(rec):
    lis = make(rec, combos={})
    feed(lis, key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == []


# ---- dictation key behavior is unchanged ----------------------------------

def test_hold_and_release(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_RIGHTCTRL, DOWN), key(ecodes.KEY_RIGHTCTRL, REPEAT),
         key(ecodes.KEY_RIGHTCTRL, UP))
    assert rec.calls == ["press", "release"]


def test_other_key_while_held_is_chord(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_RIGHTCTRL, DOWN), key(ecodes.KEY_C, DOWN),
         key(ecodes.KEY_C, UP), key(ecodes.KEY_RIGHTCTRL, UP))
    assert rec.calls == ["press", "chord", "release"]


def test_combo_while_dictation_key_held_is_only_chord(rec):
    """While dictating, the settings combo just cancels; it doesn't open settings."""
    lis = make(rec, hotkey=ecodes.KEY_F12)
    feed(lis, key(ecodes.KEY_F12, DOWN), key(ecodes.KEY_LEFTMETA, DOWN),
         key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == ["press", "chord", "chord"]


def test_combo_works_after_dictation_key_released(rec):
    lis = make(rec, hotkey=ecodes.KEY_F12)
    feed(lis, key(ecodes.KEY_F12, DOWN), key(ecodes.KEY_F12, UP),
         key(ecodes.KEY_LEFTMETA, DOWN), key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == ["press", "release", "settings"]


def test_dictation_key_counts_as_modifier(rec):
    """rightctrl held means ctrl is held, so super+comma is not an exact match."""
    lis = make(rec)
    feed(lis, key(ecodes.KEY_RIGHTCTRL, DOWN), key(ecodes.KEY_LEFTMETA, DOWN),
         key(ecodes.KEY_COMMA, DOWN))
    assert rec.calls == ["press", "chord", "chord"]


def test_syn_dropped_while_held_cancels(rec):
    lis = make(rec)
    feed(lis, key(ecodes.KEY_RIGHTCTRL, DOWN), syn_dropped(), key(ecodes.KEY_RIGHTCTRL, UP))
    assert rec.calls == ["press", "chord"]
