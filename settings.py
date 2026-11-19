#!/usr/bin/env python3
"""
SoupaWhisper settings window (GTK 3).

Edits ~/.config/soupawhisper/config.ini through settings_backend and restarts
the dictation service on save. Runs as a normal app window, separate from the
daemon and its overlay. `--screenshot PATH` renders a page offscreen to PNG.
"""

import argparse
import configparser
import copy
import signal
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import settings_backend as backend  # noqa: E402

APP_ID = "io.github.zudsniper.SoupaWhisper.Settings"
TITLE = "SoupaWhisper Settings"
WIN_W, WIN_H = 900, 600
DEFAULT_ID = "@default"
COMPUTE_TYPES = ["float16", "int8_float16", "int8", "float32"]
PAGES = ["mic", "hotkey", "transcription", "behavior", "status"]
DETECT_HINT = "Records every input for 3 seconds while you speak."
CSS = b"""
.sw-error { color: @error_color; }
.sw-ok { color: @success_color; }
.sw-warn { color: @warning_color; }
.sw-heading { font-weight: bold; }
.sw-mono { font-family: monospace; }
"""


# ---- small widget helpers -------------------------------------------------

def _label(text="", css=(), xalign=0.0, wrap=False):
    lbl = Gtk.Label(label=text, xalign=xalign)
    if wrap:
        lbl.set_line_wrap(True)
        lbl.set_max_width_chars(60)
    for cls in css:
        lbl.get_style_context().add_class(cls)
    return lbl


def _row(title, control=None, subtitle=None):
    """ListBox row: title (+ dim subtitle) on the left, control on the right."""
    box = Gtk.Box(spacing=12, margin=12)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER)
    text.pack_start(_label(title), False, False, 0)
    sub = _label(subtitle or "", css=("dim-label",), wrap=True)
    sub.set_no_show_all(not subtitle)
    text.pack_start(sub, False, False, 0)
    box.pack_start(text, True, True, 0)
    if control is not None:
        control.set_valign(Gtk.Align.CENTER)
        box.pack_end(control, False, False, 0)
    row = Gtk.ListBoxRow(activatable=False, selectable=False)
    row.add(box)
    row.subtitle = sub
    return row


def _set_subtitle(row, text, css=None):
    """Show/hide a row's subtitle; css is sw-error, sw-warn or None."""
    sub = row.subtitle
    sub.set_text(text or "")
    sub.set_visible(bool(text))
    ctx = sub.get_style_context()
    for cls in ("sw-error", "sw-warn"):
        ctx.remove_class(cls)
    if css:
        ctx.add_class(css)


