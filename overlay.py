"""
On-screen status pill: listening / transcribing / done.

Runs through XWayland on purpose. An X11 override-redirect popup is the only
kind of window a regular app can place on top of GNOME Wayland *without*
taking keyboard focus away from the text field being dictated into.

All public methods are thread-safe; they hop onto the GTK main loop.
"""

import math
import os
import time

os.environ.setdefault("GDK_BACKEND", "x11")

import cairo  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
gi.require_foreign("cairo")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

WIN_W, WIN_H = 520, 96
PILL_H = 44
FRAME_MS = 16
FONT = Pango.FontDescription.from_string("Inter, Cantarell, Sans Medium 11")

COLORS = {
    "listening": (1.00, 0.30, 0.36),
    "transcribing": (1.00, 0.74, 0.25),
    "done": (0.33, 0.87, 0.53),
    "nospeech": (0.62, 0.64, 0.70),
    "error": (1.00, 0.38, 0.38),
}
LABELS = {
    "listening": "Listening",
    "transcribing": "Transcribing",
    "nospeech": "No speech detected",
}
# How long terminal states stay up before fading out
LINGER = {"done": 1.4, "nospeech": 1.0, "error": 3.0}
METER_W = 27
METER_WEIGHTS = (0.55, 0.8, 1.0, 0.8, 0.55)
SNIPPET_LEN = 48


def set_clipboard(text):
    """
    Own the clipboard from this long-lived process (Mutter bridges X11
    selections to Wayland). Unlike wl-copy on GNOME, this never needs focus.
    """
    def _set():
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return False
    GLib.idle_add(_set)


