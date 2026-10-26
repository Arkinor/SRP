"""Glass backdrop rendering plus a small canvas widget kit."""
from __future__ import annotations

import math
import time
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Iterable, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageTk

from . import icons
from .theme import (
    ACCENT, ACCENT_DEEP, BLUE, DIM, FAINT, GREEN, MUTED, PURPLE, RED, TEXT, TEXT_SOFT,
    Theme, WHITE, YELLOW, blend, button_face, glow, gradient, rounded_mask, shade, solid, toggle_face,
    slider_thumb,
)

SS = 2  # supersampling of the baked background


# ----------------------------------------------------------------------
# Background
# ----------------------------------------------------------------------
@dataclass
class Panel:
    x: float
    y: float
    width: float
    height: float
    radius: float = 22
    tone: str = "glass"     # glass | dim | raised | popup


def _blend_white(arr: np.ndarray, alpha: float) -> np.ndarray:
    return arr * (1.0 - alpha) + 255.0 * alpha


def _panel_border(width: int, height: int, radius: int, top_alpha: int = 92,
                  bottom_alpha: int = 26, line: int = 2) -> Image.Image:
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius,
                          outline=(255, 255, 255, 255), width=line)
    arr = np.asarray(layer).astype(np.float32)
    ramp = np.linspace(1.0, 0.0, height, dtype=np.float32)[:, None]
    ramp = ramp ** 0.65
    arr[:, :, 3] *= (bottom_alpha + (top_alpha - bottom_alpha) * ramp) / 255.0
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def _soft_shadow(width: int, height: int, radius: int, blur: int, alpha: int,
                 offset_y: int = 0) -> Image.Image:
    pad = blur * 2
    layer = Image.new("RGBA", (width + pad * 2, height + pad * 2 + offset_y), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle((pad, pad + offset_y, pad + width, pad + offset_y + height),
                           radius=radius, fill=(0, 0, 0, alpha))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def build_background(width: int, height: int, theme: Theme,
                     panels: Sequence[Panel] = (), header_line: Optional[float] = None,
                     footer_line: Optional[float] = None) -> Image.Image:
    """Bake the mesh gradient, glass cards, hairlines and grain into one image."""
    s = theme.s
    W, H = int(width) * SS, int(height) * SS

    # --- mesh gradient at low resolution -----------------------------
    lw, lh = max(12, W // 24), max(12, H // 24)
    ys, xs = np.mgrid[0:lh, 0:lw].astype(np.float32)
    xn = (xs + 0.5) / lw
    yn = (ys + 0.5) / lh
    base = np.zeros((lh, lw, 3), dtype=np.float32)
    base[:, :, 0] = 7.0
    base[:, :, 1] = 8.5
    base[:, :, 2] = 15.0

    blobs = [
        (0.06, 0.02, 0.62, 0.48, (74, 34, 196), 0.95),
        (0.96, 0.16, 0.52, 0.46, (10, 104, 132), 0.82),
        (0.18, 1.02, 0.55, 0.52, (176, 32, 84), 0.72),
        (0.74, 0.94, 0.62, 0.55, (30, 62, 188), 0.65),
        (0.52, 0.42, 0.42, 0.40, (96, 24, 128), 0.34),
        (0.86, 0.62, 0.30, 0.30, (24, 154, 148), 0.28),
    ]
    for bx, by, rx, ry, color, strength in blobs:
        dist = ((xn - bx) / rx) ** 2 + ((yn - by) / ry) ** 2
        falloff = np.exp(-dist * 2.4) * strength
        for i in range(3):
            base[:, :, i] += color[i] * falloff

    # subtle top light
    top = np.exp(-((yn - 0.01) / 0.30) ** 2) * 0.09
    base += top[:, :, None] * 255.0

    # vignette
    dx = (xn - 0.5) * 1.32
    dy = (yn - 0.5) * 1.22
    vign = np.clip(1.0 - (dx * dx + dy * dy) * 0.42, 0.0, 1.0)
    base *= (0.58 + 0.42 * vign)[:, :, None]

    base = np.clip(base, 0, 255).astype(np.uint8)
    image = Image.fromarray(base, "RGB").resize((W, H), Image.BICUBIC)
    image = image.filter(ImageFilter.GaussianBlur(1.2 * SS))

    # --- cards ---------------------------------------------------------
    for panel in panels:
        x, y = int(panel.x * s), int(panel.y * s)
        w, h = max(2, int(panel.width * s)), max(2, int(panel.height * s))
        radius = max(2, int(panel.radius * s))
        rx, ry, rw, rh = x * SS, y * SS, w * SS, h * SS
        rx = max(0, min(rx, W - 2))
        ry = max(0, min(ry, H - 2))
        rw = min(rw, W - rx)
        rh = min(rh, H - ry)
        if rw < 4 or rh < 4:
            continue

        # drop shadow
        shadow = _soft_shadow(rw, rh, radius * SS, max(6, 12 * SS), 120, 4 * SS)
        pad = max(6, 12 * SS) * 2
        image.paste(Image.new("RGB", shadow.size, (0, 0, 0)),
                    (rx - pad, ry - pad), shadow)

        crop = image.crop((rx, ry, rx + rw, ry + rh)).filter(
            ImageFilter.GaussianBlur(max(2, int(9 * SS))))
        arr = np.asarray(crop).astype(np.float32)
        if panel.tone == "dim":
            arr *= 0.88
            arr = _blend_white(arr, 0.018)
        elif panel.tone == "raised":
            arr *= 1.06
            arr = _blend_white(arr, 0.055)
        else:
            arr *= 1.04
            arr = _blend_white(arr, 0.038)
        crop = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")

        mask = rounded_mask(rw, rh, radius * SS)
        tile = Image.new("RGBA", (rw, rh), (0, 0, 0, 0))
        tile.paste(crop, (0, 0), mask)

        # gentle inner sheen at the top
        sheen = gradient((rw, rh), (255, 255, 255, 16), (255, 255, 255, 0))
        tile = Image.alpha_composite(tile, Image.composite(
            sheen, Image.new("RGBA", (rw, rh), (0, 0, 0, 0)), mask))

        tile = Image.alpha_composite(tile, _panel_border(rw, rh, radius * SS))
        image.paste(tile, (rx, ry), tile)

    # --- hairlines -----------------------------------------------------
    draw = ImageDraw.Draw(image, "RGBA")
    if header_line is not None:
        y = int(header_line * s) * SS
        draw.line([(int(24 * s) * SS, y), (W - int(24 * s) * SS, y)],
                  fill=(255, 255, 255, 20), width=max(1, SS))
    if footer_line is not None:
        y = int(footer_line * s) * SS
        draw.line([(int(24 * s) * SS, y), (W - int(24 * s) * SS, y)],
                  fill=(255, 255, 255, 16), width=max(1, SS))

    image = image.resize((width, height), Image.LANCZOS)

    # --- grain ---------------------------------------------------------
    arr = np.asarray(image).astype(np.int16)
    rng = np.random.default_rng(7)
    noise = rng.normal(0, 2.1, size=(arr.shape[0], arr.shape[1], 1)).astype(np.int16)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


# ----------------------------------------------------------------------
# Small standalone glass tiles (rows, popups, modals)
# ----------------------------------------------------------------------
@dataclass
class PopupItem:
    label: str
    value: object = None
    sub: str = ""
    icon: Optional[str] = None
    selected: bool = False


@dataclass
class Hit:
    rect: tuple[int, int, int, int]
    on_click: Optional[Callable] = None
    cursor: str = "hand2"
    on_hover: Optional[Callable] = None
    on_press: Optional[Callable] = None
    on_release: Optional[Callable] = None
    on_drag: Optional[Callable] = None
    interactive: bool = True

    def contains(self, x: int, y: int) -> bool:
        rx, ry, rw, rh = self.rect
        return rx <= x <= rx + rw and ry <= y <= ry + rh


class Button:
    def __init__(self, ui: "UI", item: int, hit: Hit, states: dict, icon_item, label_item):
        self.ui = ui
        self.item = item
        self.hit = hit
        self.states = states
        self.icon_item = icon_item
        self.label_item = label_item
        self._state = 0

    def set_state(self, state: int) -> None:
        if state == self._state:
            return
        self._state = state
        try:
            self.ui.canvas.itemconfig(self.item, image=self.states[state])
        except Exception:
            pass


# ----------------------------------------------------------------------
# UI surface
# ----------------------------------------------------------------------
class UI:
    def __init__(self, canvas: tk.Canvas, theme: Theme, size: tuple[int, int]):
        self.canvas = canvas
        self.t = theme
        self.w, self.h = size
        self.hits: list[Hit] = []
        self._photos: dict[int, tuple[ImageTk.PhotoImage, Image.Image]] = {}
        self._font_cache: dict[tuple, tkfont.Font] = {}
        self.menu_rect: Optional[tuple[int, int, int, int]] = None
        self.menu_offset = 0

    # ---- lifecycle ----------------------------------------------------
    def clear(self) -> None:
        self.canvas.delete("all")
        self.hits.clear()
        self._photos.clear()
        self.menu_rect = None

    def photo(self, image: Image.Image) -> ImageTk.PhotoImage:
        key = id(image)
        cached = self._photos.get(key)
        if cached is None:
            cached = (ImageTk.PhotoImage(image), image)
            self._photos[key] = cached
        return cached[0]

    def draw_image(self, x: float, y: float, image: Image.Image, anchor: str = "nw") -> int:
        return self.canvas.create_image(int(x), int(y), anchor=anchor, image=self.photo(image))

    def background(self, image: Image.Image) -> None:
        self.draw_image(0, 0, image)

    # ---- text ---------------------------------------------------------
    def measure(self, text: str, size: float, weight: str = "normal") -> int:
        key = self.t.font(size, weight)
        font = self._font_cache.get(key)
        if font is None:
            font = tkfont.Font(font=key)
            self._font_cache[key] = font
        return font.measure(text)

    def fit(self, text: str, max_width: int, size: float, weight: str = "normal") -> str:
        if not text:
            return text
        if self.measure(text, size, weight) <= max_width:
            return text
        ellipsis = "…"
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.measure(text[:mid] + ellipsis, size, weight) <= max_width:
                lo = mid + 1
            else:
                hi = mid
        return text[:max(0, lo - 1)].rstrip() + ellipsis

    def text(self, x: float, y: float, text: str, size: float = 12, color: str = TEXT,
             weight: str = "normal", anchor: str = "w", max_width: Optional[int] = None,
             justify: str = "left") -> int:
        if max_width is not None:
            text = self.fit(text, max_width, size, weight)
        return self.canvas.create_text(
            int(x), int(y), text=text, fill=color, anchor=anchor,
            font=self.t.font(size, weight), justify=justify,
        )

    def text_block(self, x: float, y: float, text: str, size: float = 12,
                   color: str = TEXT, weight: str = "normal", max_width: int = 300,
                   line_gap: float = 1.35) -> int:
        """Simple word wrap rendered as several canvas text items, left aligned."""
        font = self._font_cache.get(self.t.font(size, weight))
        if font is None:
            font = tkfont.Font(font=self.t.font(size, weight))
            self._font_cache[self.t.font(size, weight)] = font
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip()
            if font.measure(candidate) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        line_h = int(size * self.t.s * line_gap)
        for index, line in enumerate(lines):
            self.canvas.create_text(int(x), int(y + index * line_h), text=line,
                                    fill=color, anchor="nw", font=self.t.font(size, weight))
        return len(lines) * line_h

    def icon(self, x: float, y: float, name: str, size: float, color: str = TEXT,
             alpha: int = 255, anchor: str = "center") -> int:
        image = icons.icon(name, max(6, self.t.px(size)), color, alpha)
        return self.draw_image(x, y, image, anchor)

    # ---- hits ---------------------------------------------------------
    def hit(self, rect, on_click=None, cursor: str = "hand2", on_hover=None,
            on_press=None, on_release=None, on_drag=None,
            interactive: bool = True) -> Hit:
        hit = Hit(tuple(int(v) for v in rect), on_click, cursor, on_hover,
                  on_press, on_release, on_drag, interactive)
        self.hits.append(hit)
        return hit

    # ---- widgets ------------------------------------------------------
    def button(self, rect, label: str = "", kind: str = "secondary", icon: Optional[str] = None,
               icon_size: float = 15, on_click: Optional[Callable] = None,
               radius: float = 12, size: float = 12.5, weight: str = "semibold",
               gap: float = 8, anchor: str = "center", enabled: bool = True) -> Button:
        x, y, w, h = [int(v) for v in rect]
        radius_px = max(2, self.t.px(radius))
        faces = {0: button_face(w, h, radius_px, kind, 0),
                 1: button_face(w, h, radius_px, kind, 1),
                 2: button_face(w, h, radius_px, kind, 2)}
        if not enabled:
            faces = {0: button_face(w, h, radius_px, "ghost", 0),
                     1: button_face(w, h, radius_px, "ghost", 0),
                     2: button_face(w, h, radius_px, "ghost", 0)}
        item = self.draw_image(x, y, faces[0])

        color = TEXT
        if kind == "primary":
            color = "#FFFFFF"
        elif kind == "danger":
            color = "#FFA3AA"
        elif kind == "ghost":
            color = MUTED
        if not enabled:
            color = FAINT

        icon_item = None
        label_item = None
        text_w = self.measure(label, size, weight) if label else 0
        icon_px = self.t.px(icon_size)
        group = text_w + (icon_px + self.t.px(gap) if icon else 0)
        if anchor == "center":
            start = x + (w - group) / 2
        elif anchor == "left":
            start = x + self.t.px(gap) + self.t.px(6)
        else:
            start = x + w - group - self.t.px(gap) - self.t.px(6)
        text_x = start
        if icon:
            icon_item = self.icon(start + icon_px / 2, y + h / 2, icon, icon_size, color)
            text_x = start + icon_px + self.t.px(gap)
        if label:
            label_item = self.text(text_x, y + h / 2 + 0.5, label, size, color, weight, anchor="w")

        button = Button(self, item, None, faces, icon_item, label_item)

        def hover(state: bool) -> None:
            button.set_state(1 if state else 0)

        hit = self.hit((x, y, w, h), on_click if enabled else None,
                       on_hover=hover if enabled else None,
                       on_press=lambda *_: button.set_state(2) if enabled else None,
                       on_release=lambda *_: button.set_state(1 if enabled else 0))
        button.hit = hit

        def set_enabled(value: bool) -> None:
            hit.on_click = on_click if value else None
            if label_item is not None:
                self.canvas.itemconfig(label_item, fill=color if value else FAINT)
        button.set_enabled = set_enabled  # type: ignore[attr-defined]
        return button

    def icon_button(self, cx: float, cy: float, diameter: float, icon: str,
                    on_click: Optional[Callable] = None, kind: str = "secondary",
                    icon_color: str = TEXT, icon_size: Optional[float] = None) -> Button:
        d = self.t.px(diameter)
        return self.button((cx - d / 2, cy - d / 2, d, d), "", kind, icon,
                           icon_size or diameter * 0.42, on_click,
                           radius=diameter / 2, gap=0)

    def toggle(self, x: float, y: float, value: bool,
               on_change: Optional[Callable[[bool], None]] = None,
               width: float = 46, height: float = 27) -> int:
        w, h = self.t.px(width), self.t.px(height)
        image = toggle_face(w, h, value)
        item = self.draw_image(x, y, image)

        def click() -> None:
            new_value = not value
            self.canvas.itemconfig(item, image=self.photo(toggle_face(w, h, new_value)))
            if on_change:
                on_change(new_value)

        self.hit((x, y, w, h), click)
        return item

    def segmented(self, rect, options: Sequence[str], index: int,
                  on_change: Optional[Callable[[int], None]], radius: float = 11,
                  size: float = 11.5, weight: str = "semibold",
                  icons_list: Optional[Sequence[str]] = None) -> None:
        x, y, w, h = [int(v) for v in rect]
        r = self.t.px(radius)
        self.draw_image(x, y, button_face(w, h, r, "secondary", 0))
        pad = max(2, self.t.px(3))
        gap = max(1, self.t.px(2))
        count = max(1, len(options))
        seg_w = (w - pad * 2 - gap * (count - 1)) / count
        seg_h = h - pad * 2
        for i, label in enumerate(options):
            sx = x + pad + i * (seg_w + gap)
            if i == index:
                self.draw_image(sx, y + pad, button_face(int(seg_w), int(seg_h),
                                                         r - pad // 2, "selected", 0))
            cx = sx + seg_w / 2
            color = TEXT if i == index else MUTED
            tw = self.measure(label, size, weight)
            icon_name = icons_list[i] if icons_list and i < len(icons_list) else None
            if icon_name:
                icon_px = self.t.px(size + 1)
                total = tw + icon_px + self.t.px(5)
                ix = cx - total / 2 + icon_px / 2
                self.icon(ix, y + h / 2, icon_name, size + 1, color)
                text_item = self.text(ix + icon_px / 2 + self.t.px(5), y + h / 2 + 0.5,
                                      label, size, color, weight)
            else:
                text_item = self.text(cx, y + h / 2 + 0.5, label, size, color,
                                      weight, anchor="center")

            def hover(state: bool, item=text_item, selected=(i == index)) -> None:
                if selected:
                    return
                self.canvas.itemconfig(item, fill=TEXT_SOFT if state else MUTED)

            def click(target=i) -> None:
                if target != index and on_change:
                    on_change(target)

            self.hit((sx, y + pad, seg_w, seg_h), click, on_hover=hover)

    def field(self, rect, value: str, sub: str = "", icon: Optional[str] = None,
              on_click: Optional[Callable] = None, chevron: bool = True,
              size: float = 12, enabled: bool = True) -> Button:
        x, y, w, h = [int(v) for v in rect]
        r = self.t.px(11)
        faces = {0: button_face(w, h, r, "secondary", 0),
                 1: button_face(w, h, r, "secondary", 1),
                 2: button_face(w, h, r, "secondary", 2)}
        if not enabled:
            faces = {0: button_face(w, h, r, "ghost", 0),
                     1: button_face(w, h, r, "ghost", 0),
                     2: button_face(w, h, r, "ghost", 0)}
        item = self.draw_image(x, y, faces[0])
        left = x + self.t.px(12)
        if icon:
            self.icon(left + self.t.px(8), y + h / 2, icon, size + 2,
                      MUTED if enabled else FAINT)
            left += self.t.px(24)
        right = x + w - self.t.px(12)
        if chevron:
            self.icon(right - self.t.px(7), y + h / 2, "chevron_down", 9.5,
                      MUTED if enabled else FAINT)
            right -= self.t.px(22)
        color = TEXT if enabled else FAINT
        if sub:
            self.text(left, y + h / 2 - self.t.px(7), value, size, color, "semibold",
                      max_width=int(right - left))
            self.text(left, y + h / 2 + self.t.px(8.5), sub, size - 2.4, DIM, "normal",
                      max_width=int(right - left))
        else:
            self.text(left, y + h / 2 + 0.5, value, size, color, "semibold",
                      max_width=int(right - left))

        button = Button(self, item, None, faces, None, None)

        def hover(state: bool) -> None:
            button.set_state(1 if state else 0)

        hit = self.hit((x, y, w, h), on_click if enabled else None,
                       on_hover=hover if enabled else None,
                       on_press=lambda *_: button.set_state(2) if enabled else None,
                       on_release=lambda *_: button.set_state(1 if enabled else 0))
        button.hit = hit
        return button

    def slider(self, rect, value: float, on_change: Optional[Callable[[float], None]],
               minimum: float = 0.0, maximum: float = 2.0, step: float = 0.05,
               color: str = BLUE, enabled: bool = True) -> dict:
        x, y, w, h = [int(v) for v in rect]
        cy = y + h // 2
        track_x0, track_x1 = x, x + w
        thumb_d = self.t.px(17)
        if not enabled:
            color = "#4C5468"
        self.canvas.create_line(track_x0, cy, track_x1, cy, fill="#3A4256",
                                width=max(2, self.t.px(4)), capstyle=tk.ROUND)
        fill_item = self.canvas.create_line(track_x0, cy, track_x0 + 1, cy, fill=color,
                                            width=max(2, self.t.px(4)), capstyle=tk.ROUND)
        thumb = slider_thumb(thumb_d)
        thumb_item = self.draw_image(0, 0, thumb)
        side = thumb.size[0]

        state = {"value": value, "dragging": False}

        def thumb_pos(v: float) -> float:
            ratio = (v - minimum) / max(1e-6, maximum - minimum)
            return track_x0 + max(0.0, min(1.0, ratio)) * (track_x1 - track_x0)

        def render(v: float) -> None:
            px = thumb_pos(v)
            self.canvas.coords(fill_item, track_x0, cy, max(track_x0 + 1, px), cy)
            self.canvas.coords(thumb_item, px - side / 2, cy - side / 2)

        def apply(xpos: float, notify: bool = True) -> None:
            ratio = max(0.0, min(1.0, (xpos - track_x0) / max(1, track_x1 - track_x0)))
            v = minimum + ratio * (maximum - minimum)
            if step > 0:
                v = round(v / step) * step
            v = max(minimum, min(maximum, v))
            state["value"] = v
            render(v)
            if notify and on_change:
                on_change(v)

        def press(px: int, py: int) -> None:
            state["dragging"] = True
            apply(px)

        def drag(px: int, py: int) -> None:
            if state["dragging"]:
                apply(px)

        def release(px: int, py: int) -> None:
            state["dragging"] = False

        render(value)
        hit = self.hit((x, y - h // 2, w, h * 2), None, cursor="hand2" if enabled else "arrow",
                       on_press=press if enabled else None,
                       on_drag=drag if enabled else None,
                       on_release=release if enabled else None)

        def set_value(v: float) -> None:
            state["value"] = v
            render(v)
        return {"set": set_value, "hit": hit, "thumb": thumb_item, "fill": fill_item}

    def meter(self, rect, level: float, active: bool = True) -> dict:
        x, y, w, h = [int(v) for v in rect]
        cy = y + h // 2
        width = max(2, self.t.px(4))
        track = self.canvas.create_line(x, cy, x + w, cy, fill="#333A4C", width=width,
                                        capstyle=tk.ROUND)
        if not active:
            bar = self.canvas.create_line(x, cy, x + 1, cy, fill="#333A4C", width=width,
                                          capstyle=tk.ROUND)
            return {"track": track, "bar": bar}

        norm = max(0.0, min(1.0, level ** 0.55))
        color = GREEN if norm < 0.72 else (YELLOW if norm < 0.9 else RED)
        bar = self.canvas.create_line(x, cy, x + max(1, int(w * norm)), cy, fill=color,
                                      width=width, capstyle=tk.ROUND)
        if norm < 0.01:
            self.canvas.itemconfig(bar, state="hidden")
        return {"track": track, "bar": bar, "color": color}

    def update_meter(self, meter: dict, level: float) -> None:
        canvas = self.canvas
        x0, cy, _x1, _y1 = canvas.coords(meter["track"])
        x1 = canvas.coords(meter["track"])[2]
        norm = max(0.0, min(1.0, level ** 0.55))
        color = GREEN if norm < 0.72 else (YELLOW if norm < 0.9 else RED)
        canvas.coords(meter["bar"], x0, cy, x0 + max(1, int((x1 - x0) * norm)), cy)
        canvas.itemconfig(meter["bar"], state="normal" if norm >= 0.01 else "hidden")
        if color != meter.get("color"):
            canvas.itemconfig(meter["bar"], fill=color)
            meter["color"] = color

    # ---- popups -------------------------------------------------------
    def menu(self, anchor_rect, items: Sequence[PopupItem], on_pick: Callable,
             selected: object = None, title: str = "", offset: int = 0,
             on_dismiss: Optional[Callable] = None, width: Optional[float] = None,
             max_rows: int = 8) -> None:
        ax, ay, aw, ah = [int(v) for v in anchor_rect]
        row_h = self.t.px(38)
        pad = self.t.px(7)
        title_h = self.t.px(30) if title else 0
        visible = min(len(items), max_rows)
        thumb = len(items) > visible
        menu_w = int(width if width else max(aw, self.t.px(230)))
        menu_w = min(menu_w, self.w - self.t.px(24))
        menu_h = pad * 2 + title_h + visible * row_h
        mx = min(max(self.t.px(12), ax), self.w - menu_w - self.t.px(12))
        below = ay + ah + self.t.px(6)
        if below + menu_h > self.h - self.t.px(12):
            my = ay - menu_h - self.t.px(6)
        else:
            my = below
        my = max(self.t.px(12), min(my, self.h - menu_h - self.t.px(12)))

        # full-surface dismiss layer
        self.hit((0, 0, self.w, self.h), on_dismiss, cursor="arrow", interactive=True)

        shadow_pad = self.t.px(16)
        surface = Image.new("RGBA", (menu_w + shadow_pad * 2, menu_h + shadow_pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        draw.rounded_rectangle((shadow_pad, shadow_pad + 2, shadow_pad + menu_w, shadow_pad + menu_h + 2),
                               radius=self.t.px(14), fill=(0, 0, 0, 130))
        surface = surface.filter(ImageFilter.GaussianBlur(self.t.px(10)))
        body_mask = rounded_mask(menu_w, menu_h, self.t.px(14))
        body = Image.new("RGBA", (menu_w, menu_h), (17, 19, 30, 246))
        body = Image.alpha_composite(body, _glass_sheen(menu_w, menu_h, self.t.px(14)))
        body = Image.alpha_composite(body, _glass_outline(menu_w, menu_h, self.t.px(14), 70, 34))
        surface.paste(body, (shadow_pad, shadow_pad), body_mask)
        self.draw_image(mx - shadow_pad, my - shadow_pad, surface)

        self.menu_rect = (mx, my, menu_w, menu_h)
        self.menu_offset = offset

        if title:
            self.text(mx + self.t.px(16), my + pad + title_h / 2, title,
                      10.5, DIM, "semibold")

        start_index = max(0, min(offset, max(0, len(items) - visible)))
        for row in range(visible):
            index = start_index + row
            if index >= len(items):
                break
            item = items[index]
            ry = my + pad + title_h + row * row_h
            rect = (mx + self.t.px(6), ry + 1, menu_w - self.t.px(12), row_h - 2)
            hover_img = button_face(rect[2], rect[3], self.t.px(9), "ghost", 1)
            hover_item = self.draw_image(rect[0], rect[1], hover_img)
            self.canvas.itemconfig(hover_item, state="hidden")

            text_x = mx + self.t.px(16)
            if item.icon:
                self.icon(text_x + self.t.px(8), ry + row_h / 2, item.icon, 13, MUTED)
                text_x += self.t.px(26)
            is_selected = item.selected or (selected is not None and item.value == selected)
            if item.sub:
                self.text(text_x, ry + row_h / 2 - self.t.px(7), item.label, 11.5,
                          TEXT if is_selected else TEXT_SOFT, "semibold",
                          max_width=int(menu_w - (text_x - mx) - self.t.px(56)))
                self.text(text_x, ry + row_h / 2 + self.t.px(7.5), item.sub, 9.2,
                          DIM, max_width=int(menu_w - (text_x - mx) - self.t.px(56)))
            else:
                self.text(text_x, ry + row_h / 2 + 0.5, item.label, 11.5,
                          TEXT if is_selected else TEXT_SOFT, "semibold",
                          max_width=int(menu_w - (text_x - mx) - self.t.px(52)))
            if is_selected:
                self.icon(mx + menu_w - self.t.px(20), ry + row_h / 2, "check", 12, BLUE)

            def enter(item=hover_item) -> None:
                self.canvas.itemconfig(item, state="normal")

            def leave(item=hover_item) -> None:
                self.canvas.itemconfig(item, state="hidden")

            self.hit(rect, lambda value=item.value, idx=index: on_pick(idx), on_hover=None)
            self.hits[-1].on_hover = lambda state, e=enter, l=leave: (e() if state else l())

        if thumb:
            track_x = mx + menu_w - self.t.px(8)
            track_y0 = my + pad + title_h + self.t.px(4)
            track_y1 = my + menu_h - pad - self.t.px(4)
            self.canvas.create_line(track_x, track_y0, track_x, track_y1,
                                    fill=solid("#FFFFFF22"), width=max(1, self.t.px(2)),
                                    capstyle=tk.ROUND)
            ratio = visible / len(items)
            thumb_h = max(self.t.px(18), (track_y1 - track_y0) * ratio)
            start = start_index / max(1, len(items) - visible)
            ty = track_y0 + (track_y1 - track_y0 - thumb_h) * start
            self.canvas.create_line(track_x, ty, track_x, ty + thumb_h,
                                    fill=solid("#FFFFFF66"), width=max(1, self.t.px(2)),
                                    capstyle=tk.ROUND)

    # ---- overlays -----------------------------------------------------
    def dim(self) -> None:
        image = _dim_image(self.w, self.h)
        self.draw_image(0, 0, image)
        self.hit((0, 0, self.w, self.h), None, cursor="arrow")

    def modal(self, title: str, message: str, buttons: Sequence[tuple[str, str, Callable]],
              width: float = 430) -> None:
        self.dim()
        w = self.t.px(width)
        pad = self.t.px(24)
        wrapped = _wrap(message, self.fit, self, self.t.px(width - 56), 12.5)
        lines_h = len(wrapped) * int(12.5 * self.t.s * 1.45)
        h = pad + self.t.px(22) + self.t.px(10) + lines_h + self.t.px(22) + self.t.px(40) + pad
        x = (self.w - w) // 2
        y = (self.h - h) // 2
        shadow_pad = self.t.px(22)
        surface = Image.new("RGBA", (w + shadow_pad * 2, h + shadow_pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        draw.rounded_rectangle((shadow_pad, shadow_pad + self.t.px(4),
                                shadow_pad + w, shadow_pad + h + self.t.px(4)),
                               radius=self.t.px(18), fill=(0, 0, 0, 150))
        surface = surface.filter(ImageFilter.GaussianBlur(self.t.px(14)))
        mask = rounded_mask(w, h, self.t.px(18))
        body = Image.new("RGBA", (w, h), (20, 22, 34, 250))
        body = Image.alpha_composite(body, _glass_sheen(w, h, self.t.px(18)))
        body = Image.alpha_composite(body, _glass_outline(w, h, self.t.px(18), 80, 36))
        surface.paste(body, (shadow_pad, shadow_pad), mask)
        self.draw_image(x - shadow_pad, y - shadow_pad, surface)

        self.text(x + pad, y + pad + self.t.px(11), title, 16.5, TEXT, "semibold")
        yy = y + pad + self.t.px(22) + self.t.px(10)
        for line in wrapped:
            self.text(x + pad, yy, line, 12.5, MUTED)
            yy += int(12.5 * self.t.s * 1.45)

        count = max(1, len(buttons))
        bw = min(self.t.px(150), (w - pad * 2 - self.t.px(10) * (count - 1)) // count)
        bx = x + w - pad - (bw * count + self.t.px(10) * (count - 1))
        by = y + h - pad - self.t.px(40)
        for i, (label, kind, callback) in enumerate(buttons):
            self.button((bx + i * (bw + self.t.px(10)), by, bw, self.t.px(40)),
                        label, kind, on_click=callback, size=12, radius=11)

    def progress_card(self, title: str, subtitle: str, width: float = 380) -> dict:
        self.dim()
        w = self.t.px(width)
        h = self.t.px(136)
        x = (self.w - w) // 2
        y = (self.h - h) // 2
        shadow_pad = self.t.px(20)
        surface = Image.new("RGBA", (w + shadow_pad * 2, h + shadow_pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        draw.rounded_rectangle((shadow_pad, shadow_pad + self.t.px(4),
                                shadow_pad + w, shadow_pad + h + self.t.px(4)),
                               radius=self.t.px(18), fill=(0, 0, 0, 150))
        surface = surface.filter(ImageFilter.GaussianBlur(self.t.px(13)))
        mask = rounded_mask(w, h, self.t.px(18))
        body = Image.new("RGBA", (w, h), (20, 22, 34, 250))
        body = Image.alpha_composite(body, _glass_sheen(w, h, self.t.px(18)))
        body = Image.alpha_composite(body, _glass_outline(w, h, self.t.px(18), 80, 36))
        surface.paste(body, (shadow_pad, shadow_pad), mask)
        self.draw_image(x - shadow_pad, y - shadow_pad, surface)

        self.text(x + self.t.px(24), y + self.t.px(30), title, 16, TEXT, "semibold")
        self.text(x + self.t.px(24), y + self.t.px(54), subtitle, 11.5, MUTED,
                  max_width=w - self.t.px(48))
        bar_x = x + self.t.px(24)
        bar_y = y + h - self.t.px(40)
        bar_w = w - self.t.px(48)
        self.canvas.create_line(bar_x, bar_y, bar_x + bar_w, bar_y, fill="#333A4C",
                                width=self.t.px(6), capstyle=tk.ROUND)
        fill = self.canvas.create_line(bar_x, bar_y, bar_x + 2, bar_y, fill=ACCENT,
                                       width=self.t.px(6), capstyle=tk.ROUND)
        percent = self.text(x + w - self.t.px(24), y + self.t.px(31), "", 11.5, MUTED,
                            "semibold", anchor="e")
        return {"fill": fill, "percent": percent, "x": bar_x, "y": bar_y, "w": bar_w}

    def update_progress(self, card: dict, value: float) -> None:
        value = max(0.0, min(1.0, value))
        x, w, y = card["x"], card["w"], card["y"]
        self.canvas.coords(card["fill"], x, y, x + max(2, w * value), y)
        self.canvas.itemconfig(card["percent"], text=f"{int(value * 100)}%")

    def wait_card(self, title: str, subtitle: str, icon: str = "clock",
                  width: float = 380) -> None:
        self.dim()
        w = self.t.px(width)
        h = self.t.px(118)
        x = (self.w - w) // 2
        y = (self.h - h) // 2
        shadow_pad = self.t.px(20)
        surface = Image.new("RGBA", (w + shadow_pad * 2, h + shadow_pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        draw.rounded_rectangle((shadow_pad, shadow_pad + self.t.px(4),
                                shadow_pad + w, shadow_pad + h + self.t.px(4)),
                               radius=self.t.px(18), fill=(0, 0, 0, 150))
        surface = surface.filter(ImageFilter.GaussianBlur(self.t.px(13)))
        mask = rounded_mask(w, h, self.t.px(18))
        body = Image.new("RGBA", (w, h), (20, 22, 34, 250))
        body = Image.alpha_composite(body, _glass_sheen(w, h, self.t.px(18)))
        body = Image.alpha_composite(body, _glass_outline(w, h, self.t.px(18), 80, 36))
        surface.paste(body, (shadow_pad, shadow_pad), mask)
        self.draw_image(x - shadow_pad, y - shadow_pad, surface)

        self.icon(x + self.t.px(34), y + h / 2, icon, 22, MUTED)
        self.text(x + self.t.px(60), y + h / 2 - self.t.px(12), title, 15.5, TEXT,
                  "semibold")
        self.text(x + self.t.px(60), y + h / 2 + self.t.px(12), subtitle, 11, DIM,
                  max_width=w - self.t.px(84))

    def toast(self, message: str, icon: str = "check", color: str = GREEN,
              width: Optional[float] = None) -> None:
        size = 11.5
        text_w = self.measure(message, size, "semibold")
        w = int(width or min(self.w - self.t.px(48), text_w + self.t.px(78)))
        h = self.t.px(44)
        x = (self.w - w) // 2
        y = self.h - self.t.px(26) - h
        pad = self.t.px(12)
        surface = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
        draw = ImageDraw.Draw(surface)
        draw.rounded_rectangle((pad, pad + self.t.px(3), pad + w, pad + h + self.t.px(3)),
                               radius=h // 2, fill=(0, 0, 0, 120))
        surface = surface.filter(ImageFilter.GaussianBlur(self.t.px(9)))
        mask = rounded_mask(w, h, h // 2)
        body = Image.new("RGBA", (w, h), (22, 24, 36, 248))
        body = Image.alpha_composite(body, _glass_sheen(w, h, h // 2))
        body = Image.alpha_composite(body, _glass_outline(w, h, h // 2, 80, 36))
        surface.paste(body, (pad, pad), mask)
        self.draw_image(x - pad, y - pad, surface)
        self.icon(x + self.t.px(20), y + h / 2, icon, 14, color)
        self.text(x + self.t.px(36), y + h / 2 + 0.5, message, size, TEXT_SOFT, "semibold",
                  max_width=w - self.t.px(48))


# ----------------------------------------------------------------------
# Cached helper images
# ----------------------------------------------------------------------
def _glass_sheen(w: int, h: int, radius: int) -> Image.Image:
    layer = gradient((w, h), (255, 255, 255, 14), (255, 255, 255, 0))
    return Image.composite(layer, Image.new("RGBA", (w, h), (0, 0, 0, 0)),
                           rounded_mask(w, h, radius))


def _glass_outline(w: int, h: int, radius: int, top: int, bottom: int) -> Image.Image:
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius,
                                            outline=(255, 255, 255, 255), width=1)
    arr = np.asarray(layer).astype(np.float32)
    ramp = np.linspace(1.0, 0.0, h, dtype=np.float32)[:, None]
    arr[:, :, 3] *= (bottom + (top - bottom) * ramp) / 255.0
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


_dim_cache: dict[tuple[int, int], Image.Image] = {}


def _dim_image(w: int, h: int) -> Image.Image:
    key = (w, h)
    image = _dim_cache.get(key)
    if image is None:
        image = Image.new("RGBA", (w, h), (4, 5, 10, 168))
        if len(_dim_cache) > 4:
            _dim_cache.clear()
        _dim_cache[key] = image
    return image


@lru_cache(maxsize=256)
def tile_image(w: int, h: int, radius: int, tone: str = "glass") -> Image.Image:
    """Standalone translucent tile (rows, chips, placeholders)."""
    w, h = max(2, int(w)), max(2, int(h))
    ss = 2 if max(w, h) <= 700 else 1
    W, H = w * ss, h * ss
    fill = {
        "glass": (255, 255, 255, 12),
        "raised": (255, 255, 255, 20),
        "chip": (5, 6, 12, 148),
        "red": (255, 78, 96, 38),
        "dark": (10, 11, 20, 180),
    }.get(tone, (255, 255, 255, 12))
    border = {
        "glass": 30, "raised": 44, "chip": 46, "red": 100, "dark": 44,
    }.get(tone, 30)
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    mask = rounded_mask(W, H, radius * ss)
    image.paste(Image.new("RGBA", (W, H), fill), (0, 0), mask)
    if tone in ("glass", "raised", "dark"):
        sheen = gradient((W, H), (255, 255, 255, 18), (255, 255, 255, 0))
        image = Image.alpha_composite(image, Image.composite(
            sheen, Image.new("RGBA", (W, H), (0, 0, 0, 0)), mask))
    outline = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(outline).rounded_rectangle((0, 0, W - 1, H - 1), radius=radius * ss,
                                             outline=(255, 255, 255, 255), width=max(1, ss))
    arr = np.asarray(outline).astype(np.float32)
    ramp = np.linspace(1.0, 0.25, H, dtype=np.float32)[:, None]
    arr[:, :, 3] *= (border / 255.0) * ramp
    image = Image.alpha_composite(image, Image.fromarray(arr.astype(np.uint8), "RGBA"))
    return image.resize((w, h), Image.LANCZOS) if ss > 1 else image



def _wrap(text: str, fit, ui: UI, width: int, size: float) -> list[str]:
    font_key = ui.t.font(size, "normal")
    font = ui._font_cache.get(font_key)
    if font is None:
        font = tkfont.Font(font=font_key)
        ui._font_cache[font_key] = font
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if font.measure(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


@dataclass
class ButtonState:
    kind: str = "secondary"
    enabled: bool = True
