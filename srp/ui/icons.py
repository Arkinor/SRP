"""Crisp vector-ish icons rendered with PIL and cached per size/colour."""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Callable

from PIL import Image, ImageDraw

from .theme import rgb


def _line(draw: ImageDraw.ImageDraw, points, width: int, fill, caps: bool = True) -> None:
    pts = [(float(x), float(y)) for x, y in points]
    draw.line(pts, fill=fill, width=max(1, width), joint="curve")
    if caps and width > 1:
        r = width / 2.0
        for x, y in (pts[0], pts[-1]):
            draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _polygon(draw: ImageDraw.ImageDraw, points, fill=None, outline=None, width: int = 1) -> None:
    pts = [(float(x), float(y)) for x, y in points]
    if fill is not None:
        draw.polygon(pts, fill=fill)
    if outline is not None:
        draw.line(pts + [pts[0]], fill=outline, width=max(1, width), joint="curve")


def _draw_record(d, S, w, c):
    r = S * 0.245
    d.ellipse((S / 2 - r, S / 2 - r, S / 2 + r, S / 2 + r), fill=c)


def _draw_stop(d, S, w, c):
    r = S * 0.10
    d.rounded_rectangle((S * 0.29, S * 0.29, S * 0.71, S * 0.71), radius=r, fill=c)


def _draw_pause(d, S, w, c):
    r = S * 0.045
    d.rounded_rectangle((S * 0.34, S * 0.26, S * 0.44, S * 0.74), radius=r, fill=c)
    d.rounded_rectangle((S * 0.56, S * 0.26, S * 0.66, S * 0.74), radius=r, fill=c)


def _draw_play(d, S, w, c):
    _polygon(d, [(S * 0.34, S * 0.24), (S * 0.76, S * 0.5), (S * 0.34, S * 0.76)], fill=c)


def _draw_mic(d, S, w, c):
    d.rounded_rectangle((S * 0.36, S * 0.13, S * 0.64, S * 0.56),
                        radius=S * 0.14, outline=c, width=w)
    d.arc((S * 0.26, S * 0.34, S * 0.74, S * 0.72), 0, 180, fill=c, width=w)
    _line(d, [(S * 0.5, S * 0.72), (S * 0.5, S * 0.84)], w, c)
    _line(d, [(S * 0.36, S * 0.87), (S * 0.64, S * 0.87)], w, c)


def _draw_mic_off(d, S, w, c):
    _draw_mic(d, S, w, c)
    _line(d, [(S * 0.18, S * 0.18), (S * 0.82, S * 0.86)], w * 1.15, c)


def _draw_speaker(d, S, w, c):
    _polygon(d, [(S * 0.12, S * 0.42), (S * 0.28, S * 0.42), (S * 0.50, S * 0.24),
                 (S * 0.50, S * 0.76), (S * 0.28, S * 0.58), (S * 0.12, S * 0.58)], fill=c)
    d.arc((S * 0.44, S * 0.32, S * 0.68, S * 0.68), -55, 55, fill=c, width=w)
    d.arc((S * 0.42, S * 0.20, S * 0.86, S * 0.80), -50, 50, fill=c, width=w)


def _draw_monitor(d, S, w, c):
    d.rounded_rectangle((S * 0.11, S * 0.16, S * 0.89, S * 0.70),
                        radius=S * 0.075, outline=c, width=w)
    _line(d, [(S * 0.5, S * 0.70), (S * 0.5, S * 0.84)], w, c, caps=False)
    _line(d, [(S * 0.34, S * 0.86), (S * 0.66, S * 0.86)], w, c)


def _draw_window(d, S, w, c):
    d.rounded_rectangle((S * 0.13, S * 0.20, S * 0.87, S * 0.80),
                        radius=S * 0.08, outline=c, width=w)
    _line(d, [(S * 0.13, S * 0.37), (S * 0.87, S * 0.37)], w, c, caps=False)
    for x in (0.23, 0.31):
        r = S * 0.022
        d.ellipse((S * x - r, S * 0.285 - r, S * x + r, S * 0.285 + r), fill=c)


