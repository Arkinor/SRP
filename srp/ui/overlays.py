"""Full-screen region selection overlay."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Optional

from PIL import Image, ImageEnhance, ImageFilter, ImageTk

from .. import winapi
from .kit import UI, tile_image
from .theme import ACCENT, DIM, MUTED, TEXT, TEXT_SOFT, Theme, solid


class RegionSelector:
    def __init__(self, master: tk.Misc, theme: Theme, screenshot: Image.Image,
                 origin: tuple[int, int], on_done: Callable[[dict], None],
                 on_cancel: Optional[Callable[[], None]] = None):
        self.theme = theme
        self.t = theme
        self.origin_x, self.origin_y = origin
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.screenshot = screenshot.convert("RGB")
        self.w, self.h = screenshot.size
        self.start: Optional[tuple[int, int]] = None
        self.rect: Optional[tuple[int, int, int, int]] = None
        self.dragging = False
        self.active = True
        self._last_abs: Optional[tuple[int, int]] = None

        self.top = tk.Toplevel(master)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.geometry(f"{self.w}x{self.h}+{self.origin_x}+{self.origin_y}")
        self.top.configure(bg="#000000")
        self.canvas = tk.Canvas(self.top, width=self.w, height=self.h,
                                highlightthickness=0, bd=0, bg="#05060B",
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self._build_static()
        self._bind()
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()
        try:
            self.top.grab_set()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _build_static(self) -> None:
        dark = ImageEnhance.Brightness(self.screenshot).enhance(0.42)
        dark = dark.filter(ImageFilter.GaussianBlur(self.t.px(2)))
        self.dark_photo = ImageTk.PhotoImage(dark)
        self.canvas.create_image(0, 0, anchor="nw", image=self.dark_photo)

        self.bright_item = self.canvas.create_image(0, 0, anchor="nw")

        # hint panel
        hw, hh = self.t.px(330), self.t.px(70)
        hx = (self.w - hw) // 2
        hy = self.t.px(34)
        tile = tile_image(hw, hh, self.t.px(18), "chip")
        self.canvas.create_image(hx, hy, anchor="nw", image=self._photo_keep(tile))
        self.canvas.create_text(hx + hw // 2, hy + self.t.px(24), text="Выделите область записи",
                                fill=TEXT, font=self.t.font(14, "semibold"))
        self.canvas.create_text(hx + hw // 2, hy + self.t.px(45),
                                text="Перетащите мышь · Esc — отмена",
                                fill=DIM, font=self.t.font(10, "normal"))

        # selection extras
        self.size_bg = self.canvas.create_image(0, 0, anchor="nw", state="hidden")
        self.size_tile = tile_image(self.t.px(126), self.t.px(30), self.t.px(15), "chip")
        self.size_photo = self._photo_keep(self.size_tile)
        self.canvas.itemconfig(self.size_bg, image=self.size_photo)
        self.size_text = self.canvas.create_text(0, 0, text="", fill=TEXT,
                                                 font=self.t.font(11, "semibold"),
                                                 state="hidden")

        self.guide_v = self.canvas.create_line(0, 0, 0, 0, fill=solid("#FFFFFF30"), width=1,
                                               state="hidden")
        self.guide_h = self.canvas.create_line(0, 0, 0, 0, fill=solid("#FFFFFF30"), width=1,
                                               state="hidden")

        # toolbar
        tw, th = self.t.px(336), self.t.px(64)
        tx = (self.w - tw) // 2
        ty = self.h - th - self.t.px(44)
        toolbar_tile = tile_image(tw, th, self.t.px(20), "chip")
        self.toolbar_bg = self.canvas.create_image(tx, ty, anchor="nw",
                                                   image=self._photo_keep(toolbar_tile))

        before = set(self.canvas.find_all())
        self.ui = UI(self.canvas, self.t, (self.w, self.h))
        bw, bh = self.t.px(136), self.t.px(40)
        self.cancel_btn = self.ui.button(
            (tx + self.t.px(12), ty + (th - bh) // 2, bw, bh), "Отмена", "ghost",
            None, 13, self.cancel, size=11, radius=10)
        self.save_btn = self.ui.button(
            (tx + tw - bw - self.t.px(12), ty + (th - bh) // 2, bw, bh),
            "Сохранить", "primary", "check", 13, self.confirm, size=11, radius=10)
        self.save_btn.set_enabled(False)
        for item in set(self.canvas.find_all()) - before:
            self.canvas.addtag_withtag("toolbar", item)
        self.canvas.addtag_withtag("toolbar", self.toolbar_bg)
        self.canvas.tag_raise("toolbar")

    def _photo_keep(self, image: Image.Image) -> ImageTk.PhotoImage:
        photo = ImageTk.PhotoImage(image)
        if not hasattr(self, "_refs"):
            self._refs = []
        self._refs.append(photo)
        return photo

    # ------------------------------------------------------------------
    def _bind(self) -> None:
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<ButtonPress-3>", lambda _e: self.cancel())
        self.top.bind("<Escape>", lambda _e: self.cancel())
        self.top.bind("<Return>", lambda _e: self.confirm())

    def _hit(self, x: int, y: int):
        for hit in reversed(self.ui.hits):
            if hit.contains(x, y):
                return hit
        return None

    def _motion(self, event) -> None:
        hit = self._hit(event.x, event.y)
        self.top.configure(cursor="hand2" if hit else "crosshair")
        old = getattr(self, "_hover", None)
        if old is not hit:
            if old is not None and old.on_hover:
                old.on_hover(False)
            if hit is not None and hit.on_hover:
                hit.on_hover(True)
            self._hover = hit

    # ------------------------------------------------------------------
    def _press(self, event) -> None:
        hit = self._hit(event.x, event.y)
        if hit is not None:
            self._down = hit
            if hit.on_press:
                hit.on_press(event.x, event.y)
            return
        self._down = None
        self.start = (event.x, event.y)
        self.rect = (event.x, event.y, event.x, event.y)
        self.dragging = True
        self._update_guides(event.x, event.y)

    def _drag(self, event) -> None:
        if self._down is not None:
            if self._down.on_drag:
                self._down.on_drag(event.x, event.y)
            return
        if not self.dragging or self.start is None:
            return
        x0, y0 = self.start
        x1 = max(0, min(self.w - 1, event.x))
        y1 = max(0, min(self.h - 1, event.y))
        self.rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self._render_selection()
        self._update_guides(event.x, event.y)

    def _release(self, event) -> None:
        if self._down is not None:
            down = self._down
            self._down = None
            inside = down.contains(event.x, event.y)
            if inside and down.on_click:
                down.on_click()
            if down.on_release:
                down.on_release(event.x, event.y)
            return
        self.dragging = False
        self.canvas.itemconfig(self.guide_v, state="hidden")
        self.canvas.itemconfig(self.guide_h, state="hidden")
        if self.rect:
            x0, y0, x1, y1 = self.rect
            if x1 - x0 < 8 or y1 - y0 < 8:
                self.rect = None
                self.canvas.itemconfig(self.bright_item, state="hidden")
                self.canvas.itemconfig(self.size_bg, state="hidden")
                self.canvas.itemconfig(self.size_text, state="hidden")
                self.save_btn.set_enabled(False)
                return
            self.save_btn.set_enabled(True)

    def _update_guides(self, x: int, y: int) -> None:
        self.canvas.coords(self.guide_v, x, 0, x, self.h)
        self.canvas.coords(self.guide_h, 0, y, self.w, y)
        self.canvas.itemconfig(self.guide_v, state="normal")
        self.canvas.itemconfig(self.guide_h, state="normal")

    def _render_selection(self) -> None:
        if not self.rect:
            return
        x0, y0, x1, y1 = [int(v) for v in self.rect]
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        crop = self.screenshot.crop((x0, y0, x1, y1))
        self._crop_photo = ImageTk.PhotoImage(crop)
        self.canvas.itemconfig(self.bright_item, image=self._crop_photo, state="normal")
        self.canvas.coords(self.bright_item, x0, y0)

        # border + handles
        self.canvas.delete("selection")
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=ACCENT, width=self.t.px(1.6),
                                     tags="selection")
        hs = self.t.px(4)
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            self.canvas.create_rectangle(cx - hs, cy - hs, cx + hs, cy + hs,
                                         fill="#FFFFFF", outline="", tags="selection")

        label = f"{w} × {h}"
        self.canvas.itemconfig(self.size_text, text=label, state="normal")
        sw = self.t.px(126)
        sx = min(max(self.t.px(6), (x0 + x1 - sw) // 2), self.w - sw - self.t.px(6))
        sy = y0 - self.t.px(38)
        if sy < self.t.px(6):
            sy = y1 + self.t.px(8)
        sy = min(sy, self.h - self.t.px(36))
        self.canvas.coords(self.size_bg, sx, sy)
        self.canvas.itemconfig(self.size_bg, state="normal")
        self.canvas.coords(self.size_text, sx + sw // 2, sy + self.t.px(15))
        self.canvas.tag_raise("selection")
        self.canvas.tag_raise(self.size_bg)
        self.canvas.tag_raise(self.size_text)
        self.canvas.tag_raise("toolbar")

    # ------------------------------------------------------------------
    def confirm(self) -> None:
        if not self.rect:
            return
        x0, y0, x1, y1 = [int(v) for v in self.rect]
        w, h = x1 - x0, y1 - y0
        if w < 8 or h < 8:
            return
        rect = {"left": self.origin_x + x0, "top": self.origin_y + y0,
                "width": w, "height": h}
        self._close()
        self.on_done(rect)

    def cancel(self) -> None:
        if not self.active:
            return
        self._close()
        if self.on_cancel:
            self.on_cancel()

    def _close(self) -> None:
        self.active = False
        try:
            self.top.grab_release()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass
