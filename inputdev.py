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


def resolve_key(name):
    """Map a config key name (e.g. 'rightctrl', 'f12') to an evdev keycode."""
    name = name.strip().lower()
    name = KEY_ALIASES.get(name, name).replace("_", "")
    code = ecodes.ecodes.get("KEY_" + name.upper())
    if code is None:
        raise ValueError(f"Unknown key: {name}")
    return code


class HotkeyListener:
    """
    Watches every keyboard that has `keycode` and reports hold/release.

    Devices are read non-exclusively, so the key keeps its normal function.
    If any other key goes down while the hotkey is held, `on_chord` fires so
    the caller can tell a shortcut (Ctrl+C) apart from push-to-talk.
    """

    def __init__(self, keycode, on_press, on_release, on_chord):
        self.keycode = keycode
        self.on_press = on_press
        self.on_release = on_release
        self.on_chord = on_chord
        self.held = False
        self.selector = selectors.DefaultSelector()
        self.devices = {}

    def _scan(self):
        for path in evdev.list_devices():
            if path in self.devices:
                continue
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            keys = dev.capabilities().get(ecodes.EV_KEY, [])
            if dev.name == VIRTUAL_KEYBOARD_NAME or self.keycode not in keys:
                dev.close()
                continue
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

    def _handle(self, event):
        if event.type != ecodes.EV_KEY or event.value == 2:  # ignore autorepeat
            return
        if event.code == self.keycode:
            if event.value == 1 and not self.held:
                self.held = True
                self.on_press()
            elif event.value == 0 and self.held:
                self.held = False
                self.on_release()
        elif event.value == 1 and self.held:
            self.on_chord()

    def run(self):
        self._scan()
        if not self.devices:
            print("No readable keyboard found. Is your user in the 'input' group?")
        last_scan = time.monotonic()
        while True:
            for key, _ in self.selector.select(timeout=RESCAN_INTERVAL):
                dev = key.fileobj
                try:
                    events = list(dev.read())
                except OSError:  # unplugged
                    self._drop(dev)
                    continue
                for event in events:
                    try:
                        self._handle(event)
                    except Exception as e:  # keep listening no matter what
                        print(f"Hotkey handler error: {e}")
            if time.monotonic() - last_scan >= RESCAN_INTERVAL:
                self._scan()
                last_scan = time.monotonic()

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
            self._emit(code, 1)
            self._emit(code, 0)
            if shift:
                self._emit(ecodes.KEY_LEFTSHIFT, 0)
            time.sleep(self.delay)

    def close(self):
        self.ui.close()
