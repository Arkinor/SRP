"""Standalone video editor window, opened from a gallery recording.

The editor is a separate top-level window (its own taskbar entry) and is not
part of the recorder shell.  It supports the essentials: trim the edges of a
fragment, cut out a fragment, append more videos and export the result as a
single file.
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from tkinter import filedialog
from typing import Optional

import tkinter as tk
from PIL import Image, ImageOps, ImageTk

from .. import editing, logger, winapi
from .kit import Panel, UI, build_background, tile_image
from .theme import (
    ACCENT, DIM, FAINT, GREEN, MUTED, RED, TEXT, TEXT_SOFT, YELLOW,
    Theme, button_face, round_image, solid,
)
from .views import fmt_elapsed

LOG = logger.get()

W, H = 1120, 790
HEADER = 70
LIST_ROWS = 7
ROW_H = 46
ROW_GAP = 4
ROW_STEP = ROW_H + ROW_GAP


def _fmt_time(seconds: float, decimals: int = 1) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if decimals <= 0:
        secs_text = f"{int(secs):02d}"
    else:
        secs_text = f"{secs:0{3 + decimals}.{decimals}f}"
    if hours >= 1:
        return f"{int(hours)}:{int(minutes):02d}:{secs_text}"
    return f"{int(minutes):02d}:{secs_text}"


def _short_name(name: str, limit: int = 30) -> str:
    return name if len(name) <= limit else name[:limit - 1].rstrip() + "…"


class VideoEditor:
    def __init__(self, app, path: str, hidden: bool = False):
        self.app = app
        self.path = str(path)
        self._closed = False
        self._suspended = False
        self._hidden_start = hidden
        self.events: "queue.Queue[tuple]" = queue.Queue()

        clip = editing.make_clip(path)
        if clip is None:
            self._closed = True
            app.toast("Не удалось открыть видео для монтажа", icon="warning", color=RED)
            return

        self.clips: list[editing.Clip] = [clip]
        self.initial_path = self.path
        self.selected = 0
        self.playhead = 0.0
        self.playing = False
        self.muted = False
        self.exporting = False
        self.progress = 0.0
        self._progress_card = None
        self.cancel_event: Optional[threading.Event] = None
        self._undo: list[tuple[list[editing.Clip], int, float]] = []
        self._status: Optional[tuple[str, str, str, float]] = None

        self._preview_image: Optional[Image.Image] = None
        self._preview_photo: Optional[ImageTk.PhotoImage] = None
        self._frame_item = None
        self._time_item = None
        self._playhead_line = None
        self._playhead_knob = None
        self._frame_mask: Optional[Image.Image] = None
        self._frame_size = (0, 0)

        self._stream: Optional[editing.PreviewStream] = None
        self._stream_start = 0.0
        self._still_token = 0
        self._still_job = None

        self._hover = None
        self._active = None
        self._moved = False
        self._win_drag = None
        self._list_scroll = 0

        self._tl_x0 = self._tl_x1 = 0
        self._tl_top = self._tl_bottom = 0
        self._preview_rect = (0, 0, 1, 1)

        try:
            self._make_window()
            self._make_background()
            self.build()
            self._schedule_still()
            self._tick()
        except Exception:
            self._closed = True
            try:
                self.top.destroy()
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------
    # window
    def _make_window(self) -> None:
        work = winapi.workarea()
        scale = min(self.app.scale, (work[2] - 64) / W, (work[3] - 76) / H)
        self.scale = max(0.72, scale)
        self.t = Theme(self.scale)
        self.p = self.t.px
        self.W, self.H = self.p(W), self.p(H)

        top = tk.Toplevel(self.app.root)
        top.withdraw()
        top.title("Редактор — SR Pro")
        top.overrideredirect(True)
        top.configure(bg="#05060B")
        top.attributes("-topmost", False)
        x = work[0] + max(0, (work[2] - self.W) // 2)
        y = work[1] + max(0, (work[3] - self.H) // 2)
        top.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.canvas = tk.Canvas(top, width=self.W, height=self.H, bd=0,
                                highlightthickness=0, bg="#05060B")
        self.canvas.pack(fill="both", expand=True)
        top.update_idletasks()

        self.hwnd = winapi.get_hwnd(top)
        winapi.make_taskbar_window(self.hwnd)
        winapi.set_round_region(self.hwnd, self.W, self.H, self.p(24))
        try:
            top.iconphoto(True, self.app._icon_photo)
        except Exception:
            pass

        self.top = top
        self.ui = UI(self.canvas, self.t, (self.W, self.H))
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        top.bind("<space>", self._on_space)
        top.bind("<Left>", lambda _e: self._nudge(-1.0))
        top.bind("<Right>", lambda _e: self._nudge(1.0))
        top.bind("<Delete>", lambda _e: self.delete_clip())
        top.bind("<Escape>", self._on_escape)
        top.bind("<Control-z>", lambda _e: self.undo())
        top.bind("<Control-s>", lambda _e: self.save())
        top.protocol("WM_DELETE_WINDOW", self.close)

        if self._hidden_start:
            self._suspended = True
        else:
            top.deiconify()
            top.lift()
            top.focus_force()
            self.canvas.focus_set()

    def _make_background(self) -> None:
        panels = [
            Panel(24, 82, 700, 394, 22, "dim"),
            Panel(24, 536, 700, 190, 22),
            Panel(748, 82, 348, 418, 22),
            Panel(748, 512, 348, 270, 22),
        ]
        self.background = build_background(self.W, self.H, self.t, panels,
                                           header_line=HEADER)

    # ------------------------------------------------------------------
    # rendering
    def build(self) -> None:
        if self._closed:
            return
        ui = self.ui
        ui.clear()
        self._hover = None
        self._active = None
        self._frame_item = None
        self._time_item = None
        self._playhead_line = None
        self._playhead_knob = None
        self._progress_card = None
        try:
            self.canvas.config(cursor="arrow")
        except Exception:
            pass

        ui.background(self.background)
        self._draw_header(ui)
        self._draw_preview(ui)
        self._draw_transport(ui)
        self._draw_timeline(ui)
        self._draw_clips(ui)
        self._draw_actions(ui)
        self._draw_footer(ui)

        if self.exporting:
            self._draw_export(ui)
        if self._status and self._status[3] > time.time():
            self._toast_ui(ui)
        self._update_playhead_items()

    def _draw_header(self, ui: UI) -> None:
        p = self.p
        total = editing.total_duration(self.clips)
        ui.icon(p(36), p(37), "scissors", 17, ACCENT)
        ui.text(p(60), p(29), "Редактор видео", 18, TEXT, "semibold")
        subtitle = (f"{_short_name(Path(self.initial_path).stem, 52)} · "
                    f"{len(self.clips)} фрагм. · {fmt_elapsed(total)}")
        ui.text(p(60), p(49), subtitle, 9.8, DIM, max_width=p(780))
        ui.icon_button(p(1060), p(36), 32, "minimize", self.minimize,
                       kind="ghost", icon_color=MUTED)
        ui.icon_button(p(1096), p(36), 32, "close", self.close,
                       kind="ghost", icon_color=MUTED)
        ui.button((p(866), p(16), p(170), p(40)), "Сохранить видео", "primary",
                  "check", 14, self.save, size=11.2, radius=11,
                  enabled=not self.exporting and bool(self.clips))

    def _draw_preview(self, ui: UI) -> None:
        p = self.p
        x = p(32)
        y = p(90)
        w = p(684)
        h = p(378)
        self._preview_rect = (x, y, w, h)
        if (w, h) != self._frame_size:
            self._frame_size = (w, h)
            mask = Image.new("L", (w, h), 0)
            from PIL import ImageDraw
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1),
                                                   radius=p(14), fill=255)
            self._frame_mask = mask
        if self._preview_image is not None:
            self._render_preview_image(ui)
        else:
            ui.draw_image(x, y, round_image(tile_image(w, h, p(14), "dark"), p(14)))
            ui.icon(x + w / 2, y + h / 2 - p(12), "play", 34, "#FFFFFF30")
            ui.text(x + w / 2, y + h / 2 + p(26), "Загружаем кадр…", 11, DIM,
                    anchor="center")

    def _render_preview_image(self, ui: Optional[UI] = None) -> None:
        image = self._preview_image
        if image is None:
            return
        ui = ui or self.ui
        x, y, w, h = self._preview_rect
        size = (w, h)
        try:
            padded = ImageOps.pad(image.convert("RGB"), size, Image.BILINEAR,
                                  color=(8, 9, 16), centering=(0.5, 0.5))
        except Exception:
            return
        if self._frame_mask is not None:
            padded = padded.convert("RGBA")
            padded.putalpha(self._frame_mask)
        self._preview_photo = ImageTk.PhotoImage(padded)
        if self._frame_item is None:
            self._frame_item = ui.canvas.create_image(x, y, anchor="nw",
                                                      image=self._preview_photo)
        else:
            ui.canvas.itemconfig(self._frame_item, image=self._preview_photo)
            ui.canvas.coords(self._frame_item, x, y)

    def _draw_transport(self, ui: UI) -> None:
        p = self.p
        total = editing.total_duration(self.clips)
        ui.icon_button(p(24) + p(30), p(484) + p(21), 42,
                       "pause" if self.playing else "play",
                       self.toggle_play, kind="secondary", icon_color=TEXT)
        label = (f"{_fmt_time(self.playhead)} / {_fmt_time(total, 0)}"
                 if total else "00:00.0 / 00:00")
        self._time_item = ui.text(p(24) + p(62), p(484) + p(21) + p(1), label,
                                  12.2, TEXT_SOFT, "semibold")
        ui.text(p(24) + p(62), p(484) + p(42), "Пробел — играть · ← → — шаг 1 с",
                8.8, FAINT)

    def _draw_timeline(self, ui: UI) -> None:
        p = self.p
        x0, y0 = p(24), p(536)
        total = max(1e-3, editing.total_duration(self.clips))
        ui.text(x0 + p(20), y0 + p(24), "Дорожка", 12.5, TEXT, "semibold")
        ui.text(x0 + p(680), y0 + p(24), f"Всего {fmt_elapsed(total)}", 9.8,
                DIM, anchor="e")

        self._tl_x0 = x0 + p(20)
        self._tl_x1 = x0 + p(680)
        self._tl_top = y0 + p(74)
        self._tl_bottom = y0 + p(126)
        width = self._tl_x1 - self._tl_x0

        ui.canvas.create_line(self._tl_x0, self._tl_top, self._tl_x1,
                              self._tl_top, fill=solid("#FFFFFF10"), width=1)
        ui.canvas.create_line(self._tl_x0, self._tl_bottom, self._tl_x1,
                              self._tl_bottom, fill=solid("#FFFFFF10"), width=1)

        step = self._nice_step(total)
        tick = 0.0
        while tick <= total + 1e-6:
            x = self._tl_x0 + width * (tick / total)
            ui.canvas.create_line(x, y0 + p(66), x, y0 + p(72),
                                  fill=solid("#FFFFFF22"), width=1)
            ui.text(x, y0 + p(56), _fmt_time(tick, 0 if step >= 1 else 1), 8.2,
                    FAINT, anchor="center")
            tick += step

        for index, clip in enumerate(self.clips):
            start, _end = editing.clip_span(self.clips, index)
            bx = self._tl_x0 + width * (start / total)
            bw = max(2.0, width * clip.duration / total - 1.0)
            selected = index == self.selected
            radius = p(7)
            if selected:
                face = button_face(int(bw), int(self._tl_bottom - self._tl_top),
                                   radius, "selected", 0)
            else:
                face = tile_image(int(bw), int(self._tl_bottom - self._tl_top),
                                  radius, "raised")
            ui.draw_image(bx, self._tl_top, face)
            if bw > p(34):
                ui.text(bx + p(7), self._tl_top + p(15), str(index + 1), 9.2,
                        TEXT if selected else TEXT_SOFT, "semibold")
            if bw > p(64):
                ui.text(bx + p(7), self._tl_top + p(32),
                        fmt_elapsed(clip.duration), 8.4,
                        TEXT_SOFT if selected else DIM)

        hint = "Нажмите на дорожку, чтобы поставить курсор"
        if self.selected < len(self.clips):
            start, end = editing.clip_span(self.clips, self.selected)
            hint = (f"Фрагмент {self.selected + 1}: "
                    f"{_fmt_time(self.clips[self.selected].start)} – "
                    f"{_fmt_time(self.clips[self.selected].end)} источника "
                    f"({fmt_elapsed(self.clips[self.selected].duration)})")
        ui.text(x0 + p(20), y0 + p(152), hint, 9.2, DIM, max_width=p(680))

        track_hit = ui.hit((self._tl_x0 - p(4), y0 + p(62), width + p(8),
                            p(76)), None, cursor="hand2",
                           on_press=self._timeline_press,
                           on_drag=self._timeline_drag,
                           on_release=self._timeline_release)
        track_hit.on_click = None

        if self.clips:
            line_x = self._tl_x0 + width * (self.playhead / total)
            self._playhead_line = ui.canvas.create_line(
                line_x, self._tl_top - p(10), line_x, self._tl_bottom + p(6),
                fill=ACCENT, width=max(1, p(2)))
            knob = p(7)
            self._playhead_knob = ui.canvas.create_oval(
                line_x - knob, self._tl_top - p(10) - knob * 0.4,
                line_x + knob, self._tl_top - p(10) + knob * 0.6,
                fill=ACCENT, outline="")

    def _draw_clips(self, ui: UI) -> None:
        p = self.p
        x0, y0 = p(748), p(82)
        count = len(self.clips)
        ui.text(x0 + p(20), y0 + p(24), "Фрагменты", 12.5, TEXT, "semibold")
        ui.text(x0 + p(328), y0 + p(24), str(count), 10, DIM, "semibold",
                anchor="e")

        list_top = y0 + p(42)
        visible = LIST_ROWS
        maximum = max(0, count - visible)
        self._list_scroll = max(0, min(self._list_scroll, maximum))
        row_w = p(316)
        for row in range(visible):
            index = self._list_scroll + row
            if index >= count:
                break
            clip = self.clips[index]
            ry = list_top + row * p(ROW_STEP)
            hover = ui.draw_image(x0 + p(16), ry,
                                  tile_image(row_w, p(ROW_H), p(11), "raised"))
            ui.canvas.itemconfig(hover, state="hidden")
            if index == self.selected:
                ui.draw_image(x0 + p(16), ry,
                              button_face(row_w, p(ROW_H), p(11), "selected", 0))
                ui.canvas.create_line(x0 + p(16), ry + p(8), x0 + p(16),
                                      ry + p(ROW_H) - p(8), fill=ACCENT,
                                      width=max(1, p(3)))

            ui.text(x0 + p(32), ry + p(15), f"{index + 1}", 9.4, DIM, "semibold")
            ui.text(x0 + p(48), ry + p(15),
                    ui.fit(clip.name, p(188), 10.4, "semibold"), 10.4,
                    TEXT if index == self.selected else TEXT_SOFT, "semibold")
            ui.text(x0 + p(48), ry + p(32),
                    f"{_fmt_time(clip.start)} – {_fmt_time(clip.end)}",
                    8.6, DIM)
            ui.text(x0 + p(308), ry + p(23), fmt_elapsed(clip.duration), 8.8,
                    MUTED, "semibold", anchor="e")

            def enter(item=hover) -> None:
                if index != self.selected:
                    ui.canvas.itemconfig(item, state="normal")

            def leave(item=hover) -> None:
                ui.canvas.itemconfig(item, state="hidden")

            hit = ui.hit((x0 + p(16), ry, row_w, p(ROW_H)),
                         lambda target=index: self.select_clip(target))
            hit.on_hover = lambda state, e=enter, l=leave: (e() if state else l())

        if count > visible:
            track_x = x0 + p(336)
            track_top = list_top + p(4)
            track_bottom = list_top + visible * p(ROW_STEP) - p(ROW_GAP)
            ui.canvas.create_line(track_x, track_top, track_x, track_bottom,
                                  fill=solid("#FFFFFF14"),
                                  width=max(1, p(2)), capstyle="round")
            ratio = visible / count
            thumb_h = (track_bottom - track_top) * ratio
            pos = self._list_scroll / max(1, maximum)
            ty = track_top + (track_bottom - track_top - thumb_h) * pos
            ui.canvas.create_line(track_x, ty, track_x, ty + thumb_h,
                                  fill=solid("#FFFFFF55"),
                                  width=max(1, p(2)), capstyle="round")

    def _draw_actions(self, ui: UI) -> None:
        p = self.p
        x0, y0 = p(748), p(512)
        ui.text(x0 + p(20), y0 + p(24), "Действия", 12.5, TEXT, "semibold")
        ui.text(x0 + p(250), y0 + p(24), "Без звука", 9.8, MUTED, "semibold",
                anchor="e")
        ui.toggle(x0 + p(264), y0 + p(11), self.muted,
                  lambda value: self.set_muted(value), width=46)

        selected = self.clips[self.selected] if self.clips else None
        inside = False
        local = 0.0
        if selected is not None:
            start, end = editing.clip_span(self.clips, self.selected)
            local = self.playhead - start
            inside = 0.12 < local < selected.duration - 0.12

        ui.button((x0 + p(16), y0 + p(40), p(316), p(42)),
                  "Разрезать по курсору", "secondary", "scissors", 14,
                  self.split, size=10.8, radius=11,
                  enabled=inside and not self.exporting)

        half = p(153)
        ui.button((x0 + p(16), y0 + p(90), half, p(40)),
                  "Обрезать начало", "secondary", "chevron_right", 12,
                  self.trim_start, size=9.6, radius=10,
                  enabled=inside and not self.exporting)
        ui.button((x0 + p(179), y0 + p(90), half, p(40)),
                  "Обрезать конец", "secondary", "chevron_left", 12,
                  self.trim_end, size=9.6, radius=10,
                  enabled=inside and not self.exporting)

        ui.button((x0 + p(16), y0 + p(140), p(316), p(40)),
                  "Удалить фрагмент", "danger", "trash", 13,
                  self.delete_clip, size=10.6, radius=10,
                  enabled=len(self.clips) > 1 and not self.exporting)

        ui.button((x0 + p(16), y0 + p(188), p(316), p(40)),
                  "Добавить видео", "secondary", "plus", 13,
                  self.add_clips, size=10.8, radius=10,
                  enabled=not self.exporting)

        ui.button((x0 + p(16), y0 + p(234), half, p(34)),
                  "Отменить", "ghost", "undo", 12, self.undo, size=9.8,
                  radius=9, enabled=bool(self._undo) and not self.exporting)
        ui.button((x0 + p(179), y0 + p(234), half, p(34)),
                  "Сбросить", "ghost", "refresh", 12, self.reset, size=9.8,
                  radius=9, enabled=not self.exporting)

    def _draw_footer(self, ui: UI) -> None:
        p = self.p
        if self.clips and self.selected < len(self.clips):
            clip = self.clips[self.selected]
            text = (f"Выбран фрагмент {self.selected + 1} из {len(self.clips)} · "
                    f"{fmt_elapsed(clip.duration)} · "
                    f"в источнике {_fmt_time(clip.start)}–{_fmt_time(clip.end)}")
        else:
            text = "Добавьте видео, чтобы начать монтаж"
        ui.text(p(24), p(760), text, 9.8, DIM, max_width=p(700))

    def _draw_export(self, ui: UI) -> None:
        p = self.p
        total = editing.total_duration(self.clips)
        card = ui.progress_card("Сохраняем видео",
                               f"{len(self.clips)} фрагм. · {fmt_elapsed(total)} · "
                               f"не закрывайте окно", width=420)
        self._progress_card = card
        ui.update_progress(card, self.progress)
        y = (self.H - p(136)) // 2 + p(136) + p(12)
        ui.button((self.W // 2 - p(70), y, p(140), p(36)), "Отмена", "ghost",
                  None, 12, self.cancel_export, size=10.5, radius=10)

    def _toast_ui(self, ui: UI) -> None:
        text, icon, color, _until = self._status  # type: ignore[misc]
        ui.toast(text, icon, color)

    def _show_status(self, text: str, icon: str = "info", color: str = TEXT_SOFT,
                     seconds: float = 4.0) -> None:
        self._status = (text, icon, color, time.time() + seconds)
        self.build()

    # ------------------------------------------------------------------
    # helpers
    def _toast(self, text: str, icon: str = "check", color: str = GREEN,
               seconds: float = 4.0) -> None:
        self._status = (text, icon, color, time.time() + seconds)
        self.build()

    def _nice_step(self, duration: float) -> float:
        for step in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900):
            if duration / step <= 10:
                return step
        return 1800

    def _snapshot(self) -> None:
        self._undo.append(([clip.copy() for clip in self.clips],
                           self.selected, self.playhead))
        if len(self._undo) > 50:
            self._undo.pop(0)

    def _stream_size(self) -> tuple[int, int]:
        _x, _y, w, h = self._preview_rect
        source_w, source_h = editing.video_size(self.clips)
        aspect = source_w / max(1, source_h)
        width = int(w)
        height = int(round(width / aspect))
        if height > h:
            height = int(h)
            width = int(round(height * aspect))
        width = max(2, width - width % 2)
        height = max(2, height - height % 2)
        return width, height

    def select_clip(self, index: int) -> None:
        if not 0 <= index < len(self.clips):
            return
        self.selected = index
        start, _end = editing.clip_span(self.clips, index)
        self._pause(keep_frame=True)
        self.playhead = min(start + 0.01, editing.total_duration(self.clips))
        self.build()
        self._schedule_still()

    # ------------------------------------------------------------------
    # editing actions
    def split(self) -> None:
        if not self.clips or self.exporting:
            return
        index, local = editing.locate(self.clips, self.playhead)
        clip = self.clips[index]
        if local < 0.12 or clip.duration - local < 0.12:
            self._show_status("Поставьте курсор внутрь фрагмента, подальше от краёв",
                              icon="info", color=YELLOW)
            return
        self._snapshot()
        left = clip.copy()
        left.end = clip.start + local
        right = clip.copy()
        right.start = clip.start + local
        self.clips[index:index + 1] = [left, right]
        self.selected = index + 1
        self.playhead = editing.clip_span(self.clips, index + 1)[0]
        self._pause(keep_frame=True)
        self.build()
        self._schedule_still()
        self._show_status("Фрагмент разрезан", icon="scissors", color=GREEN,
                          seconds=2.5)

    def trim_start(self) -> None:
        self._trim(True)

    def trim_end(self) -> None:
        self._trim(False)

    def _trim(self, start_edge: bool) -> None:
        if not self.clips or self.exporting or self.selected >= len(self.clips):
            return
        clip = self.clips[self.selected]
        start_global, _end_global = editing.clip_span(self.clips, self.selected)
        local = self.playhead - start_global
        if local <= 0.12 or local >= clip.duration - 0.12:
            self._show_status("Курсор должен стоять внутри выбранного фрагмента",
                              icon="info", color=YELLOW)
            return
        self._snapshot()
        if start_edge:
            clip.start = min(clip.end - 0.12, clip.start + local)
            self.playhead = start_global
        else:
            clip.end = max(clip.start + 0.12, clip.start + local)
            self.playhead = start_global + clip.duration
        self._pause(keep_frame=True)
        self.build()
        self._schedule_still()
        self._show_status("Начало обрезано" if start_edge else "Конец обрезан",
                          icon="crop", color=GREEN, seconds=2.5)

    def delete_clip(self) -> None:
        if not self.clips or self.exporting:
            return
        if len(self.clips) <= 1:
            self._show_status("Нельзя удалить единственный фрагмент",
                              icon="info", color=YELLOW)
            return
        self._snapshot()
        index = self.selected
        del self.clips[index]
        self.selected = max(0, min(index, len(self.clips) - 1))
        self._pause(keep_frame=True)
        self.playhead = editing.clip_span(self.clips, self.selected)[0]
        self.build()
        self._schedule_still()
        self._show_status("Фрагмент удалён", icon="trash", color=YELLOW,
                          seconds=2.5)

    def add_clips(self) -> None:
        if self.exporting:
            return
        folder = str(Path(self.clips[0].path).parent) if self.clips \
            else str(self.app.settings.output_dir)
        try:
            self.top.attributes("-topmost", True)
            paths = filedialog.askopenfilenames(
                parent=self.top, title="Добавить видео", initialdir=folder,
                filetypes=[("Видео", "*.mp4 *.mkv *.mov *.webm"),
                           ("Все файлы", "*.*")],
            )
        except Exception:
            paths = ()
        finally:
            try:
                self.top.attributes("-topmost", False)
            except Exception:
                pass
        if not paths:
            return
        added = [clip for clip in (editing.make_clip(path) for path in paths)
                 if clip is not None]
        if not added:
            self._show_status("Не удалось прочитать выбранные видео",
                              icon="warning", color=RED)
            return
        self._snapshot()
        self.clips.extend(added)
        self._list_scroll = max(0, len(self.clips) - LIST_ROWS)
        self.build()
        self._schedule_still()
        self._show_status(f"Добавлено видео: {len(added)}", icon="plus",
                          color=GREEN, seconds=2.5)

    def reset(self) -> None:
        if self.exporting:
            return
        clip = editing.make_clip(self.initial_path)
        if clip is None:
            self._show_status("Исходное видео недоступно", icon="warning",
                              color=RED)
            return
        self._snapshot()
        self.clips = [clip]
        self.selected = 0
        self.playhead = 0.0
        self._list_scroll = 0
        self._pause(keep_frame=True)
        self.build()
        self._schedule_still()
        self._show_status("Изменения сброшены", icon="refresh", color=GREEN,
                          seconds=2.5)

    def undo(self) -> None:
        if not self._undo or self.exporting:
            return
        clips, selected, playhead = self._undo.pop()
        self.clips = clips
        self.selected = max(0, min(selected, len(clips) - 1))
        self.playhead = max(0.0, min(playhead, editing.total_duration(clips)))
        self._pause(keep_frame=True)
        self.build()
        self._schedule_still()
        self._show_status("Изменение отменено", icon="undo", color=GREEN,
                          seconds=2.0)

    def set_muted(self, value: bool) -> None:
        self.muted = bool(value)
        self.build()

    # ------------------------------------------------------------------
    # playback
    def toggle_play(self) -> None:
        if self.playing:
            self._pause()
        else:
            self._play()

    def _play(self) -> None:
        if not self.clips or self.exporting:
            return
        total = editing.total_duration(self.clips)
        if total <= 0.2:
            return
        self._still_token += 1
        if self._still_job is not None:
            try:
                self.top.after_cancel(self._still_job)
            except Exception:
                pass
            self._still_job = None
        if self.playhead >= total - 0.05:
            self.playhead = 0.0
        start = self.playhead
        self._stop_stream()
        stream = editing.PreviewStream(self.clips, start, self._stream_size(),
                                       fps=editing.PREVIEW_FPS)
        self._stream = stream
        self._stream_start = start
        self.playing = True
        self.build()

        def on_frame(image: Image.Image, index: int) -> None:
            self.events.put(("frame", stream, image, index))

        def on_done() -> None:
            self.events.put(("stream_done", stream))

        stream.start(on_frame, on_done)

    def _pause(self, keep_frame: bool = True) -> None:
        was_playing = self.playing
        self.playing = False
        self._stop_stream()
        if was_playing and keep_frame:
            self.build()
        if keep_frame:
            self._schedule_still()

    def _stop_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()

    def _on_space(self, _event=None) -> str:
        self.toggle_play()
        return "break"

    def _on_escape(self, _event=None) -> None:
        if self.exporting:
            self.cancel_export()
        else:
            self.close()

    def _nudge(self, delta: float) -> None:
        if not self.clips:
            return
        total = editing.total_duration(self.clips)
        playing = self.playing
        if playing:
            self._pause(keep_frame=False)
        self.playhead = max(0.0, min(total, self.playhead + delta))
        self.build()
        self._schedule_still()

    def _seek(self, value: float, restart: bool = True) -> None:
        if not self.clips:
            return
        total = editing.total_duration(self.clips)
        self.playhead = max(0.0, min(total, value))
        if self.playing and restart:
            start = self.playhead
            self._stop_stream()
            stream = editing.PreviewStream(self.clips, start,
                                           self._stream_size(),
                                           fps=editing.PREVIEW_FPS)
            self._stream = stream
            self._stream_start = start

            def on_frame(image: Image.Image, index: int) -> None:
                self.events.put(("frame", stream, image, index))

            def on_done() -> None:
                self.events.put(("stream_done", stream))

            stream.start(on_frame, on_done)
        elif not self.playing:
            self._schedule_still()
        self._update_playhead_items()

    def _schedule_still(self) -> None:
        if self._closed or self.playing or not self.clips:
            return
        self._still_token += 1
        token = self._still_token
        if self._still_job is not None:
            try:
                self.top.after_cancel(self._still_job)
            except Exception:
                pass

        def run() -> None:
            self._still_job = None
            if token != self._still_token or self.playing:
                return
            clip = self.clips
            position = self.playhead
            width = self._stream_size()[0]

            def worker() -> None:
                image = editing.still_frame(clip, position, width=width)
                if image is not None:
                    self.events.put(("still", token, image))

            threading.Thread(target=worker, name="EditStill", daemon=True).start()

        self._still_job = self.top.after(140, run)

    def _tick(self) -> None:
        if self._closed:
            return
        try:
            self._drain()
        except Exception:
            LOG.debug("Ошибка цикла редактора", exc_info=True)
        if self._status and self._status[3] <= time.time():
            self._status = None
            self.build()
        try:
            self.top.after(33, self._tick)
        except Exception:
            pass

    def _drain(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            if kind == "frame":
                _kind, stream, image, index = event
                if stream is not self._stream:
                    continue
                self._on_stream_frame(image, index)
            elif kind == "stream_done":
                stream = event[1]
                if stream is not self._stream:
                    continue
                self._on_stream_done()
            elif kind == "still":
                token, image = event[1], event[2]
                if token != self._still_token or self.playing:
                    continue
                self._preview_image = image
                if self._frame_item is not None:
                    self._render_preview_image(self.ui)
                else:
                    self.build()
            elif kind == "progress":
                self.progress = float(event[1])
                if self._progress_card is not None:
                    self.ui.update_progress(self._progress_card, self.progress)
            elif kind == "export_done":
                self._on_export_done(event[1], event[2], event[3])

    def _on_stream_frame(self, image: Image.Image, index: int) -> None:
        self._preview_image = image
        total = editing.total_duration(self.clips)
        self.playhead = min(total, self._stream_start + index / editing.PREVIEW_FPS)
        if self._frame_item is not None:
            self._render_preview_image(self.ui)
        self._update_playhead_items()

    def _on_stream_done(self) -> None:
        if not self.playing:
            return
        self.playing = False
        self._stream = None
        total = editing.total_duration(self.clips)
        self.playhead = min(total, self.playhead)
        self.build()
        self._schedule_still()

    def _update_playhead_items(self) -> None:
        if self._time_item is not None:
            total = editing.total_duration(self.clips)
            label = (f"{_fmt_time(self.playhead)} / {_fmt_time(total, 0)}"
                     if total else "00:00.0 / 00:00")
            try:
                self.canvas.itemconfig(self._time_item, text=label)
            except Exception:
                pass
        if self._playhead_line is not None and self._tl_x1 > self._tl_x0:
            total = max(1e-3, editing.total_duration(self.clips))
            width = self._tl_x1 - self._tl_x0
            x = self._tl_x0 + width * min(1.0, self.playhead / total)
            try:
                self.canvas.coords(self._playhead_line, x, self._tl_top - self.p(10),
                                   x, self._tl_bottom + self.p(6))
                if self._playhead_knob is not None:
                    knob = self.p(7)
                    self.canvas.coords(self._playhead_knob, x - knob,
                                       self._tl_top - self.p(10) - knob * 0.4,
                                       x + knob,
                                       self._tl_top - self.p(10) + knob * 0.6)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # save
    def save(self) -> None:
        if self.exporting or not self.clips:
            return
        self._pause(keep_frame=True)
        first = Path(self.clips[0].path)
        default = editing.unique_output(first.with_name(f"{first.stem} — монтаж.mp4"))
        try:
            self.top.attributes("-topmost", True)
            path = filedialog.asksaveasfilename(
                parent=self.top, title="Сохранить видео",
                initialdir=str(first.parent), initialfile=default.name,
                defaultextension=".mp4",
                filetypes=[("Видео MP4", "*.mp4"), ("Видео MKV", "*.mkv")],
            )
        except Exception:
            path = ""
        finally:
            try:
                self.top.attributes("-topmost", False)
            except Exception:
                pass
        if not path:
            return
        target = Path(path).resolve()
        sources = {Path(clip.path).resolve() for clip in self.clips}
        if target in sources:
            self._toast("Это исходное видео — выберите другое имя файла",
                        icon="warning", color=RED, seconds=6)
            return
        cancel = threading.Event()
        self.cancel_event = cancel
        self.exporting = True
        self.progress = 0.0
        self.build()
        clips = [clip.copy() for clip in self.clips]
        quality = self.app.settings.quality
        fps = int(self.app.settings.fps or 30)
        with_audio = not self.muted

        def worker() -> None:
            ok, error = editing.render(
                clips, path, quality=quality, with_audio=with_audio, fps=fps,
                progress=lambda value: self.events.put(("progress", value)),
                cancel=cancel,
            )
            self.events.put(("export_done", ok, error, path))

        threading.Thread(target=worker, name="EditRender", daemon=True).start()

    def cancel_export(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()

    def _on_export_done(self, ok: bool, error: str, path: str) -> None:
        if self._closed:
            return
        self.exporting = False
        self.cancel_event = None
        self.progress = 0.0
        self.build()
        if ok:
            self._toast(f"Сохранено: {Path(path).name}", icon="check",
                        color=GREEN, seconds=6)
            try:
                self.app.on_edit_saved(path)
            except Exception:
                LOG.debug("Не удалось обновить галерею", exc_info=True)
        elif error == "Отменено":
            self._toast("Сохранение отменено", icon="info", color=YELLOW,
                        seconds=3)
        else:
            first = (error or "").splitlines()[0][:120]
            self._toast(f"Не удалось сохранить: {first}", icon="warning",
                        color=RED, seconds=8)

    # ------------------------------------------------------------------
    # mouse
    def _hit_at(self, x: int, y: int):
        for hit in reversed(self.ui.hits):
            if hit.contains(x, y):
                return hit
        return None

    def _on_motion(self, event) -> None:
        x, y = event.x, event.y
        if self._win_drag is not None:
            dx = x - self._win_drag[0]
            dy = y - self._win_drag[1]
            self.top.geometry(f"+{self._win_drag[2] + dx}+{self._win_drag[3] + dy}")
            return
        if self._active is not None and self._active.on_drag is not None:
            try:
                self._active.on_drag(x, y)
            except Exception:
                LOG.debug("on_drag редактора", exc_info=True)
            self._moved = True
            return
        hit = self._hit_at(x, y)
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
                self.canvas.config(cursor=hit.cursor if hit else "arrow")
            except Exception:
                pass

    def _on_press(self, event) -> None:
        x, y = event.x, event.y
        self._moved = False
        hit = self._hit_at(x, y)
        if hit is not None:
            self._active = hit
            if hit.on_press:
                try:
                    hit.on_press(x, y)
                except Exception:
                    LOG.debug("on_press редактора", exc_info=True)
            return
        self._active = None
        if y < self.p(HEADER) and not self.exporting:
            self._win_drag = (x, y, self.top.winfo_x(), self.top.winfo_y())

    def _on_drag(self, event) -> None:
        self._on_motion(event)

    def _on_release(self, event) -> None:
        x, y = event.x, event.y
        if self._win_drag is not None:
            self._win_drag = None
            return
        hit = self._active
        self._active = None
        if hit is not None:
            if hit.on_release:
                try:
                    hit.on_release(x, y)
                except Exception:
                    LOG.debug("on_release редактора", exc_info=True)
            if not self._moved and hit.contains(x, y) and hit.on_click:
                try:
                    hit.on_click()
                except Exception:
                    LOG.exception("Ошибка нажатия в редакторе")
            self._hover = None

    def _on_wheel(self, event) -> None:
        if len(self.clips) <= LIST_ROWS:
            return
        maximum = max(0, len(self.clips) - LIST_ROWS)
        delta = -1 if event.delta > 0 else 1
        value = max(0, min(maximum, self._list_scroll + delta))
        if value != self._list_scroll:
            self._list_scroll = value
            self.build()

    def _timeline_press(self, x: int, y: int) -> None:
        if not self.clips:
            return
        self._pause(keep_frame=False)
        self._timeline_seek(x)
        self._moved = False

    def _timeline_drag(self, x: int, y: int) -> None:
        if not self.clips:
            return
        self._timeline_seek(x)
        self._moved = True

    def _timeline_release(self, x: int, y: int) -> None:
        if not self.clips:
            return
        self._timeline_seek(x)
        self.build()
        self._schedule_still()

    def _timeline_seek(self, x: int) -> None:
        width = max(1, self._tl_x1 - self._tl_x0)
        ratio = max(0.0, min(1.0, (x - self._tl_x0) / width))
        total = editing.total_duration(self.clips)
        self.playhead = ratio * total
        index, _local = editing.locate(self.clips, self.playhead)
        self.selected = index
        self._update_playhead_items()

    # ------------------------------------------------------------------
    # lifecycle
    def minimize(self) -> None:
        winapi.minimize(self.hwnd)

    def suspend(self) -> None:
        """Hide the editor while the recorder is capturing the screen."""
        if self._closed or self._suspended:
            return
        self._suspended = True
        self._pause(keep_frame=False)
        try:
            self.top.withdraw()
        except Exception:
            pass

    def resume(self) -> None:
        if self._closed or not self._suspended:
            return
        self._suspended = False
        try:
            self.top.deiconify()
            self.top.lift()
            self.top.focus_force()
            winapi.restore(self.hwnd)
        except Exception:
            pass
        self._schedule_still()

    def close(self) -> None:
        if self._closed:
            return
        if self.cancel_event is not None:
            self.cancel_event.set()
        self._closed = True
        self._still_token += 1
        self._stop_stream()
        try:
            self.top.destroy()
        except Exception:
            pass
        if self.app._editor is self:
            self.app._editor = None

    @property
    def alive(self) -> bool:
        return not self._closed