def _draw_crop(d, S, w, c):
    for (x, y, dx, dy) in ((0.20, 0.20, 1, 1), (0.80, 0.20, -1, 1),
                           (0.20, 0.80, 1, -1), (0.80, 0.80, -1, -1)):
        _line(d, [(S * (x + dx * 0.14), S * y), (S * x, S * y), (S * x, S * (y + dy * 0.14))],
              w, c, caps=False)
    d.rounded_rectangle((S * 0.38, S * 0.38, S * 0.62, S * 0.62), radius=S * 0.03, outline=c, width=max(1, w // 1))


def _draw_folder(d, S, w, c):
    pts = [(S * 0.12, S * 0.78), (S * 0.12, S * 0.28), (S * 0.38, S * 0.28),
           (S * 0.47, S * 0.39), (S * 0.88, S * 0.39), (S * 0.88, S * 0.78)]
    _polygon(d, pts, outline=c, width=w)


def _draw_tune(d, S, w, c):
    rows = ((0.32, 0.38), (0.5, 0.64), (0.68, 0.34))
    for y, knob in rows:
        yy = S * y
        r = S * 0.062
        _line(d, [(S * 0.16, yy), (S * knob - r, yy)], w, c, caps=False)
        _line(d, [(S * knob + r, yy), (S * 0.84, yy)], w, c, caps=False)
        d.ellipse((S * knob - r, yy - r, S * knob + r, yy + r), outline=c, width=w)


def _draw_camera(d, S, w, c):
    d.rounded_rectangle((S * 0.11, S * 0.30, S * 0.89, S * 0.82),
                        radius=S * 0.09, outline=c, width=w)
    _line(d, [(S * 0.34, S * 0.30), (S * 0.42, S * 0.20), (S * 0.58, S * 0.20),
              (S * 0.66, S * 0.30)], w, c)
    r = S * 0.16
    d.ellipse((S * 0.5 - r, S * 0.56 - r, S * 0.5 + r, S * 0.56 + r), outline=c, width=w)


def _draw_trash(d, S, w, c):
    _line(d, [(S * 0.22, S * 0.27), (S * 0.78, S * 0.27)], w, c)
    _line(d, [(S * 0.40, S * 0.27), (S * 0.40, S * 0.18), (S * 0.60, S * 0.18),
              (S * 0.60, S * 0.27)], w, c)
    _line(d, [(S * 0.30, S * 0.27), (S * 0.33, S * 0.82), (S * 0.67, S * 0.82),
              (S * 0.70, S * 0.27)], w, c)
    for x in (0.43, 0.5, 0.57):
        _line(d, [(S * x, S * 0.40), (S * x, S * 0.70)], max(1, w - 1), c, caps=False)


def _draw_refresh(d, S, w, c):
    d.arc((S * 0.20, S * 0.20, S * 0.80, S * 0.80), 40, 320, fill=c, width=w)
    _polygon(d, [(S * 0.78, S * 0.16), (S * 0.82, S * 0.36), (S * 0.63, S * 0.31)], fill=c)


def _draw_minimize(d, S, w, c):
    _line(d, [(S * 0.27, S * 0.5), (S * 0.73, S * 0.5)], w, c)


def _draw_close(d, S, w, c):
    _line(d, [(S * 0.29, S * 0.29), (S * 0.71, S * 0.71)], w, c)
    _line(d, [(S * 0.71, S * 0.29), (S * 0.29, S * 0.71)], w, c)


def _draw_chevron_left(d, S, w, c):
    _line(d, [(S * 0.58, S * 0.30), (S * 0.40, S * 0.50), (S * 0.58, S * 0.70)], w, c)


def _draw_chevron_right(d, S, w, c):
    _line(d, [(S * 0.42, S * 0.30), (S * 0.60, S * 0.50), (S * 0.42, S * 0.70)], w, c)


def _draw_scissors(d, S, w, c):
    _line(d, [(S * 0.30, S * 0.18), (S * 0.70, S * 0.62)], w, c, caps=False)
    _line(d, [(S * 0.70, S * 0.18), (S * 0.30, S * 0.62)], w, c, caps=False)
    r = S * 0.11
    d.ellipse((S * 0.16 - r, S * 0.66 - r, S * 0.16 + r, S * 0.66 + r),
              outline=c, width=w)
    d.ellipse((S * 0.84 - r, S * 0.66 - r, S * 0.84 + r, S * 0.66 + r),
              outline=c, width=w)


def _draw_undo(d, S, w, c):
    d.arc((S * 0.20, S * 0.26, S * 0.80, S * 0.84), 35, 320, fill=c, width=w)
    _polygon(d, [(S * 0.16, S * 0.24), (S * 0.20, S * 0.48), (S * 0.40, S * 0.34)], fill=c)


def _draw_chevron_down(d, S, w, c):
    _line(d, [(S * 0.30, S * 0.42), (S * 0.50, S * 0.60), (S * 0.70, S * 0.42)], w, c)


def _draw_chevron_up(d, S, w, c):
    _line(d, [(S * 0.30, S * 0.58), (S * 0.50, S * 0.40), (S * 0.70, S * 0.58)], w, c)


def _draw_check(d, S, w, c):
    _line(d, [(S * 0.24, S * 0.52), (S * 0.43, S * 0.70), (S * 0.77, S * 0.31)], w, c)


def _draw_clock(d, S, w, c):
    d.ellipse((S * 0.16, S * 0.16, S * 0.84, S * 0.84), outline=c, width=w)
    _line(d, [(S * 0.5, S * 0.46), (S * 0.5, S * 0.27)], w, c)
    _line(d, [(S * 0.5, S * 0.46), (S * 0.65, S * 0.56)], w, c)


def _draw_cursor(d, S, w, c):
    pts = [(S * 0.30, S * 0.17), (S * 0.30, S * 0.76), (S * 0.43, S * 0.63),
           (S * 0.53, S * 0.84), (S * 0.64, S * 0.78), (S * 0.54, S * 0.58),
           (S * 0.72, S * 0.56)]
    _polygon(d, pts, fill=c)


def _draw_sparkle(d, S, w, c):
    _polygon(d, [(S * 0.5, S * 0.12), (S * 0.58, S * 0.42), (S * 0.88, S * 0.5),
                 (S * 0.58, S * 0.58), (S * 0.5, S * 0.88), (S * 0.42, S * 0.58),
                 (S * 0.12, S * 0.5), (S * 0.42, S * 0.42)], fill=c)


def _draw_info(d, S, w, c):
    d.ellipse((S * 0.14, S * 0.14, S * 0.86, S * 0.86), outline=c, width=w)
    r = S * 0.035
    d.ellipse((S * 0.5 - r, S * 0.31 - r, S * 0.5 + r, S * 0.31 + r), fill=c)
    _line(d, [(S * 0.5, S * 0.44), (S * 0.5, S * 0.70)], w, c)


def _draw_external(d, S, w, c):
    _line(d, [(S * 0.44, S * 0.24), (S * 0.22, S * 0.24), (S * 0.22, S * 0.78),
              (S * 0.76, S * 0.78), (S * 0.76, S * 0.56)], w, c)
    _line(d, [(S * 0.46, S * 0.54), (S * 0.80, S * 0.20)], w, c)
    _line(d, [(S * 0.62, S * 0.20), (S * 0.80, S * 0.20), (S * 0.80, S * 0.38)], w, c)


def _draw_plus(d, S, w, c):
    _line(d, [(S * 0.5, S * 0.24), (S * 0.5, S * 0.76)], w, c)
    _line(d, [(S * 0.24, S * 0.5), (S * 0.76, S * 0.5)], w, c)


def _draw_warning(d, S, w, c):
    _polygon(d, [(S * 0.5, S * 0.14), (S * 0.90, S * 0.84), (S * 0.10, S * 0.84)],
             outline=c, width=w)
    _line(d, [(S * 0.5, S * 0.40), (S * 0.5, S * 0.60)], w, c)
    r = S * 0.032
    d.ellipse((S * 0.5 - r, S * 0.69 - r, S * 0.5 + r, S * 0.69 + r), fill=c)


_DRAW: dict[str, Callable] = {
    "record": _draw_record,
    "stop": _draw_stop,
    "pause": _draw_pause,
    "play": _draw_play,
    "mic": _draw_mic,
    "mic_off": _draw_mic_off,
    "speaker": _draw_speaker,
    "monitor": _draw_monitor,
    "window": _draw_window,
    "crop": _draw_crop,
    "folder": _draw_folder,
    "tune": _draw_tune,
    "camera": _draw_camera,
    "trash": _draw_trash,
    "refresh": _draw_refresh,
    "minimize": _draw_minimize,
    "close": _draw_close,
    "chevron_down": _draw_chevron_down,
    "chevron_up": _draw_chevron_up,
    "chevron_left": _draw_chevron_left,
    "chevron_right": _draw_chevron_right,
    "scissors": _draw_scissors,
    "undo": _draw_undo,
    "check": _draw_check,
    "clock": _draw_clock,
    "cursor": _draw_cursor,
    "sparkle": _draw_sparkle,
    "info": _draw_info,
    "external": _draw_external,
    "plus": _draw_plus,
    "warning": _draw_warning,
}


@lru_cache(maxsize=512)
def icon(name: str, size: int, color: str = "#FFFFFF", alpha: int = 255,
         stroke: float = 0.082) -> Image.Image:
    size = max(6, int(size))
    ss = 4
    S = size * ss
    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    r, g, b = rgb(color)
    fill = (r, g, b, alpha)
    width = max(1, round(size * stroke * ss))
    fn = _DRAW.get(name)
    if fn is not None:
        try:
            fn(draw, S, width, fill)
        except Exception:
            pass
    return image.resize((size, size), Image.LANCZOS)
