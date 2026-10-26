"""Visual language: palette, typography, gradients and glass surfaces."""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Iterable, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ----------------------------------------------------------------------
# Palette
# ----------------------------------------------------------------------
TEXT = "#F5F7FC"
TEXT_SOFT = "#D7DCEA"
MUTED = "#A6B0C6"
DIM = "#7C869C"
FAINT = "#5A6377"
WHITE = "#FFFFFF"
BLACK = "#05060B"

ACCENT = "#FF5F6D"
ACCENT_DEEP = "#E62E4B"
ACCENT_LIGHT = "#FF8A93"
GREEN = "#35E08D"
BLUE = "#5AA9FF"
YELLOW = "#FFCE6B"
PURPLE = "#B79CFF"
RED = "#FF6B6B"

FACE_SEMIBOLD = "Segoe UI Semibold"
FACE = "Segoe UI"
FACE_LIGHT = "Segoe UI Light"
FACE_MONO = "Consolas"


def rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def rgba(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    r, g, b = rgb(value)
    return r, g, b, alpha


def blend(fg: str, bg: str, t: float) -> str:
    fr, fg_, fb = rgb(fg)
    br, bg_, bb = rgb(bg)
    t = max(0.0, min(1.0, t))
    return "#%02X%02X%02X" % (
        round(fr * t + br * (1 - t)),
        round(fg_ * t + bg_ * (1 - t)),
        round(fb * t + bb * (1 - t)),
    )


def solid(value: str, base: str = "#0B0F18") -> str:
    """Flatten a #RRGGBBAA colour to #RRGGBB for Tk canvas items."""
    value = value.lstrip("#")
    if len(value) == 8:
        return blend("#" + value[:6], base, int(value[6:8], 16) / 255)
    return "#" + value


def shade(value: str, factor: float) -> str:
    r, g, b = rgb(value)
    if factor >= 1.0:
        return "#%02X%02X%02X" % (
            min(255, round(r * factor)), min(255, round(g * factor)), min(255, round(b * factor)))
    return "#%02X%02X%02X" % (round(r * factor), round(g * factor), round(b * factor))


def mix_color(a: tuple[int, int, int, int], b: tuple[int, int, int, int], t: float) -> tuple[int, int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(4))  # type: ignore[return-value]


class Theme:
    """Scale-aware metrics and fonts."""

    def __init__(self, scale: float = 1.0):
        self.s = max(0.7, float(scale))
        self._fonts: dict[str, tuple] = {}

    # ---- geometry -----------------------------------------------------
    def px(self, value: float) -> int:
        return int(round(value * self.s))

    def pt(self, value: float) -> float:
        return value * self.s

    # ---- typography ---------------------------------------------------
    def font(self, size: float, weight: str = "normal") -> tuple:
        key = f"{size}:{weight}"
        cached = self._fonts.get(key)
        if cached is not None:
            return cached
        pixels = max(7, int(round(size * self.s)))
        if weight == "semibold":
            font = (FACE_SEMIBOLD, -pixels)
        elif weight == "bold":
            font = (FACE, -pixels, "bold")
        elif weight == "light":
            font = (FACE_LIGHT, -pixels)
        elif weight == "mono":
            font = (FACE_MONO, -pixels)
        else:
            font = (FACE, -pixels)
        self._fonts[key] = font
        return font


# ----------------------------------------------------------------------
# Image primitives (all rendered supersampled for smooth edges)
# ----------------------------------------------------------------------
def _ss_for(width: int, height: int, base: int = 4, cap: int = 1500) -> int:
    longest = max(1, max(width, height))
    ss = max(1, min(base, cap // longest))
    return ss


@lru_cache(maxsize=512)
def _rounded_mask(width: int, height: int, radius: int, ss: int) -> Image.Image:
    mask = Image.new("L", (width * ss, height * ss), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, width * ss - 1, height * ss - 1),
        radius=max(1, radius * ss), fill=255,
    )
    if ss > 1:
        mask = mask.resize((width, height), Image.LANCZOS)
    return mask


def rounded_mask(width: int, height: int, radius: int) -> Image.Image:
    width, height = max(1, int(width)), max(1, int(height))
    radius = max(0, min(int(radius), min(width, height) // 2))
    return _rounded_mask(width, height, radius, _ss_for(width, height))


@lru_cache(maxsize=256)
def _gradient(size: tuple[int, int], top: tuple[int, int, int, int],
              bottom: tuple[int, int, int, int], diagonal: bool = False) -> Image.Image:
    width, height = size
    if height <= 0 or width <= 0:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    if diagonal:
        x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
        y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
        ramp = np.clip((x + y) * 0.5, 0.0, 1.0).astype(np.float32)
    arr = np.zeros((height, width, 4), dtype=np.float32)
    for i in range(4):
        arr[:, :, i] = top[i] + (bottom[i] - top[i]) * ramp
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def gradient(size: tuple[int, int], top: Sequence[int], bottom: Sequence[int],
             diagonal: bool = False) -> Image.Image:
    return _gradient((int(size[0]), int(size[1])), tuple(top), tuple(bottom), diagonal)  # type: ignore[arg-type]


def round_image(image: Image.Image, radius: int, border: Optional[tuple[int, int, int, int]] = None,
                border_width: int = 1) -> Image.Image:
    """Return RGBA copy of image clipped to rounded corners with an optional border."""
    width, height = image.size
    result = image.convert("RGBA")
    result.putalpha(rounded_mask(width, height, radius))
    if border is not None and border_width > 0:
        stroke = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(stroke)
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1), radius=max(1, radius),
            outline=border, width=max(1, border_width),
        )
        result = Image.alpha_composite(result, stroke)
    return result


def glow(width: int, height: int, color: tuple[int, int, int], strength: float = 0.55,
         blur: Optional[int] = None) -> Image.Image:
    """Soft radial glow; returned image is padded by the blur radius."""
    blur = int(blur if blur is not None else max(10, min(width, height) * 0.35))
    pad = blur * 2
    canvas = Image.new("RGBA", (width + pad * 2, height + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((pad, pad, pad + width, pad + height),
                           radius=min(width, height) // 2,
                           fill=(*color, int(255 * strength)))
    return canvas.filter(ImageFilter.GaussianBlur(blur))


@lru_cache(maxsize=512)
def button_face(width: int, height: int, radius: int, kind: str, state: int) -> Image.Image:
    """Glass/gradient face for a button or field.

    state: 0 normal, 1 hover, 2 pressed, 3 disabled
    """
    width, height = max(2, int(width)), max(2, int(height))
    ss = _ss_for(width, height)
    w, h = width * ss, height * ss
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius * ss, fill=255)

    if kind == "primary":
        base = gradient((w, h), (255, 126, 116, 255), (227, 39, 71, 255))
        body = base
        if state == 1:
            body = gradient((w, h), (255, 140, 128, 255), (236, 52, 82, 255))
        elif state == 2:
            body = gradient((w, h), (236, 106, 100, 255), (205, 28, 58, 255))
        canvas.paste(body, (0, 0), mask)
        gloss = gradient((w, h), (255, 255, 255, 64), (255, 255, 255, 0))
        gloss_mask = Image.new("L", (w, h), 0)
        gloss_mask.paste(mask)
        # only the top 45% carries the gloss
        fade = Image.new("L", (w, h), 0)
        ImageDraw.Draw(fade).rectangle((0, 0, w, int(h * 0.48)), fill=255)
        fade = fade.filter(ImageFilter.GaussianBlur(max(1, h * 0.06)))
        gloss_mask = Image.composite(gloss_mask, Image.new("L", (w, h), 0), fade)
        canvas = Image.composite(gloss, canvas, gloss_mask)
        outline = (255, 255, 255, 84 if state == 0 else 110)
    elif kind == "danger":
        alpha = (34, 50, 66)[min(state, 2)]
        body = Image.new("RGBA", (w, h), (255, 92, 102, alpha))
        canvas.paste(body, (0, 0), mask)
        outline = (255, 120, 128, 84 if state == 0 else 112)
    elif kind == "ghost":
        alpha = (0, 15, 26)[min(state, 2)]
        if alpha:
            canvas.paste(Image.new("RGBA", (w, h), (255, 255, 255, alpha)), (0, 0), mask)
        outline = (255, 255, 255, 0)
    elif kind == "dark":
        alpha = (240, 246, 250)[min(state, 2)]
        canvas.paste(Image.new("RGBA", (w, h), (12, 14, 24, alpha)), (0, 0), mask)
        outline = (255, 255, 255, 34 if state == 0 else 54)
    elif kind == "selected":
        alpha = (48, 62, 76)[min(state, 2)]
        canvas.paste(Image.new("RGBA", (w, h), (255, 255, 255, alpha)), (0, 0), mask)
        outline = (255, 255, 255, 86 if state == 0 else 110)
    elif kind == "green":
        body = gradient((w, h), (70, 235, 160, 255), (28, 184, 114, 255))
        canvas.paste(body, (0, 0), mask)
        outline = (255, 255, 255, 70)
    else:  # secondary glass
        alpha = (26, 38, 50)[min(state, 2)]
        canvas.paste(Image.new("RGBA", (w, h), (255, 255, 255, alpha)), (0, 0), mask)
        outline = (255, 255, 255, 46 if state == 0 else 78)

    if outline[3]:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=radius * ss, outline=outline,
            width=max(1, round(1.2 * ss)),
        )
        canvas = Image.alpha_composite(canvas, layer)

    # top inner specular
    if kind not in ("ghost",):
        spec = gradient((w, h), (255, 255, 255, 30), (255, 255, 255, 0))
        spec_mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(spec_mask).rounded_rectangle(
            (0, 0, w - 1, h - 1), radius=radius * ss, outline=255, width=max(1, ss),
        )
        top = Image.new("L", (w, h), 0)
        ImageDraw.Draw(top).rectangle((0, 0, w, int(h * 0.55)), fill=255)
        spec_mask = Image.composite(spec_mask, Image.new("L", (w, h), 0), top)
        canvas = Image.composite(spec, canvas, spec_mask)

    return canvas.resize((width, height), Image.LANCZOS)


@lru_cache(maxsize=64)
def toggle_face(width: int, height: int, on: bool) -> Image.Image:
    ss = 4
    w, h = width * ss, height * ss
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    radius = h // 2
    if on:
        track = gradient((w, h), (68, 232, 158, 255), (31, 193, 118, 255))
    else:
        track = Image.new("RGBA", (w, h), (255, 255, 255, 40))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    canvas.paste(track, (0, 0), mask)
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, w - 1, h - 1), radius=radius,
        outline=(255, 255, 255, 70 if on else 46), width=max(1, ss),
    )
    knob = int(h * 0.78)
    cx = int(w - radius) if on else int(radius)
    cy = h // 2
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (cx - knob // 2 - ss, cy - knob // 2 + ss, cx + knob // 2 + ss, cy + knob // 2 + ss),
        fill=(0, 0, 0, 70),
    )
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(ss)))
    ImageDraw.Draw(canvas).ellipse(
        (cx - knob // 2, cy - knob // 2, cx + knob // 2, cy + knob // 2),
        fill=(255, 255, 255, 255),
    )
    return canvas.resize((width, height), Image.LANCZOS)


@lru_cache(maxsize=32)
def slider_thumb(diameter: int) -> Image.Image:
    ss = 4
    size = diameter * ss
    pad = max(3 * ss, diameter // 5 * ss)
    canvas = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((pad, pad + ss, pad + size, pad + size + ss), fill=(0, 0, 0, 90))
    canvas = canvas.filter(ImageFilter.GaussianBlur(ss))
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((pad, pad, pad + size, pad + size), fill=(255, 255, 255, 255))
    side = (size + pad * 2) // ss
    return canvas.resize((side, side), Image.LANCZOS)


@lru_cache(maxsize=16)
def logo(size: int = 36) -> Image.Image:
    ss = 4
    w = h = size * ss
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    radius = int(w * 0.29)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    body = gradient((w, h), (46, 30, 82, 255), (10, 11, 20, 255), diagonal=True)
    canvas.paste(body, (0, 0), mask)

    halo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hd = ImageDraw.Draw(halo)
    hd.ellipse((int(w * 0.12), int(h * 0.10), int(w * 0.78), int(h * 0.72)),
               fill=(120, 84, 255, 96))
    halo = halo.filter(ImageFilter.GaussianBlur(int(w * 0.10)))
    canvas = Image.alpha_composite(canvas, Image.composite(halo, Image.new("RGBA", (w, h), (0, 0, 0, 0)), mask))

    # record disc with a glassy ring
    ring = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    cx = cy = w // 2
    rr = int(w * 0.235)
    rd.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=(255, 88, 104, 58))
    ring = ring.filter(ImageFilter.GaussianBlur(int(w * 0.025)))
    canvas = Image.alpha_composite(canvas, Image.composite(ring, Image.new("RGBA", (w, h), (0, 0, 0, 0)), mask))

    disc = gradient((w, h), (255, 138, 124, 255), (225, 40, 77, 255))
    disc_mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(disc_mask).ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=255)
    canvas.paste(disc, (0, 0), disc_mask)
    shine = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(shine).ellipse((cx - int(rr * 0.55), cy - int(rr * 0.72),
                                  cx + int(rr * 0.28), cy - int(rr * 0.18)),
                                  fill=(255, 255, 255, 82))
    canvas = Image.alpha_composite(canvas, shine.filter(ImageFilter.GaussianBlur(int(w * 0.012))))

    border = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(border).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius,
                                             outline=(255, 255, 255, 46), width=max(1, ss))
    canvas = Image.alpha_composite(canvas, border)
    return canvas.resize((size, size), Image.LANCZOS)


def app_icon_image(size: int = 256) -> Image.Image:
    """Icon for the exe / taskbar (rounded square, record disc)."""
    return logo(size)
