"""Floating recording bar shown above every window while recording.

The bar is a borderless, always-on-top helper window.  It never takes focus
(``WS_EX_NOACTIVATE``) so clicking pause in a game does not interrupt it, and it
is hidden from screen captures (``WDA_EXCLUDEFROMCAPTURE``) so it never appears
in the recording itself.  Positions are persisted in ``Settings.overlay_pos``.
"""
from __future__ import annotations

import tkinter as tk
from typing import Optional

from .. import logger, winapi
from .kit import UI, tile_image
from .theme import ACCENT, TEXT, YELLOW, solid
from .views import fmt_elapsed

LOG = logger.get()

EXPANDED_W, EXPANDED_H = 306, 62
COLLAPSED_W, COLLAPSED_H = 178, 50
MARGIN = 26


class RecordingOverlay:
    def __init__(self, app):
        self.app = app
        self.t = app.t
        self.p = app.t.px
        self.top: Optional[tk.Toplevel] = None
        self.canvas: Optional[tk.Canvas] = None
        self.ui: Optional[UI] = None
        self.hwnd = 0
        self.collapsed = False
        self.paused = False
        self.elapsed = 0.0
        self.timer_item = None
        self._visible = False
        self._x = 0
        self._y = 0
        self._w = 0
        self._h = 0
        self._drag: Optional[tuple[int, int, int, int]] = None
        self._moved = False
        self._active = None
        self._hover = None

    # ------------------------------------------------------------------
    # window
    def _ensure_window(self) -> bool:
        if self.top is not None:
            return True
        try:
            top = tk.Toplevel(self.app.root)
            top.withdraw()
            top.overrideredirect(True)
            top.configure(bg="#05060B")
            top.attributes("-topmost", True)
            canvas = tk.Canvas(top, bd=0, highlightthickness=0, bg="#05060B")
            canvas.pack(fill="both", expand=True)
            canvas.bind("<Motion>", self._on_motion)
            canvas.bind("<ButtonPress-1>", self._on_press)
            canvas.bind("<B1-Motion>", self._on_drag)
            canvas.bind("<ButtonRelease-1>", self._on_release)
            canvas.bind("<Double-Button-1>", self._on_double)
            top.update_idletasks()
            self.top = top
            self.canvas = canvas
            self.hwnd = winapi.get_hwnd(top)
            winapi.set_no_activate(self.hwnd)
            return True
        except Exception:
            LOG.exception("Не удалось создать панель записи")
            self.top = None
            self.canvas = None
            return False

    def _size(self) -> tuple[int, int]:
        if self.collapsed:
            return self.p(COLLAPSED_W), self.p(COLLAPSED_H)
        return self.p(EXPANDED_W), self.p(EXPANDED_H)

    def _bounds(self) -> tuple[int, int, int, int]:
        left, top, width, height = winapi.virtual_screen()
        w, h = self._size()
        x0 = left + 4
        y0 = top + 4
        x1 = max(x0, left + width - w - 4)
        y1 = max(y0, top + height - h - 4)
        return x0, y0, x1, y1

    def _default_pos(self) -> tuple[int, int]:
        left, top, width, height = winapi.workarea()
        w, h = self._size()
        return (left + max(0, width - w - MARGIN * self.t.s),
                top + max(0, height - h - MARGIN * self.t.s))

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        x0, y0, x1, y1 = self._bounds()
        return max(x0, min(x1, int(x))), max(y0, min(y1, int(y)))

    # ------------------------------------------------------------------
    # render
    def _background(self, w: int, h: int):
        bg = tile_image(w, h, self.p(19), "dark").convert("RGBA")
        return bg

    def render(self) -> None:
        if not self._ensure_window():
            return
        w, h = self._size()
        self._w, self._h = w, h
        self._x, self._y = self._clamp(self._x, self._y)
        self.top.geometry(f"{w}x{h}+{self._x}+{self._y}")
        ui = UI(self.canvas, self.t, (w, h))
        ui.clear()
        self.ui = ui
        p = self.p

        bg = self._background(w, h)
        ui.background(bg)
        self._content(ui, w, h)

        # Win32 style/region calls must happen *before* the final geometry
        # request: Tk otherwise forgets a pending move on the ConfigureNotify
        # those calls generate, leaving the bar at its previous coordinates.
        winapi.set_round_region(self.hwnd, w, h, p(19))
        self._apply_exclusion()
        try:
            if not self.top.attributes("-topmost"):
                self.top.attributes("-topmost", True)
        except Exception:
            pass
        self.top.geometry(f"{w}x{h}+{self._x}+{self._y}")

    def _content(self, ui: UI, w: int, h: int) -> None:
        p = self.p
        accent = YELLOW if self.paused else ACCENT
        text = "ПАУЗА" if self.paused else "ЗАПИСЬ"
        timer_text = fmt_elapsed(self.elapsed)

        if self.collapsed:
            ui.icon(p(18), h / 2, "record", 10, accent)
            self.timer_item = ui.text(p(32), h / 2 + p(1), timer_text, 13.4, TEXT,
                                      "semibold")
            d = p(32)
            stop_x = w - p(10) - d
            ui.button((stop_x, (h - d) / 2, d, d), "", "primary", "stop", 13,
                      self.app.stop, radius=32 / 2)
            e = p(24)
            ui.button((stop_x - p(6) - e, (h - e) / 2, e, e), "", "ghost",
                      "chevron_down", 9, self.expand, radius=24 / 2)
            return

        ui.icon(p(20), h / 2, "record", 12, accent)
        ui.text(p(38), p(21), text, 8.2, accent, "semibold")
        self.timer_item = ui.text(p(38), p(40), timer_text, 15.5, TEXT, "semibold")

        ui.canvas.create_line(p(160), p(15), p(160), h - p(15),
                              fill=solid("#FFFFFF12"), width=1)

        stop_w, stop_h = p(46), p(42)
        stop_x = w - p(12) - stop_w
        ui.button((stop_x, (h - stop_h) / 2, stop_w, stop_h), "", "primary", "stop",
                  15, self.app.stop, radius=13)

        d = p(38)
        pause_x = stop_x - p(8) - d
        ui.button((pause_x, (h - d) / 2, d, d), "", "secondary",
                  "play" if self.paused else "pause", 14,
                  self.app.toggle_pause, radius=38 / 2)

        e = p(28)
        ui.button((pause_x - p(6) - e, (h - e) / 2, e, e), "", "ghost",
                  "chevron_up", 10, self.collapse, radius=28 / 2)

    def _apply_exclusion(self) -> None:
        if not self.hwnd:
            return
        try:
            winapi.set_capture_excluded(self.hwnd, True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # visibility
    def show(self, paused: bool = False) -> None:
        if not self._ensure_window():
            return
        self.paused = paused
        self._w, self._h = self._size()
        position = self.app.settings.overlay_pos or {}
        if "x" in position and "y" in position:
            try:
                x = int(position["x"])
                y = int(position["y"])
            except Exception:
                x, y = self._default_pos()
        else:
            x, y = self._default_pos()
        self._x, self._y = self._clamp(x, y)
        self.render()
        try:
            self.top.deiconify()
            self.top.update_idletasks()
        except Exception:
            LOG.debug("Не удалось показать панель записи", exc_info=True)
            return
        self._visible = True
        self._apply_exclusion()
        winapi.set_no_activate(self.hwnd)
        try:
            if not self.top.attributes("-topmost"):
                self.top.attributes("-topmost", True)
            self.top.lift()
        except Exception:
            pass
        # re-assert the position after the Win32 style changes above
        try:
            self.top.geometry(f"{self._w}x{self._h}+{self._x}+{self._y}")
        except Exception:
            pass
        LOG.info("Панель записи показана")

    def hide(self) -> None:
        self._visible = False
        self._drag = None
        self._active = None
        if self.top is None:
            return
        try:
            self.top.withdraw()
        except Exception:
            pass

    def destroy(self) -> None:
        self._visible = False
        top = self.top
        self.top = None
        self.canvas = None
        self.ui = None
        if top is not None:
            try:
                top.destroy()
            except Exception:
                pass

    @property
    def visible(self) -> bool:
        return self._visible

    def tick(self, elapsed: float, paused: bool) -> None:
        self.elapsed = elapsed
        if paused != self.paused:
            self.paused = paused
            if self._visible:
                self.render()
            return
        if not self._visible or self.timer_item is None:
            return
        try:
            self.canvas.itemconfig(self.timer_item, text=fmt_elapsed(elapsed))
        except Exception:
            pass

    def set_paused(self, paused: bool) -> None:
        self.tick(self.elapsed, paused)

    # ------------------------------------------------------------------
    # collapse / drag
    def collapse(self) -> None:
        self.collapsed = True
        self._reflow()

    def expand(self) -> None:
        self.collapsed = False
        self._reflow()

    def toggle_collapsed(self) -> None:
        self.collapsed = not self.collapsed
        self._reflow()

    def _reflow(self) -> None:
        if not self._visible:
            return
        w, h = self._size()
        # keep the right edge in place so the bar grows to the left
        right = self._x + self._w
        self._x = right - w
        self._y = self._y + self._h - h if h < self._h else self._y
        self._w, self._h = w, h
        self._x, self._y = self._clamp(self._x, self._y)
        self.render()
        self._remember()

    def _remember(self) -> None:
        try:
            self.app.settings.overlay_pos = {"x": int(self._x), "y": int(self._y)}
            self.app.save_now()
        except Exception:
            LOG.debug("Не удалось сохранить позицию панели", exc_info=True)

    # ------------------------------------------------------------------
    # mouse
    def _hit_at(self, x: int, y: int):
        if self.ui is None:
            return None
        for hit in reversed(self.ui.hits):
            if hit.contains(x, y):
                return hit
        return None

    def _on_motion(self, event) -> None:
        if self._drag is not None:
            rx, ry, wx, wy = self._drag
            nx = wx + (event.x_root - rx)
            ny = wy + (event.y_root - ry)
            if abs(event.x_root - rx) > 2 or abs(event.y_root - ry) > 2:
                self._moved = True
            self._x, self._y = nx, ny
            try:
                self.top.geometry(f"+{int(nx)}+{int(ny)}")
            except Exception:
                pass
            return
        if self._active is not None and self._active.on_drag is not None:
            try:
                self._active.on_drag(event.x, event.y)
            except Exception:
                pass
            return
        hit = self._hit_at(event.x, event.y)
        if hit is not self._hover:
            old = self._hover
            self._hover = hit
            if old is not None and old.on_hover:
                try:
                    old.on_hover(False)
                except Exception:
                    pass
            if hit is not None and hit.on_hover:
                try:
                    hit.on_hover(True)
                except Exception:
                    pass
            try:
                self.canvas.config(cursor=hit.cursor if hit else "fleur")
            except Exception:
                pass

    def _on_press(self, event) -> None:
        self._moved = False
        hit = self._hit_at(event.x, event.y)
        if hit is not None:
            self._active = hit
            if hit.on_press:
                try:
                    hit.on_press(event.x, event.y)
                except Exception:
                    pass
            return
        self._active = None
        if self.top is not None:
            self._drag = (event.x_root, event.y_root,
                          self.top.winfo_x(), self.top.winfo_y())

    def _on_drag(self, event) -> None:
        self._on_motion(event)

    def _on_release(self, event) -> None:
        if self._drag is not None:
            self._drag = None
            self._x, self._y = self._clamp(self.top.winfo_x(), self.top.winfo_y())
            try:
                self.top.geometry(f"+{self._x}+{self._y}")
            except Exception:
                pass
            if self._moved:
                self._remember()
            return
        hit = self._active
        self._active = None
        if hit is not None:
            if hit.on_release:
                try:
                    hit.on_release(event.x, event.y)
                except Exception:
                    pass
            if not self._moved and hit.contains(event.x, event.y) and hit.on_click:
                try:
                    hit.on_click()
                except Exception:
                    LOG.exception("Ошибка кнопки панели записи")

    def _on_double(self, event) -> None:
        if self._hit_at(event.x, event.y) is None:
            self.toggle_collapsed()