def _group(title, rows, header_widget=None):
    """Heading + framed ListBox of rows."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    head = Gtk.Box(spacing=6)
    head.pack_start(_label(title, css=("sw-heading",)), True, True, 0)
    if header_widget is not None:
        head.pack_end(header_widget, False, False, 0)
    box.pack_start(head, False, False, 0)
    listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
    listbox.set_header_func(_separator_header)
    for row in rows:
        listbox.add(row)
    frame = Gtk.Frame()
    frame.add(listbox)
    box.pack_start(frame, False, False, 0)
    box.listbox = listbox
    return box


def _separator_header(row, before):
    if before is not None and row.get_header() is None:
        row.set_header(Gtk.Separator())


def _page(*groups):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin=24)
    for g in groups:
        box.pack_start(g, False, False, 0)
    scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.add(box)
    return scroller


def _fmt(value, digits):
    if digits == 0:
        return str(round(value))
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _to_bool(text, default):
    return configparser.ConfigParser.BOOLEAN_STATES.get(text.lower(), default)


def _to_float(text, default):
    try:
        return float(text)
    except ValueError:
        return float(default)


# ---- the settings view ----------------------------------------------------

class SettingsView(Gtk.Box):
    """Header bar + sidebar + pages, bound to a working copy of the config."""

    def __init__(self, config_path):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.config_path = Path(config_path).expanduser()
        self.loaded = backend.read_config(self.config_path)
        self.working = copy.deepcopy(self.loaded)
        self._loaders = []     # refresh widgets from self.working
        self._watchers = []    # dependent UI, run after any change
        self._errors = set()
        self._loading = False
        self._active = False   # window on screen -> mic monitor may run
        self._busy = False     # save in progress
        self._detecting = False
        self._capture_id = None
        self._swallow_code = None
        self.sources = []
        self.monitor = backend.LevelMonitor(self._on_level_thread)

        self._build_header()
        self.infobar = Gtk.InfoBar(show_close_button=True)
        self.infobar_label = _label(wrap=True)
        self.infobar.get_content_area().add(self.infobar_label)
        self.infobar.connect("response", lambda bar, _r: bar.set_revealed(False))
        self.infobar.set_revealed(False)
        self.infobar.set_no_show_all(True)
        self.pack_start(self.infobar, False, False, 0)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_titled(self._mic_page(), "mic", "Microphone")
        self.stack.add_titled(self._hotkey_page(), "hotkey", "Hotkey")
        self.stack.add_titled(self._transcription_page(), "transcription", "Transcription")
        self.stack.add_titled(self._behavior_page(), "behavior", "Behavior")
        self.stack.add_titled(self._status_page(), "status", "Status")
        self.stack.connect("notify::visible-child", lambda *_: self._update_monitor())
        sidebar = Gtk.StackSidebar(stack=self.stack)
        sidebar.set_size_request(180, -1)
        body = Gtk.Box()
        body.pack_start(sidebar, False, False, 0)
        body.pack_start(self.stack, True, True, 0)
        self.pack_start(body, True, True, 0)

        self._load_widgets()
        self.refresh_status()

    # ---- header, dirty tracking, save ------------------------------------

    def _build_header(self):
        self.header = Gtk.HeaderBar(title=TITLE, show_close_button=True)
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.connect("clicked", lambda *_: self.revert())
        self.save_btn = Gtk.Button(label="Save & Restart")
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self.save())
        self.header.pack_start(self.revert_btn)
        self.header.pack_end(self.save_btn)

    @property
    def dirty(self):
        return self.working != self.loaded

    @property
    def can_save(self):
        return self.dirty and not self._errors and not self._busy

    def _set(self, section, key, value):
        if self._loading or self.working[section][key] == value:
            return
        self.working[section][key] = value
        self._changed()

    def _changed(self):
        for watch in self._watchers:
            watch()
        self.save_btn.set_sensitive(self.can_save)
        self.revert_btn.set_sensitive(self.dirty and not self._busy)

    def _load_widgets(self):
        self._loading = True
        try:
            for load in self._loaders:
                load()
        finally:
            self._loading = False
        self._changed()

    def revert(self):
        self.loaded = backend.read_config(self.config_path)
        self.working = copy.deepcopy(self.loaded)
        self._errors.clear()
        self._load_widgets()
        self._update_monitor()

    def save(self, on_done=None):
        """Write the config, restart the service in a thread, then reload."""
        if not self.can_save:
            return
        try:
            backend.write_config(self.working, self.config_path)
        except OSError as e:
            self.show_message(f"Could not save {self.config_path}: {e}", Gtk.MessageType.ERROR)
            return
        self._busy = True
        self.stack.set_sensitive(False)  # edits made now would be lost on reload
        self._changed()
        custom = self.config_path.resolve() != backend.CONFIG_PATH.resolve()

        def work():
            if custom:
                result = (True, f"Saved to {self.config_path}. Custom path, so the service was not restarted.")
            else:
                result = backend.restart_service()
            GLib.idle_add(self._saved, result, on_done)

        threading.Thread(target=work, daemon=True).start()

    def _saved(self, result, on_done):
        ok, message = result
        self._busy = False
        self.revert()
        self.show_message(message or "Saved.", Gtk.MessageType.INFO if ok else Gtk.MessageType.WARNING)
        self.refresh_status()
        self.stack.set_sensitive(True)
        if on_done and ok:  # on failure stay open so the warning is visible
            on_done()
        return False

    def show_message(self, text, kind=Gtk.MessageType.INFO):
        self.infobar.set_message_type(kind)
        self.infobar_label.set_text(text)
        self.infobar.show_all()
        self.infobar.set_revealed(True)

    # ---- generic bound controls ------------------------------------------

    def _switch(self, section, key):
        sw = Gtk.Switch()
        default = _to_bool(backend.SCHEMA[section][key], False)
        self._loaders.append(lambda: sw.set_active(_to_bool(self.working[section][key], default)))
        sw.connect("notify::active", lambda w, _p: self._set(section, key, "true" if w.get_active() else "false"))
        return sw

    def _spin(self, section, key, lo, hi, step, digits=0):
        spin = Gtk.SpinButton.new_with_range(lo, hi, step)
        spin.set_digits(digits)
        default = backend.SCHEMA[section][key]
        self._loaders.append(lambda: spin.set_value(_to_float(self.working[section][key], default)))
        spin.connect("value-changed", lambda w: self._set(section, key, _fmt(w.get_value(), digits)))
        return spin

    def _combo(self, section, key, options, entry=False):
        combo = Gtk.ComboBoxText.new_with_entry() if entry else Gtk.ComboBoxText()
        for opt in options:
            combo.append(opt, opt)

        def load():
            value = self.working[section][key]
            if entry:
                combo.get_child().set_text(value)
            elif not combo.set_active_id(value):
                combo.append(value, value)  # keep unknown values visible
                combo.set_active_id(value)

        def changed(w):
            value = w.get_child().get_text().strip() if entry else w.get_active_id()
            if value is not None:
                self._set(section, key, value)

        self._loaders.append(load)
        combo.connect("changed", changed)
        return combo

    def _entry(self, section, key, placeholder="", width=16):
        ent = Gtk.Entry(placeholder_text=placeholder, width_chars=width)
        self._loaders.append(lambda: ent.set_text(self.working[section][key]))
        ent.connect("changed", lambda w: self._set(section, key, w.get_text().strip()))
        return ent

    # ---- Microphone page --------------------------------------------------

    def _mic_page(self):
        self.mic_combo = Gtk.ComboBoxText()
        self.mic_combo.set_size_request(340, -1)
        cell = self.mic_combo.get_cells()[0]
        cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.mic_combo.connect("changed", self._on_mic_changed)
        self.mic_row = _row("Input device", self.mic_combo)
        self._watchers.append(self._watch_mic)

        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda *_: self._reload_sources())
        self.detect_btn = Gtk.Button(label="Auto-detect")
        self.detect_btn.connect("clicked", lambda *_: self._auto_detect())
        self.detect_spinner = Gtk.Spinner()
        buttons = Gtk.Box(spacing=6)
        for w in (self.detect_spinner, refresh, self.detect_btn):
            buttons.pack_start(w, False, False, 0)
        self.detect_row = _row("Find my microphone", buttons, DETECT_HINT)
        self.detect_results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                      margin_start=12, margin_end=12, margin_bottom=12)
        self.results_row = Gtk.ListBoxRow(activatable=False, selectable=False)
        self.results_row.add(self.detect_results)
        self.results_row.set_no_show_all(True)

        self.level_bar = Gtk.LevelBar(min_value=0.0, max_value=1.0)
        self.level_bar.set_size_request(260, -1)
        self.db_label = _label("", css=("sw-mono", "dim-label"), xalign=1.0)
        self.db_label.set_width_chars(11)
        meter = Gtk.Box(spacing=12)
        meter.pack_start(self.level_bar, True, True, 0)
        meter.pack_start(self.db_label, False, False, 0)
        level_row = _row("Live level", meter, "Speak normally: aim for about halfway.")
        self.peak_label = _label("–", css=("sw-mono",), xalign=1.0)
        peak_row = _row("Loudest so far", self.peak_label, "Below about −45 dBFS counts as no speech.")

        self._loaders.append(self._load_mic)
        self.sources = backend.list_sources()
        return _page(_group("Input", [self.mic_row, self.detect_row, self.results_row]),
                     _group("Level", [level_row, peak_row]))

    def _reload_sources(self):
        self.sources = backend.list_sources()
        self._loading = True
        try:
            self._load_mic()
        finally:
            self._loading = False
        self._changed()
        self._update_monitor()

    def _load_mic(self):
        combo, current = self.mic_combo, self.working["audio"]["device"]
        combo.remove_all()
        default = next((s for s in self.sources if s.is_default), None)
        combo.append(DEFAULT_ID, f"System default ({default.description})" if default else "System default")
        for src in self.sources:
            combo.append(src.name, src.description)
        if current and not any(s.name == current for s in self.sources):
            combo.append(current, f"{current} (not connected)")
        combo.set_active_id(current or DEFAULT_ID)

    def _describe(self, name):
        if not name:
            return "System default"
        return next((s.description for s in self.sources if s.name == name), name)

    def _watch_mic(self):
        saved, chosen = self.loaded["audio"]["device"], self.working["audio"]["device"]
        if saved == chosen:
            text = f"Saved: {self._describe(saved)}"
        else:
            text = f"Saved: {self._describe(saved)}  →  unsaved change"
        _set_subtitle(self.mic_row, text)
        self.mic_combo.set_tooltip_text(chosen or None)

    def _on_mic_changed(self, combo):
        mic_id = combo.get_active_id()
        if mic_id is None:
            return
        self._set("audio", "device", "" if mic_id == DEFAULT_ID else mic_id)
        if not self._loading:
            self._update_monitor()

    def _update_monitor(self):
        """Run the level monitor only while the Microphone page is on screen."""
        if self._monitoring():
            self.monitor.start(self.working["audio"]["device"] or None)
            self.peak_label.set_text("–")
        else:
            self.monitor.stop()
            self.level_bar.set_value(0.0)
            self.db_label.set_text("")

    def _monitoring(self):
        return self._active and not self._detecting and self.stack.get_visible_child_name() == "mic"

    def _on_level_thread(self, level, db):
        GLib.idle_add(self._show_level, level, db)

    def _show_level(self, level, db):
        if self._monitoring():
            self.level_bar.set_value(level)
            self.db_label.set_text(f"{max(db, -99.0):5.1f} dBFS")
            peak = self.monitor.peak_dbfs
            self.peak_label.set_text(f"{peak:.1f} dBFS" if peak > -99 else "–")
        return False

    def _auto_detect(self):
        sources = list(self.sources)
        if not sources:
            self._show_detect([], "No inputs found. Is PipeWire running?")
            return
        self._detecting = True
        self.detect_btn.set_sensitive(False)
        self.detect_spinner.start()
        self._update_monitor()
        _set_subtitle(self.detect_row, "Speak now…", "sw-warn")

        def work():
            results = backend.measure_sources(sources, 3.0)
            GLib.idle_add(self._detect_done, results)

        threading.Thread(target=work, daemon=True).start()

    def _detect_done(self, results):
        self._detecting = False
        self.detect_spinner.stop()
        self.detect_btn.set_sensitive(True)
        _set_subtitle(self.detect_row, DETECT_HINT)
        best = backend.suggest_source(results)
        lines = [f"{peak:6.1f} dBFS   {src.description}" for src, peak in results]
        if best is not None:
            self.mic_combo.set_active_id(best.name)
            self._show_detect(lines, f"Selected “{best.description}”. Save to use it.")
        else:
            self._show_detect(lines, "No input stood out. Speak louder, or pick one from the list.")
        self._update_monitor()
        return False

    def _show_detect(self, lines, summary):
        box = self.detect_results
        for child in box.get_children():
            box.remove(child)
        for line in lines:
            box.pack_start(_label(line, css=("sw-mono",)), False, False, 0)
        box.pack_start(_label(summary, css=("dim-label",), wrap=True), False, False, 0)
        box.show_all()
        self.results_row.show()

    # ---- Hotkey page ------------------------------------------------------

    def _hotkey_page(self):
        self.key_label = _label(css=("sw-mono",))
        self.change_btn = Gtk.Button(label="Change…")
        self.change_btn.connect("clicked", lambda *_: self._start_capture())
        key_box = Gtk.Box(spacing=12)
        key_box.pack_start(self.key_label, False, False, 0)
        key_box.pack_start(self.change_btn, False, False, 0)
        self.key_row = _row("Dictation key", key_box)
        self._loaders.append(lambda: self.key_label.set_text(self.working["hotkey"]["key"]))

        hold = Gtk.RadioButton.new_with_label(None, "Hold")
        toggle = Gtk.RadioButton.new_with_label_from_widget(hold, "Toggle")
        modes = Gtk.Box(spacing=12)
        modes.pack_start(hold, False, False, 0)
        modes.pack_start(toggle, False, False, 0)
        self._loaders.append(
            lambda: (toggle if self.working["hotkey"]["mode"].lower() == "toggle" else hold).set_active(True))
        hold.connect("toggled", lambda w: w.get_active() and self._set("hotkey", "mode", "hold"))
        toggle.connect("toggled", lambda w: w.get_active() and self._set("hotkey", "mode", "toggle"))
        mode_row = _row("Mode", modes, "Hold to talk. Toggle also lets a quick tap start hands-free recording.")

        min_hold = _row("Minimum hold (s)", self._spin("hotkey", "min_hold", 0.0, 2.0, 0.05, 2),
                        "Shorter presses are ignored.")
        long_row = _row("Tap length (s)", self._spin("behavior", "long_press", 0.1, 2.0, 0.05, 2),
                        "Toggle mode: presses shorter than this count as a tap.")
        self._watchers.append(lambda: long_row.set_sensitive(self.working["hotkey"]["mode"].lower() == "toggle"))

        self.combo_row = _row("Open settings shortcut", self._entry("hotkey", "settings_key", "disabled", 18))
        self._watchers.append(self._watch_combo)
        return _page(_group("Dictation", [self.key_row, mode_row, min_hold, long_row]),
                     _group("Shortcuts", [self.combo_row]))

    def _watch_combo(self):
        text = self.working["hotkey"]["settings_key"]
        if text and backend.parse_combo(text) is None:
            self._errors.add("settings_key")
            _set_subtitle(self.combo_row, f"“{text}” is not a valid shortcut. Example: super+comma", "sw-error")
        else:
            self._errors.discard("settings_key")
            _set_subtitle(self.combo_row, "Example: super+comma. Leave empty to disable.")

    def _start_capture(self):
        win = self.get_toplevel()
        if self._capture_id is not None or not isinstance(win, Gtk.Window):
            return
        self._capture_id = win.connect("key-press-event", self._on_capture_key)
        self.change_btn.set_label("Press a key…")
        _set_subtitle(self.key_row, "Press the key to hold for dictation. Esc cancels.", "sw-warn")

    def _stop_capture(self, message=None, css=None):
        if self._capture_id is not None:
            self.get_toplevel().disconnect(self._capture_id)
            self._capture_id = None
        self.change_btn.set_label("Change…")
        _set_subtitle(self.key_row, message, css)

    def _on_capture_key(self, _win, event):
        code = event.hardware_keycode - 8  # X/GDK keycodes are evdev + 8
        self._swallow_code = code
        if event.keyval == Gdk.KEY_Escape:
            self._stop_capture()
            return True
        name = backend.key_name_from_evdev(code)
        if not name or not backend.is_valid_key(name):
            self._stop_capture(f"That key (code {code}) can't be used. Try another.", "sw-error")
            return True
        self.key_label.set_text(name)
        self._set("hotkey", "key", name)
        self._stop_capture()
        return True

    def on_key_release(self, _win, event):
        """Swallow the release of the captured key so it can't activate a widget."""
        if self._swallow_code is not None and event.hardware_keycode - 8 == self._swallow_code:
            self._swallow_code = None
            return True
        return self._capture_id is not None

    # ---- Transcription page -----------------------------------------------

    def _transcription_page(self):
        model = self._combo("whisper", "model", backend.MODELS, entry=True)
        model_row = _row("Model", model, "Bigger is more accurate but slower. large-v3-turbo suits most GPUs.")
        self.device_row = _row("Device", self._combo("whisper", "device", ["cuda", "cpu"]), "Checking GPU…")
        compute = self._combo("whisper", "compute_type", COMPUTE_TYPES)
        int8_btn = Gtk.Button(label="Use int8")
        int8_btn.set_no_show_all(True)
        int8_btn.connect("clicked", lambda *_: compute.set_active_id("int8"))
        compute_box = Gtk.Box(spacing=6)
        compute_box.pack_start(int8_btn, False, False, 0)
        compute_box.pack_start(compute, False, False, 0)
        compute_row = _row("Compute type", compute_box)

        def watch():
            w = self.working["whisper"]
            suggest = w["device"] == "cpu" and w["compute_type"] != "int8"
            int8_btn.set_visible(suggest)
            if suggest:
                _set_subtitle(compute_row, "int8 is much faster on CPU.", "sw-warn")
            else:
                _set_subtitle(compute_row, "float16 for GPU, int8 for CPU.")

        self._watchers.append(watch)
        lang_row = _row("Language", self._entry("whisper", "language", "auto-detect", 10),
                        "Language code such as en or de. Leave empty to auto-detect.")
        beam_row = _row("Beam size", self._spin("whisper", "beam_size", 1, 10, 1),
                        "Higher is slightly more accurate and slower.")
        return _page(_group("Model", [model_row, self.device_row, compute_row]),
                     _group("Decoding", [lang_row, beam_row]))

    # ---- Behavior page ----------------------------------------------------

    def _behavior_page(self):
        typing = [
            _row("Type into the focused window", self._switch("behavior", "auto_type"),
                 "When off, text is only copied to the clipboard."),
            _row("Trailing space", self._switch("behavior", "trailing_space"),
                 "Adds a space so consecutive dictations don't run together."),
            _row("Delay between keys (ms)", self._spin("behavior", "type_delay_ms", 0, 100, 1)),
            _row("Desktop notifications", self._switch("behavior", "notifications"),
                 "The overlay already shows status."),
        ]
        pos_row = _row("Position", self._combo("overlay", "position", ["top", "bottom"]))
        off_row = _row("Distance from edge (px)", self._spin("overlay", "offset", 0, 2000, 4))
        overlay = [_row("Show overlay", self._switch("overlay", "enabled"),
                        "On-screen listening / transcribing indicator."), pos_row, off_row]

        def watch():
            on = _to_bool(self.working["overlay"]["enabled"], True)
            pos_row.set_sensitive(on)
            off_row.set_sensitive(on)

        self._watchers.append(watch)
        return _page(_group("Typing", typing), _group("Overlay", overlay))

    # ---- Status page ------------------------------------------------------

    def _status_page(self):
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Refresh")
        refresh.set_relief(Gtk.ReliefStyle.NONE)
        refresh.connect("clicked", lambda *_: self.refresh_status())
        self.status_group = _group("System", [], refresh)
        path = _label(str(self.config_path), css=("sw-mono", "dim-label"), xalign=1.0)
        path.set_selectable(True)
        path.set_ellipsize(Pango.EllipsizeMode.START)
        path.set_max_width_chars(50)
        files = _group("Files", [_row("Config file", path)])
        return _page(self.status_group, files)

    def refresh_status(self):
        def work():
            status = backend.system_status()
            GLib.idle_add(self._show_status, status)

        threading.Thread(target=work, daemon=True).start()

    def _show_status(self, status):
        if status.gpu:
            _set_subtitle(self.device_row, f"GPU: {status.gpu}")
        else:
            _set_subtitle(self.device_row, "No NVIDIA GPU detected. Use cpu with int8.", "sw-warn")
        listbox = self.status_group.listbox
        for row in listbox.get_children():
            listbox.remove(row)
        for ok, title, detail in _status_items(status):
            icon = Gtk.Image.new_from_icon_name(
                "emblem-ok-symbolic" if ok else "dialog-warning-symbolic", Gtk.IconSize.BUTTON)
            icon.get_style_context().add_class("sw-ok" if ok else "sw-warn")
            row = _row(title, None, detail)
            box = row.get_child()
            box.pack_start(icon, False, False, 0)
            box.reorder_child(icon, 0)
            listbox.add(row)
        listbox.show_all()
        return False

    # ---- lifecycle --------------------------------------------------------

    def set_active(self, active):
        self._active = active
        self._update_monitor()

    def shutdown(self):
        self._active = False
        self.monitor.stop()


