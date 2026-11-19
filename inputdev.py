"""
Kernel-level keyboard I/O via evdev/uinput.

Works the same on X11 and Wayland because it bypasses the display server:
the hotkey is read straight from /dev/input/event* (needs the `input` group)
and text is typed through a virtual uinput keyboard (needs /dev/uinput access).
"""

import selectors
import threading
import time
import unicodedata

import evdev
from evdev import ecodes

VIRTUAL_KEYBOARD_NAME = "soupawhisper-virtual-keyboard"
RESCAN_INTERVAL = 2.0

KEY_ALIASES = {
    "ctrl_r": "rightctrl",
    "right_ctrl": "rightctrl",
    "alt_r": "rightalt",
    "right_alt": "rightalt",
    "scroll_lock": "scrolllock",
    "caps_lock": "capslock",
    "menu": "compose",
}

# Modifier keys tracked for shortcut combos (e.g. super+comma)
MODIFIER_KEYS = {
    ecodes.KEY_LEFTMETA: "super",
    ecodes.KEY_RIGHTMETA: "super",
    ecodes.KEY_LEFTCTRL: "ctrl",
    ecodes.KEY_RIGHTCTRL: "ctrl",
    ecodes.KEY_LEFTALT: "alt",
    ecodes.KEY_RIGHTALT: "alt",
    ecodes.KEY_LEFTSHIFT: "shift",
    ecodes.KEY_RIGHTSHIFT: "shift",
}
MODIFIER_ALIASES = {
    "super": "super", "meta": "super", "win": "super", "windows": "super", "logo": "super",
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt",
    "shift": "shift",
}


def resolve_key(name):
    """Map a config key name (e.g. 'rightctrl', 'f12') to an evdev keycode."""
    name = name.strip().lower()
    name = KEY_ALIASES.get(name, name).replace("_", "")
    code = ecodes.ecodes.get("KEY_" + name.upper())
    if code is None:
        raise ValueError(f"Unknown key: {name}")
    return code


def parse_combo(text):
    """
    Parse a shortcut like 'super+comma' into (frozenset({'super'}), KEY_COMMA).

    Needs at least one modifier (super, ctrl, alt, shift; meta/win and control
    are accepted too) and exactly one non-modifier key. Returns None for an
    empty or invalid combo.
    """
    parts = [p.strip().lower() for p in (text or "").split("+")]
    if len(parts) < 2 or any(not p for p in parts):
        return None
    *mod_parts, key_part = parts
    mods = set()
    for part in mod_parts:
        if part not in MODIFIER_ALIASES:
            return None
        mods.add(MODIFIER_ALIASES[part])
    try:
        keycode = resolve_key(key_part)
    except ValueError:
        return None
    if keycode in MODIFIER_KEYS:
        return None
    return frozenset(mods), keycode