def _rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Overlay:
    def __init__(self, enabled=True, position="bottom", offset=72):
        self.enabled = enabled
        self.position = position
        self.offset = offset
        self.state = "listening"
        self.label = ""
        self.since = 0.0
        self.hide_at = 0.0  # hidden until a state is set
        self.level = 0.0
        self.level_target = 0.0
        self.alpha = 0.0
        self.width = 0.0
        self.ticking = False
        if enabled:
            self._build_window()

    def _build_window(self):
        win = Gtk.Window(type=Gtk.WindowType.POPUP)
        win.set_title("SoupaWhisper")
        win.set_default_size(WIN_W, WIN_H)
        win.set_app_paintable(True)
        win.set_accept_focus(False)
        win.set_keep_above(True)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        screen = win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            win.set_visual(visual)
        else:
            print("Overlay: no compositing, background will not be transparent")
        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self._draw)
        win.add(self.area)
        self.win = win

    # ---- thread-safe API -------------------------------------------------

    def set_state(self, state, text=""):
        """state: listening | transcribing | done | nospeech | error | hidden"""
        if self.enabled:
            GLib.idle_add(self._set_state, state, text)

    def set_level(self, level):
        """Mic level 0..1, called from the audio thread."""
        self.level_target = level

    # ---- main-loop internals ---------------------------------------------

    def _set_state(self, state, text):
        now = time.monotonic()
        if state == "hidden":
            self.hide_at = now
            return False
        self.state = state
        self.label = self._label_for(state, text)
        self.since = now
        self.hide_at = now + LINGER[state] if state in LINGER else None
        if state == "listening":
            self.level = self.level_target = 0.0
        if not self.win.get_visible():
            self.alpha = 0.0
            self.width = self._target_width() * 0.6
            self._place()
            self.win.show_all()
            self.win.input_shape_combine_region(cairo.Region())  # click-through
        if not self.ticking:
            self.ticking = True
            GLib.timeout_add(FRAME_MS, self._tick)
        return False

    def _label_for(self, state, text):
        if state == "done":
            text = " ".join(text.split())
            return text if len(text) <= SNIPPET_LEN else text[:SNIPPET_LEN - 1].rstrip() + "…"
        if state == "error":
            return ("Error: " + text)[:SNIPPET_LEN]
        return LABELS[state]

    def _place(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        x = geo.x + (geo.width - WIN_W) // 2
        if self.position == "top":
            y = geo.y + self.offset
        else:
            y = geo.y + geo.height - WIN_H - self.offset
        self.win.move(x, y)

    def _target_width(self):
        layout = self.area.create_pango_layout(self.label)
        layout.set_font_description(FONT)
        text_w, _ = layout.get_pixel_size()
        width = PILL_H + text_w + 22
        if self.state == "listening":
            width += METER_W + 14
        return min(width, WIN_W - 24)

    def _tick(self):
        now = time.monotonic()
        shown = self.hide_at is None or now < self.hide_at
        self.alpha += ((1.0 if shown else 0.0) - self.alpha) * 0.22
        self.width += (self._target_width() - self.width) * 0.25
        self.level += (self.level_target - self.level) * 0.35
        if not shown and self.alpha < 0.02:
            self.alpha = 0.0
            self.win.hide()
            self.ticking = False
            return False
        self.area.queue_draw()
        return True

    # ---- drawing ---------------------------------------------------------

    def _draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        a = self.alpha
        if a <= 0.001:
            return

        t = time.monotonic() - self.since
        w, h = self.width, PILL_H
        x = (WIN_W - w) / 2
        slide = (1 - a) * 10 * (-1 if self.position == "top" else 1)
        y = (WIN_H - h) / 2 + slide
        r, g, b = COLORS[self.state]

        for grow, shadow in ((12, 0.05), (7, 0.08), (3, 0.12)):
            _rounded_rect(cr, x - grow, y - grow + 4, w + 2 * grow, h + 2 * grow, h / 2 + grow)
            cr.set_source_rgba(0, 0, 0, shadow * a)
            cr.fill()

        _rounded_rect(cr, x, y, w, h, h / 2)
        body = cairo.LinearGradient(0, y, 0, y + h)
        body.add_color_stop_rgba(0, 0.18, 0.18, 0.21, 0.94 * a)
        body.add_color_stop_rgba(1, 0.08, 0.08, 0.10, 0.94 * a)
        cr.set_source(body)
        cr.fill_preserve()
        cr.set_source_rgba(r, g, b, 0.45 * a)
        cr.set_line_width(1.2)
        cr.stroke()

        cx, cy = x + h / 2 + 2, y + h / 2
        getattr(self, "_icon_" + self.state)(cr, cx, cy, t, a, (r, g, b))

        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(FONT)
        layout.set_text(self.label, -1)
        _, text_h = layout.get_pixel_size()
        cr.move_to(x + h + 4, cy - text_h / 2)
        cr.set_source_rgba(0.96, 0.96, 0.98, a)
        PangoCairo.show_layout(cr, layout)

        if self.state == "listening":
            self._meter(cr, x + w - 20 - METER_W, cy, t, a, (r, g, b))

    def _icon_listening(self, cr, cx, cy, t, a, rgb):
        for offset in (0.0, 0.6):
            p = ((t + offset) % 1.2) / 1.2
            cr.arc(cx, cy, 6 + 11 * p, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb, (1 - p) * 0.5 * a)
            cr.set_line_width(2)
            cr.stroke()
        cr.arc(cx, cy, 6 + 2.5 * self.level, 0, 2 * math.pi)
        cr.set_source_rgba(*rgb, a)
        cr.fill()

    def _icon_transcribing(self, cr, cx, cy, t, a, rgb):
        spin = t * 4.0
        cr.set_line_width(2)
        cr.set_source_rgba(*rgb, 0.25 * a)
        cr.arc(cx, cy, 10, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgba(*rgb, a)
        cr.arc(cx, cy, 10, spin, spin + math.pi * (0.5 + 0.3 * math.sin(t * 3)))
        cr.stroke()
        for i in range(3):
            bounce = max(0.0, math.sin(t * 7 - i * 0.9))
            cr.arc(cx - 4.5 + i * 4.5, cy - 2 * bounce, 1.4, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb, (0.45 + 0.55 * bounce) * a)
            cr.fill()

    def _icon_done(self, cr, cx, cy, t, a, rgb):
        pop = min(1.0, t / 0.18)
        cr.arc(cx, cy, 10 * (0.7 + 0.3 * pop), 0, 2 * math.pi)
        cr.set_source_rgba(*rgb, a)
        cr.fill()
        # Check mark drawn on progressively
        p = min(1.0, max(0.0, (t - 0.08) / 0.25))
        pts = [(cx - 4.5, cy + 0.5), (cx - 1.2, cy + 3.8), (cx + 4.8, cy - 3.2)]
        cr.set_source_rgba(0.08, 0.10, 0.09, a)
        cr.set_line_width(2.2)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.move_to(*pts[0])
        first = min(1.0, p / 0.4)
        cr.line_to(pts[0][0] + (pts[1][0] - pts[0][0]) * first,
                   pts[0][1] + (pts[1][1] - pts[0][1]) * first)
        if p > 0.4:
            second = (p - 0.4) / 0.6
            cr.line_to(pts[1][0] + (pts[2][0] - pts[1][0]) * second,
                       pts[1][1] + (pts[2][1] - pts[1][1]) * second)
        cr.stroke()

    def _icon_nospeech(self, cr, cx, cy, t, a, rgb):
        cr.set_source_rgba(*rgb, a)
        cr.set_line_width(2)
        cr.arc(cx, cy, 9, 0, 2 * math.pi)
        cr.stroke()
        cr.move_to(cx - 4, cy)
        cr.line_to(cx + 4, cy)
        cr.stroke()

    def _icon_error(self, cr, cx, cy, t, a, rgb):
        cr.arc(cx, cy, 10, 0, 2 * math.pi)
        cr.set_source_rgba(*rgb, a)
        cr.fill()
        cr.set_source_rgba(0.1, 0.05, 0.05, a)
        cr.set_line_width(2.2)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(cx, cy - 5)
        cr.line_to(cx, cy + 1)
        cr.stroke()
        cr.arc(cx, cy + 4.5, 1.3, 0, 2 * math.pi)
        cr.fill()

    def _meter(self, cr, x, cy, t, a, rgb):
        for i, weight in enumerate(METER_WEIGHTS):
            wobble = 0.65 + 0.35 * math.sin(t * 11 + i * 1.7)
            amount = min(1.0, self.level * weight * wobble)
            bar_h = 4 + 18 * amount
            _rounded_rect(cr, x + i * 6, cy - bar_h / 2, 3, bar_h, 1.5)
            cr.set_source_rgba(*rgb, (0.55 + 0.45 * amount) * a)
            cr.fill()