def _status_items(s):
    """(ok, title, plain-language detail) rows for the Status page."""
    items = []
    if s.service_state == "active":
        items.append((True, "Dictation service is running", "systemctl --user status soupawhisper"))
    elif s.service_state == "not installed":
        items.append((False, "Dictation service is not installed", "Run ./install.sh to set it up."))
    else:
        items.append((False, f"Dictation service is {s.service_state}",
                      "Start it with: systemctl --user start soupawhisper"))
    if s.in_input_group:
        items.append((True, "Keyboard access", "You are in the input group, so the hotkey can be read."))
    elif s.input_group_pending:
        # The service is started via `sg input`, so it already has access
        items.append((True, "Keyboard access (re-login pending)",
                      "The service already has access. Log out and back in once to finish setup."))
    else:
        items.append((False, "No keyboard access",
                      "Run ./install.sh, or: sudo usermod -aG input $USER, then log out and back in."))
    if s.uinput_writable:
        items.append((True, "Typing works", "/dev/uinput is writable."))
    else:
        items.append((False, "Can't type into windows",
                      "/dev/uinput is not writable. Run ./install.sh to add the udev rule."))
    items.append((bool(s.gpu), f"GPU: {s.gpu}" if s.gpu else "No NVIDIA GPU detected",
                  "Use device cuda with float16." if s.gpu else "Use device cpu with compute type int8."))
    items.append((s.session_type in ("wayland", "x11"), f"Session: {s.session_type}",
                  "Hotkey and typing use evdev/uinput, so X11 and Wayland both work."))
    mic = s.default_source
    items.append((mic is not None, f"Default microphone: {mic.description}" if mic else "No microphone found",
                  mic.name if mic else "Plug one in, or check PipeWire with: wpctl status"))
    return items


