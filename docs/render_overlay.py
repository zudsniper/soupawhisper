"""
Render the README overlay images offscreen, no display or microphone needed.

    .venv/bin/python docs/render_overlay.py /tmp/frames
    # then, per animation:
    ffmpeg -framerate 30 -i /tmp/frames/flow/%04d.png \
      -vf "scale=780:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
      -loop 0 docs/overlay-flow.gif

Drives the real Overlay drawing code with a fake clock, mirroring Overlay._tick.
"""

import math
import os
import sys

import cairo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import overlay as ov  # noqa: E402

SCALE = 2
FPS = 30
SAMPLE_TEXT = "Ship the overlay fork and open a pull request."

clock = [100.0]
ov.time.monotonic = lambda: clock[0]
o = ov.Overlay(enabled=True)


def set_state(state, text=""):
    o.state = state
    o.label = o._label_for(state, text)
    o.since = clock[0]
    o.hide_at = clock[0] + ov.LINGER[state] if state in ov.LINGER else None


def step():
    shown = o.hide_at is None or clock[0] < o.hide_at
    o.alpha += ((1.0 if shown else 0.0) - o.alpha) * 0.22
    o.width += (o._target_width() - o.width) * 0.25
    o.level += (o.level_target - o.level) * 0.35


def frame(path):
    w, h = ov.WIN_W * SCALE, ov.WIN_H * SCALE
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    cr = cairo.Context(surface)
    wallpaper = cairo.LinearGradient(0, 0, w, h)
    wallpaper.add_color_stop_rgb(0, 0.24, 0.27, 0.42)
    wallpaper.add_color_stop_rgb(1, 0.42, 0.30, 0.45)
    cr.set_source(wallpaper)
    cr.paint()
    layer = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    lc = cairo.Context(layer)
    lc.scale(SCALE, SCALE)
    o._draw(None, lc)
    cr.set_source_surface(layer, 0, 0)
    cr.paint()
    surface.write_to_png(path)


def speech_level(t):
    """Fake syllable-shaped mic level."""
    env = max(0.0, math.sin(t * 9.0)) * (0.55 + 0.45 * math.sin(t * 2.3 + 1))
    return min(1.0, 0.08 + env)


def render(out, name, script):
    d = os.path.join(out, name)
    os.makedirs(d, exist_ok=True)
    clock[0] = 100.0
    o.alpha = o.level = o.level_target = 0.0
    n = 0
    for state, text, seconds, talking in script:
        if state:
            set_state(state, text)
            if o.alpha < 0.05:
                o.width = o._target_width() * 0.6
        for _ in range(int(seconds * FPS)):
            o.level_target = speech_level(clock[0]) if talking else 0.0
            for _ in range(2):  # the app ticks at 60 fps
                clock[0] += 1 / 60
                step()
            frame(os.path.join(d, f"{n:04d}.png"))
            n += 1


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "frames"
    render(out, "flow", [
        (None, "", 0.3, False),
        ("listening", "", 2.4, True),
        ("transcribing", "", 1.3, False),
        ("done", SAMPLE_TEXT, 2.0, False),
    ])
    render(out, "listening", [("listening", "", 2.0, True)])
    render(out, "transcribing", [("transcribing", "", 2.0, False)])
    render(out, "done", [("done", SAMPLE_TEXT, 1.8, False)])
    render(out, "nospeech", [("nospeech", "", 1.4, False)])
    render(out, "error", [("error", "model failed to load", 1.0, False)])


if __name__ == "__main__":
    main()