class HotkeyListener:
    """
    Watches every keyboard that has `keycode` (or a combo key) and reports hold/release.

    Devices are read non-exclusively, so the key keeps its normal function.
    If any other key goes down while the hotkey is held, `on_chord` fires so
    the caller can tell a shortcut (Ctrl+C) apart from push-to-talk.

    `combos` maps (frozenset of modifier names, keycode) to a callback, e.g.
    {(frozenset({"super"}), KEY_COMMA): open_settings}. It fires on key-down
    when exactly those modifiers are held (on any watched keyboard).
    """

    def __init__(self, keycode, on_press, on_release, on_chord, combos=None):
        self.keycode = keycode
        self.on_press = on_press
        self.on_release = on_release
        self.on_chord = on_chord
        self.combos = dict(combos or {})
        self.held = False
        self.mods_down = set()  # keycodes of modifier keys currently held
        self.selector = selectors.DefaultSelector()
        self.devices = {}
        self.denied = set()
        # Open keyboards that have the dictation key or any shortcut key
        self.watched_keys = {keycode} | {key for _, key in self.combos}

    def _scan(self):
        for path in evdev.list_devices():
            if path in self.devices:
                continue
            dev = None
            try:
                dev = evdev.InputDevice(path)
                keys = dev.capabilities().get(ecodes.EV_KEY, [])
                if dev.name == VIRTUAL_KEYBOARD_NAME or not self.watched_keys & set(keys):
                    dev.close()
                    continue
            except PermissionError:
                if path not in self.denied:
                    self.denied.add(path)
                    print(f"No permission to read {path}. Is your user in the 'input' group?")
                continue
            except OSError:  # vanished mid-probe
                if dev is not None:
                    dev.close()
                continue
            self.denied.discard(path)
            self.devices[path] = dev
            self.selector.register(dev, selectors.EVENT_READ)
            print(f"Listening on {dev.name} ({path})")

    def _drop(self, dev):
        self.selector.unregister(dev)
        self.devices.pop(dev.path, None)
        try:
            dev.close()
        except OSError:
            pass
        self._lost_release()

    def _lost_release(self):
        """A key-up we may never see (unplug, dropped events): treat as a chord/cancel."""
        self.mods_down.clear()
        if self.held:
            self.held = False
            self.on_chord()

    def _handle(self, event):
        if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_DROPPED:
            self._lost_release()
            return
        if event.type != ecodes.EV_KEY or event.value == 2:  # ignore autorepeat
            return
        if event.value == 1 and not self.held:  # while dictating, any key is a chord/cancel
            self._check_combo(event.code)
        if event.code in MODIFIER_KEYS:
            if event.value == 1:
                self.mods_down.add(event.code)
            else:
                self.mods_down.discard(event.code)
        if event.code == self.keycode:
            if event.value == 1 and not self.held:
                self.held = True
                self.on_press()
            elif event.value == 0 and self.held:
                self.held = False
                self.on_release()
        elif event.value == 1 and self.held:
            self.on_chord()

    def held_modifiers(self):
        return frozenset(MODIFIER_KEYS[code] for code in self.mods_down)

    def _check_combo(self, code):
        if not self.combos or code in MODIFIER_KEYS:
            return
        callback = self.combos.get((self.held_modifiers(), code))
        if callback is not None:
            try:
                callback()
            except Exception as e:  # never let a shortcut break push-to-talk
                print(f"Shortcut handler error: {e}")

    def run(self):
        self._scan()
        if not self.devices:
            print("No readable keyboard found. Is your user in the 'input' group?")
        last_scan = time.monotonic()
        while True:
            try:
                self._poll()
                if time.monotonic() - last_scan >= RESCAN_INTERVAL:
                    self._scan()
                    last_scan = time.monotonic()
            except Exception as e:  # keep listening no matter what
                print(f"Hotkey listener error: {e}")
                time.sleep(0.5)

    def _poll(self):
        for key, _ in self.selector.select(timeout=RESCAN_INTERVAL):
            dev = key.fileobj
            try:
                events = list(dev.read())
            except BlockingIOError:
                continue
            except OSError:  # unplugged
                self._drop(dev)
                continue
            for event in events:
                try:
                    self._handle(event)
                except Exception as e:
                    print(f"Hotkey handler error: {e}")

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()


def _build_keymap():
    """US-layout character -> (keycode, needs_shift)."""
    keymap = {" ": (ecodes.KEY_SPACE, False)}
    for c in "abcdefghijklmnopqrstuvwxyz":
        code = ecodes.ecodes["KEY_" + c.upper()]
        keymap[c] = (code, False)
        keymap[c.upper()] = (code, True)
    for plain, shifted in zip("1234567890", "!@#$%^&*()"):
        code = ecodes.ecodes["KEY_" + plain]
        keymap[plain] = (code, False)
        keymap[shifted] = (code, True)
    for plain, shifted, name in [
        ("-", "_", "MINUS"), ("=", "+", "EQUAL"),
        ("[", "{", "LEFTBRACE"), ("]", "}", "RIGHTBRACE"),
        ("\\", "|", "BACKSLASH"), (";", ":", "SEMICOLON"),
        ("'", '"', "APOSTROPHE"), ("`", "~", "GRAVE"),
        (",", "<", "COMMA"), (".", ">", "DOT"), ("/", "?", "SLASH"),
    ]:
        code = ecodes.ecodes["KEY_" + name]
        keymap[plain] = (code, False)
        keymap[shifted] = (code, True)
    return keymap


KEYMAP = _build_keymap()

# Characters Whisper likes to emit that have no key on a US layout
TRANSLITERATE = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
    "\n": " ", "\t": " ",
})


def to_typeable(text):
    """Reduce text to characters the virtual keyboard can type."""
    text = text.translate(TRANSLITERATE)
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if c in KEYMAP)


class VirtualKeyboard:
    """A uinput keyboard that types text into whatever window has focus."""

    def __init__(self, delay_ms=4):
        self.delay = delay_ms / 1000.0
        keys = {code for code, _ in KEYMAP.values()} | {ecodes.KEY_LEFTSHIFT}
        self.ui = evdev.UInput({ecodes.EV_KEY: sorted(keys)}, name=VIRTUAL_KEYBOARD_NAME)

    def _emit(self, code, value):
        self.ui.write(ecodes.EV_KEY, code, value)
        self.ui.syn()

    def type(self, text):
        for char in to_typeable(text):
            code, shift = KEYMAP[char]
            if shift:
                self._emit(ecodes.KEY_LEFTSHIFT, 1)
            try:
                self._emit(code, 1)
                self._emit(code, 0)
            finally:
                if shift:
                    self._emit(ecodes.KEY_LEFTSHIFT, 0)
            time.sleep(self.delay)

    def close(self):
        self.ui.close()