# ---- window and app -------------------------------------------------------

class SettingsWindow(Gtk.ApplicationWindow):
    def __init__(self, app, config_path):
        super().__init__(application=app, title=TITLE)
        self.set_default_size(WIN_W, WIN_H)
        self.set_icon_name("audio-input-microphone")
        self.view = SettingsView(config_path)
        self.set_titlebar(self.view.header)
        self.add(self.view)
        self.connect("map", lambda *_: self.view.set_active(True))
        self.connect("unmap", lambda *_: self.view.set_active(False))
        self.connect("key-release-event", self.view.on_key_release)
        self.connect("delete-event", self._on_delete)
        self.connect("destroy", lambda *_: self.view.shutdown())

    def _on_delete(self, *_):
        view = self.view
        if view._busy:  # a save/restart is finishing; its result closes or reports
            return True
        if not view.dirty:
            return False
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   text="Save changes before closing?")
        dialog.format_secondary_text("Saving restarts the dictation service." if view.can_save else
                                     "Some values are invalid and can't be saved.")
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Discard", Gtk.ResponseType.REJECT)
        if view.can_save:
            dialog.add_button("Save", Gtk.ResponseType.ACCEPT)
            dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            view.save(on_done=self.destroy)
        elif response == Gtk.ResponseType.REJECT:
            self.destroy()
        return True


class SettingsApp(Gtk.Application):
    def __init__(self, config_path):
        super().__init__(application_id=APP_ID)
        self.config_path = config_path
        self.window = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        follow_color_scheme()
        _install_css()
        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)

    def do_activate(self):
        if self.window is None:
            self.window = SettingsWindow(self, self.config_path)
            self.window.connect("destroy", self._on_window_destroy)
            self.window.show_all()
        self.window.present()

    def do_shutdown(self):
        if self.window is not None:
            self.window.view.shutdown()
        Gtk.Application.do_shutdown(self)

    def _on_window_destroy(self, _win):
        self.window = None

    def _on_signal(self):
        if self.window is not None:
            self.window.view.shutdown()
        self.quit()
        return GLib.SOURCE_REMOVE


# ---- follow the system light/dark preference ------------------------------

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Settings"
_color_scheme_refs = []  # keep the proxy / GSettings alive so change signals keep firing


BUILTIN_DARK_THEMES = {"Adwaita", "HighContrast"}  # compiled into GTK 3 with a dark variant


def _theme_dirs(name):
    data_dirs = [GLib.get_user_data_dir(), *GLib.get_system_data_dirs()]
    return [Path(GLib.get_home_dir()) / ".themes" / name] + [Path(d) / "themes" / name for d in data_dirs]


def _has_dark_variant(name):
    """True if GTK 3 can load a dark variant of theme `name`."""
    if name in BUILTIN_DARK_THEMES or name.endswith("-dark"):
        return True
    return any((d / "gtk-3.0" / "gtk-dark.css").exists() or Path(f"{d}-dark", "gtk-3.0").is_dir()
               for d in _theme_dirs(name))


def _apply_dark(dark):
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-application-prefer-dark-theme", dark)
    # A theme without a dark variant (or one that isn't installed, which GTK
    # silently replaces with light Adwaita) would ignore the preference.
    if dark and not _has_dark_variant(settings.props.gtk_theme_name or ""):
        settings.set_property("gtk-theme-name", "Adwaita")


def _portal_color_scheme():
    """XDG portal value (0 no preference, 1 dark, 2 light) and its proxy, or (None, None)."""
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None,
            PORTAL_BUS, PORTAL_PATH, PORTAL_IFACE, None)
        reply = proxy.call_sync("ReadOne", GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
                                Gio.DBusCallFlags.NONE, 1000, None)
    except GLib.Error:
        return None, None
    value = reply.unpack()[0]
    return (value if isinstance(value, int) else None), proxy


def _on_portal_changed(_proxy, _sender, signal, params):
    if signal != "SettingChanged":
        return
    namespace, key, value = params.unpack()
    if namespace == "org.freedesktop.appearance" and key == "color-scheme":
        _apply_dark(value == 1)


def _gnome_settings():
    """org.gnome.desktop.interface GSettings if it has color-scheme (GNOME 42+), else None."""
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup("org.gnome.desktop.interface", True) if source else None
    if schema is None or not schema.has_key("color-scheme"):
        return None
    return Gio.Settings.new("org.gnome.desktop.interface")


def follow_color_scheme():
    """Use the dark theme variant when the desktop prefers dark, and track changes.

    GTK 3 only honours the gtk-theme name, not the newer "prefer dark" switch
    that GNOME, KDE and others publish through the XDG settings portal.
    """
    value, proxy = _portal_color_scheme()
    if proxy is not None and value is not None:
        _apply_dark(value == 1)
        proxy.connect("g-signal", _on_portal_changed)
        _color_scheme_refs.append(proxy)
        return
    settings = _gnome_settings()
    if settings is not None:
        def sync(*_args):
            _apply_dark(settings.get_string("color-scheme") == "prefer-dark")
        sync()
        settings.connect("changed::color-scheme", sync)
        _color_scheme_refs.append(settings)


def _install_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def screenshot(path, config_path, page="mic", settle_ms=2500):
    """Render one page offscreen (never mapped on screen) and save it as PNG."""
    follow_color_scheme()
    _install_css()
    view = SettingsView(config_path)
    view.header.set_show_close_button(False)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.get_style_context().add_class("background")  # offscreen windows leave margins unpainted
    box.pack_start(view.header, False, False, 0)
    box.pack_start(view, True, True, 0)
    win = Gtk.OffscreenWindow()
    win.set_size_request(WIN_W, WIN_H)
    win.add(box)
    win.show_all()
    view.stack.set_transition_type(Gtk.StackTransitionType.NONE)
    view.stack.set_visible_child_name(page)
    view.set_active(True)
    loop = GLib.MainLoop()
    GLib.timeout_add(settle_ms, loop.quit)
    loop.run()
    view.monitor.stop()  # freeze the meter at its last reading
    while Gtk.events_pending():
        Gtk.main_iteration()
    pixbuf = win.get_pixbuf()
    pixbuf.savev(str(path), "png", [], [])
    view.shutdown()
    win.destroy()
    print(f"Saved {path} ({pixbuf.get_width()}x{pixbuf.get_height()})")


def main():
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--config", default=str(backend.CONFIG_PATH), metavar="PATH",
                        help="config file to edit (default: %(default)s)")
    parser.add_argument("--screenshot", metavar="PATH", help="render a page offscreen to PNG and exit")
    parser.add_argument("--page", choices=PAGES, default="mic", help="page for --screenshot")
    args = parser.parse_args()
    if args.screenshot:
        screenshot(args.screenshot, args.config, args.page)
        return 0
    return SettingsApp(args.config).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
