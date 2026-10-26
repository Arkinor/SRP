"""The three screens: recorder, gallery and settings."""
from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from .. import ffmpeg_tools, winapi
from . import icons
from .kit import Panel, UI, PopupItem, tile_image
from .theme import (
    ACCENT, ACCENT_LIGHT, BLUE, DIM, FAINT, GREEN, MUTED, PURPLE, RED, TEXT, TEXT_SOFT,
    WHITE, YELLOW, shade, solid,
)

# ----------------------------------------------------------------------
# geometry (logical units)
# ----------------------------------------------------------------------
W, H = 1060, 704
HEADER = 72
FOOTER_Y = 622
LEFT_X, LEFT_W = 26, 604
RIGHT_X, RIGHT_W = 650, 384
TOP = 88


def fmt_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_size(size: float) -> str:
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.1f} ГБ"
    if size >= 1024 ** 2:
        return f"{size / 1024 ** 2:.1f} МБ"
    return f"{max(1, int(size / 1024))} КБ"


def fmt_date(timestamp: float) -> str:
    dt = datetime.fromtimestamp(timestamp)
    months = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен",
              "окт", "ноя", "дек")
    today = datetime.now()
    if dt.date() == today.date():
        return f"сегодня, {dt:%H:%M}"
    if dt.year == today.year:
        return f"{dt.day} {months[dt.month - 1]}, {dt:%H:%M}"
    return f"{dt:%d.%m.%Y}"


def chip(ui: UI, x: float, y: float, label: str, icon: Optional[str] = None,
         color: str = TEXT, tone: str = "chip", size: float = 10.5,
         dot: Optional[str] = None, height: float = 28) -> int:
    t = ui.t
    p = t.px
    gap = p(6) if (icon or dot) else 0
    extra = p(13) if (icon or dot) else 0
    pad = p(11)
    width = ui.measure(label, size, "semibold") + pad * 2 + extra + gap
    ui.draw_image(x, y, tile_image(int(width), int(p(height)), int(p(height / 2)), tone))
    cx = x + pad
    if dot:
        ui.icon(cx + p(6.5), y + p(height) / 2, "record", 8.5, dot)
    elif icon:
        ui.icon(cx + p(6.5), y + p(height) / 2, icon, 11.5, color)
    if icon or dot:
        cx += extra + gap
    return ui.text(cx, y + p(height) / 2 + 0.5, label, size, color, "semibold")


def section_header(ui: UI, x: float, y: float, title: str, sub: str = "",
                   size: float = 13) -> None:
    t = ui.t
    ui.text(t.px(x), t.px(y), title, size, TEXT, "semibold")
    if sub:
        ui.text(t.px(x), t.px(y + 19), sub, 10.3, DIM)


class View:
    name = ""

    def __init__(self, app):
        self.app = app

    def panels(self) -> list[Panel]:
        return []

    def build(self, ui: UI) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def tick(self, ui: UI) -> None:
        pass


# ======================================================================
# Recorder
# ======================================================================
class RecordView(View):
    name = "record"

    def __init__(self, app):
        super().__init__(app)
        self.preview_item = None
        self.timer_item = None
        self.meters = {}
        self.stats_items = {}
        self.audio_item = None

    def panels(self) -> list[Panel]:
        return [
            Panel(RIGHT_X, 88, RIGHT_W, 232, 22),
            Panel(RIGHT_X, 332, RIGHT_W, 180, 22),
            Panel(RIGHT_X, 524, RIGHT_W, 84, 22),
        ]

    # ------------------------------------------------------------------
    def build(self, ui: UI) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        settings = app.settings
        recording = app.state in ("recording", "paused", "starting")
        paused = app.state == "paused"

        # --- preview ---------------------------------------------------
        px0, py0, pw, ph = p(LEFT_X), p(TOP), p(LEFT_W), p(340)
        if app.preview is not None:
            self.preview_item = ui.draw_image(px0, py0, app.preview)
        else:
            self.preview_item = ui.draw_image(px0, py0, tile_image(pw, ph, p(20), "dark"))
            ui.icon(px0 + pw / 2, py0 + ph / 2 - p(14), "monitor", 30, "#FFFFFF30")
            ui.text(px0 + pw / 2, py0 + ph / 2 + p(24), "Предпросмотр появится здесь",
                    11, DIM, anchor="center")

        source_label = app.source_caption()
        chip(ui, px0 + p(14), py0 + ph - p(42), source_label, height=28)

        if recording or paused:
            color = YELLOW if paused else ACCENT
            self.timer_item = chip(ui, px0 + p(14), py0 + p(14),
                                   "ПАУЗА" if paused else fmt_elapsed(app.elapsed()),
                                   dot=color, height=30)
            label_w = p(78)
            chip(ui, px0 + pw - p(14) - label_w, py0 + p(14), f"{settings.fps} к/с",
                 tone="chip", color=TEXT_SOFT, height=28)
        else:
            self.timer_item = None
            chip(ui, px0 + pw - p(14) - p(70), py0 + p(14), f"{settings.fps} к/с",
                 tone="chip", color=TEXT_SOFT, height=28)

        # --- source controls -------------------------------------------
        mode_index = ("monitor", "region", "window").index(settings.source_mode)
        ui.segmented((p(LEFT_X), p(442), p(LEFT_W), p(40)),
                     ["Экран", "Область", "Окно"], mode_index,
                     None if recording else app.set_source_mode,
                     icons_list=["monitor", "crop", "window"], size=11.5)
        self._source_row(ui, recording)
        ui.icon(p(LEFT_X + 12), p(576), "clock", 11, FAINT)
        hint = ("Ctrl+Alt+R — начать или остановить запись"
                if settings.hotkeys else "Горячие клавиши отключены в настройках")
        ui.text(p(LEFT_X + 30), p(576), hint, 10.5, FAINT)

        # --- sound -----------------------------------------------------
        self._sound_card(ui, recording)

        # --- right side: parameters or live stats ----------------------
        if recording:
            self._stats_card(ui)
        else:
            self._params_card(ui)
        self._dest_card(ui)

        # --- footer ----------------------------------------------------
        self._footer(ui, recording, paused)

    # ------------------------------------------------------------------
    def _source_row(self, ui: UI, recording: bool = False) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        settings = app.settings
        y = p(494)
        fw = p(LEFT_W - 52 * 2 - 10 * 2) if not recording else p(LEFT_W - 52)
        mode = settings.source_mode
        icon = {"monitor": "monitor", "region": "crop", "window": "window"}[mode]
        if mode == "monitor":
            cur = winapi.find_monitor(settings.monitor_name, settings.monitor_rect)
            if cur:
                value = f"Экран {cur.index} · {cur.width}×{cur.height}"
                sub = "Основной дисплей" if cur.primary else "Дополнительный дисплей"
            else:
                value, sub = "Экран не найден", "Подключите монитор"
            on_click = lambda: app.open_monitor_menu((p(LEFT_X), y, fw, p(52)))
        elif mode == "region":
            rect = settings.region or {}
            if rect.get("width"):
                value = f"Область {rect['width']}×{rect['height']}"
                sub = f"Позиция {rect.get('left', 0)}, {rect.get('top', 0)}"
            else:
                value = "Выберите область"
                sub = "Перетащите рамку по экрану"
            on_click = app.choose_region
        else:
            info = winapi.find_window(settings.window_hwnd, settings.window_title)
            if info:
                value = info.title
                sub = f"{info.process or 'Приложение'} · {info.rect['width']}×{info.rect['height']}"
            else:
                value = "Выберите окно"
                sub = "Нажмите, чтобы выбрать приложение"
            on_click = lambda: app.open_window_menu((p(LEFT_X), y, fw, p(52)))
        ui.field((p(LEFT_X), y, fw, p(52)), value, sub, icon,
                 None if recording else on_click, chevron=not recording, enabled=not recording)
        bx = p(LEFT_X + LEFT_W - 52 * 2 - 10)
        if not recording:
            refresh_icon = "crop" if mode == "region" else "refresh"
            refresh_click = app.choose_region if mode == "region" else app.refresh_preview
            ui.icon_button(bx + p(26), y + p(26), 52, refresh_icon, refresh_click)
        ui.icon_button(bx + p(52 + 10 + 26) if not recording else p(LEFT_X + LEFT_W - 26),
                       y + p(26), 52, "camera", app.screenshot)

    def _sound_card(self, ui: UI, recording: bool) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        settings = app.settings
        x0, y0 = p(RIGHT_X), p(88)
        ui.text(x0 + p(20), y0 + p(24), "Звук", 13.5, TEXT, "semibold")
        ui.text(x0 + RIGHT_W - p(20), y0 + p(24), "источники", 9.8, FAINT,
                anchor="e")
        rows = (("system", "Системный звук", "speaker", settings.record_system),
                ("mic", "Микрофон", "mic", settings.record_mic))
        for index, (kind, title, icon_name, enabled) in enumerate(rows):
            cy = y0 + p(64 + index * 86)
            device = app.device(kind)
            active = enabled and device is not None
            label_color = TEXT if active else DIM
            sub_color = DIM if enabled else FAINT
            ui.icon(x0 + p(74), cy - p(10), icon_name, 15,
                    MUTED if active else FAINT)
            ui.text(x0 + p(96), cy - p(9), title, 12.3, label_color, "semibold")
            if device:
                sub = device.short_name
                if device.is_default:
                    sub += " · по умолчанию"
                if device.bluetooth:
                    sub += " · Bluetooth"
            else:
                sub = "Устройства не найдены"
                sub_color = "#C28484" if enabled else FAINT
            ui.text(x0 + p(96), cy + p(9), sub, 9.8, sub_color, max_width=p(266))
            if not recording:
                ui.icon(x0 + RIGHT_W - p(24), cy, "chevron_down", 9, FAINT)
            if active:
                self.meters[kind] = ui.meter(
                    (x0 + p(96), cy + p(21), p(248), p(5)), 0.0)
            else:
                self.meters[kind] = ui.meter(
                    (x0 + p(96), cy + p(21), p(248), p(5)), 0.0, active=False)
            if not recording:
                rect = (x0 + p(60), cy - p(30), RIGHT_W - p(80), p(60))
                ui.hit(rect, lambda k=kind: app.open_device_menu(k))
            ui.toggle(x0 + p(20), cy - p(23), active,
                      lambda value, k=kind: app.set_audio_enabled(k, value))
        ui.canvas.create_line(x0 + p(20), y0 + p(107), x0 + RIGHT_W - p(20), y0 + p(107),
                              fill=solid("#FFFFFF12"), width=1)

    def _params_card(self, ui: UI) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        settings = app.settings
        x0, y0 = p(RIGHT_X), p(332)
        ui.text(x0 + p(20), y0 + p(22), "Параметры", 13.5, TEXT, "semibold")

        ui.text(x0 + p(20), y0 + p(54), "Плавность", 11.5, MUTED)
        fps_index = (24, 30, 60).index(settings.fps)
        ui.segmented((x0 + p(198), y0 + p(38), RIGHT_W - p(218), p(32)),
                     ["24", "30", "60"], fps_index, app.set_fps, size=11, radius=10)

        ui.text(x0 + p(20), y0 + p(94), "Качество", 11.5, MUTED)
        q_index = ("high", "balanced", "small").index(settings.quality)
        ui.segmented((x0 + p(150), y0 + p(78), RIGHT_W - p(170), p(32)),
                     ["Высокое", "Баланс", "Компактно"], q_index, app.set_quality,
                     size=10.3, radius=10)

        ui.text(x0 + p(20), y0 + p(134), "Курсор мыши", 11.5, MUTED)
        ui.toggle(x0 + RIGHT_W - p(66), y0 + p(120), settings.show_cursor,
                  lambda value: app.set_bool("show_cursor", value))

    def _stats_card(self, ui: UI) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        x0, y0 = p(RIGHT_X), p(332)
        ui.text(x0 + p(20), y0 + p(22), "Запись", 13.5, TEXT, "semibold")
        stats = app.recorder.stats() if app.recorder else {}
        audio = app.audio_summary()
        items = (
            ("Кадры/с", f"{stats.get('capture_fps', 0):.1f}", x0 + p(20), y0 + p(56)),
            ("Пропущено", f"{stats.get('dropped', 0)}", x0 + p(206), y0 + p(56)),
            ("Кодировщик", app.encoder_name(), x0 + p(20), y0 + p(114)),
            ("Звук", audio, x0 + p(206), y0 + p(114)),
        )
        for label, value, sx, sy in items:
            ui.text(sx, sy, label, 9.8, FAINT)
            item = ui.text(sx, sy + p(19), value, 12.2, TEXT_SOFT, "semibold",
                           max_width=p(170))
            self.stats_items[label] = item

    def _dest_card(self, ui: UI) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        x0, y0 = p(RIGHT_X), p(524)
        path = Path(app.settings.video_dir)
        ui.icon(x0 + p(34), y0 + p(42), "folder", 16, MUTED)
        ui.text(x0 + p(56), y0 + p(32), "Папка записей", 9.8, FAINT)
        ui.text(x0 + p(56), y0 + p(52), str(path), 11.2, TEXT_SOFT,
                "semibold", max_width=p(278))
        ui.icon(x0 + RIGHT_W - p(34), y0 + p(42), "external", 13, FAINT)
        ui.hit((x0, y0, p(RIGHT_W), p(84)), app.open_output_dir)

    def _footer(self, ui: UI, recording: bool, paused: bool) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        cy = p(663)
        x = p(26)
        if recording or paused:
            dot = YELLOW if paused else ACCENT
            ui.icon(x + p(5), cy - p(11), "record", 9, dot)
            ui.text(x + p(18), cy - p(11), "Пауза" if paused else "Идёт запись", 12.8,
                    TEXT, "semibold")
            ui.text(x + p(18), cy + p(9), "Стоп сохранит файл в папку записей",
                    10.5, FAINT)
            pause_click = app.resume if paused else app.pause
            ui.button((p(650), p(638), p(152), p(50)),
                      "Продолжить" if paused else "Пауза",
                      "secondary", "play" if paused else "pause", 15, pause_click,
                      size=12, radius=13)
            ui.button((p(814), p(638), p(220), p(50)), "Остановить", "primary", "stop",
                      14, app.stop, size=13, radius=14)
        else:
            ui.icon(x + p(5), cy - p(11), "record", 8, GREEN)
            ui.text(x + p(18), cy - p(11), "Готов к записи", 12.8, TEXT, "semibold")
            audio = app.audio_summary(verbose=True)
            ui.text(x + p(18), cy + p(9), audio, 10.5, FAINT)
            ui.button((p(814), p(638), p(220), p(50)),
                      "Начать запись", "primary", "record", 14, app.start,
                      size=13, radius=14)

    # ------------------------------------------------------------------
    def tick(self, ui: UI) -> None:
        app = self.app
        recording = app.state in ("recording", "paused", "starting")
        if recording and self.timer_item is not None:
            try:
                ui.canvas.itemconfig(self.timer_item, text=fmt_elapsed(app.elapsed()))
            except Exception:
                pass
        levels = app.levels()
        for kind, meter in self.meters.items():
            ui.update_meter(meter, levels.get(kind, 0.0))
        if self.stats_items and app.recorder is not None:
            stats = app.recorder.stats()
            values = {
                "Кадры/с": f"{stats.get('capture_fps', 0):.1f}",
                "Пропущено": f"{stats.get('dropped', 0)}",
                "Кодировщик": app.encoder_name(),
                "Звук": app.audio_summary(),
            }
            for label, item in self.stats_items.items():
                if label in values:
                    try:
                        ui.canvas.itemconfig(item, text=values[label])
                    except Exception:
                        pass

    def set_preview(self, ui: UI, image: Image.Image) -> None:
        if self.preview_item is None:
            return
        try:
            ui.canvas.itemconfig(self.preview_item, image=ui.photo(image))
        except Exception:
            pass


# ======================================================================
# Gallery
# ======================================================================
class GalleryView(View):
    name = "gallery"
    ROW_H = 82
    GAP = 10
    VISIBLE = 5

    def __init__(self, app):
        super().__init__(app)
        self.thumb_items = {}

    def build(self, ui: UI) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        items = app.gallery
        total_size = sum(entry.get("size", 0) for entry in items)
        ui.text(p(26), p(112), "Галерея", 24, TEXT, "semibold")
        subtitle = (f"{len(items)} записей · {fmt_size(total_size)}" if items
                    else "Здесь появятся ваши записи")
        ui.text(p(26), p(138), subtitle, 11, DIM)

        ui.button((p(846), p(94), p(140), p(40)), "Открыть папку", "secondary",
                  "folder", 14, app.open_output_dir, size=11.5, radius=11)
        ui.icon_button(p(1014), p(114), 40, "refresh", app.refresh_gallery)

        if not items:
            self._empty(ui)
            return

        start = app.gallery_scroll * (self.ROW_H + self.GAP)
        top = 160 - start
        for index, entry in enumerate(items):
            y = top + index * (self.ROW_H + self.GAP)
            if y < 160 - 1 or y + self.ROW_H > 620:
                continue
            self._row(ui, entry, y)

        if len(items) > self.VISIBLE:
            track_x = p(1032)
            track_top, track_bottom = p(160), p(610)
            ui.canvas.create_line(track_x, track_top, track_x, track_bottom,
                                  fill=solid("#FFFFFF14"), width=max(1, p(2)), capstyle="round")
            ratio = self.VISIBLE / len(items)
            thumb_h = (track_bottom - track_top) * ratio
            maximum = max(1, len(items) - self.VISIBLE)
            pos = app.gallery_scroll / maximum
            ty = track_top + (track_bottom - track_top - thumb_h) * pos
            ui.canvas.create_line(track_x, ty, track_x, ty + thumb_h,
                                  fill=solid("#FFFFFF55"), width=max(1, p(2)), capstyle="round")

    def _row(self, ui: UI, entry: dict, y: float) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        x, w, h = p(26), p(1008), p(self.ROW_H)
        ui.draw_image(x, y, tile_image(w, h, p(16), "glass"))
        hover = ui.draw_image(x, y, tile_image(w, h, p(16), "raised"))
        ui.canvas.itemconfig(hover, state="hidden")

        thumb_w, thumb_h = p(118), p(66)
        tx, ty = x + p(8), y + (h - thumb_h) / 2
        thumb = entry.get("thumb")
        if thumb is not None:
            ui.draw_image(tx, ty, thumb)
        else:
            ui.draw_image(tx, ty, tile_image(thumb_w, thumb_h, p(9), "dark"))
            ui.icon(tx + thumb_w / 2, ty + thumb_h / 2, "monitor", 18, "#FFFFFF28")

        duration = entry.get("duration") or 0
        title = entry.get("name", "")
        path = entry.get("path")

        def enter() -> None:
            ui.canvas.itemconfig(hover, state="normal")

        def leave() -> None:
            ui.canvas.itemconfig(hover, state="hidden")

        row_hit = ui.hit((x, y, w, h), lambda path=path: app.play_recording(path))
        row_hit.on_hover = lambda state, e=enter, l=leave: (e() if state else l())

        ui.text(tx + thumb_w + p(16), y + p(28), title, 12.8, TEXT, "semibold",
                max_width=p(520))
        meta_parts = []
        if duration:
            meta_parts.append(fmt_elapsed(duration))
        meta_parts.append(fmt_size(entry.get("size", 0)))
        meta_parts.append(fmt_date(entry.get("mtime", 0)))
        ui.text(tx + thumb_w + p(16), y + p(50), " · ".join(meta_parts), 10.2, DIM)

        # action buttons (added after the row hit, so they win hit testing)
        trash_cx = x + w - p(30)
        folder_cx = trash_cx - p(40)
        play_cx = folder_cx - p(40)
        ui.icon_button(trash_cx, y + h / 2, 34, "trash",
                       lambda path=path, name=title: app.delete_recording(path, name),
                       kind="ghost", icon_color="#C98A8F")
        ui.icon_button(folder_cx, y + h / 2, 34, "folder",
                       lambda path=path: app.reveal_file(path),
                       kind="ghost", icon_color=MUTED)
        ui.icon_button(play_cx, y + h / 2, 34, "play",
                       lambda path=path: app.play_recording(path),
                       kind="secondary", icon_color=TEXT)

    def _empty(self, ui: UI) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        cx, cy = p(530), p(348)
        for radius, alpha in ((102, "#FFFFFF10"), (74, "#FFFFFF16"), (48, "#FFFFFF22")):
            r = p(radius)
            ui.canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                  outline=solid(alpha), width=max(1, p(1)))
        ui.icon(cx, cy, "sparkle", 34, "#FFFFFF55")
        ui.text(cx, cy + p(132), "Пока нет записей", 19, TEXT, "semibold", anchor="center")
        ui.text(cx, cy + p(158), "Начните первую запись — она появится здесь", 11.5,
                DIM, anchor="center")
        ui.button((cx - p(100), cy + p(188), p(200), p(46)), "Начать запись", "primary",
                  "record", 14, app.start, size=12.5)
        ui.button((cx - p(76), cy + p(244), p(152), p(36)), "Открыть папку", "ghost",
                  "folder", 13, app.open_output_dir, size=10.8, radius=10)


# ======================================================================
# Settings
# ======================================================================
class SettingsView(View):
    name = "settings"

    def panels(self) -> list[Panel]:
        return [
            Panel(26, 148, 490, 300, 22),
            Panel(26, 460, 490, 148, 22),
            Panel(546, 148, 488, 252, 22),
            Panel(546, 412, 488, 196, 22),
        ]

    def build(self, ui: UI) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        settings = app.settings
        ui.text(p(26), p(112), "Настройки", 24, TEXT, "semibold")
        ui.text(p(26), p(138), "Изменения сохраняются автоматически", 11, DIM)
        ui.button((p(884), p(94), p(150), p(40)), "Сбросить", "secondary", "refresh", 13,
                  app.reset_settings, size=10.8, radius=11)

        # ---- video ----------------------------------------------------
        x0, y0 = p(26), p(148)
        ui.text(x0 + p(24), y0 + p(30), "Видео", 13.5, TEXT, "semibold")
        rows = [
            ("Плавность", 74), ("Качество", 120), ("Кодировщик", 166),
            ("Формат", 212), ("Курсор мыши", 258),
        ]
        for label, yy in rows:
            ui.text(x0 + p(24), y0 + p(yy), label, 11.8, MUTED)
        ui.segmented((x0 + p(202), y0 + p(58), p(124), p(32)), ["24", "30", "60"],
                     (24, 30, 60).index(settings.fps), app.set_fps, size=11, radius=10)
        ui.segmented((x0 + p(156), y0 + p(104), p(170), p(32)),
                     ["Высокое", "Баланс", "Компактно"],
                     ("high", "balanced", "small").index(settings.quality),
                     app.set_quality, size=10, radius=10)
        ui.segmented((x0 + p(202), y0 + p(150), p(124), p(32)), ["Авто", "CPU"],
                     ("auto", "software").index(settings.encoder),
                     lambda index: app.set_encoder(("auto", "software")[index]),
                     size=10.5, radius=10)
        ui.segmented((x0 + p(202), y0 + p(196), p(124), p(32)), ["MP4", "MKV"],
                     ("mp4", "mkv").index(settings.format),
                     lambda index: app.set_format(("mp4", "mkv")[index]),
                     size=10.5, radius=10)
        ui.toggle(x0 + p(424), y0 + p(246), settings.show_cursor,
                  lambda value: app.set_bool("show_cursor", value))

        # ---- folder ---------------------------------------------------
        x0, y0 = p(26), p(460)
        ui.text(x0 + p(24), y0 + p(30), "Папка сохранения", 13.5, TEXT, "semibold")
        path = Path(settings.video_dir)
        ui.text(x0 + p(24), y0 + p(56), str(path), 10.5, DIM, max_width=p(430))
        ui.button((x0 + p(252), y0 + p(96), p(108), p(36)), "Изменить", "secondary",
                  "folder", 13, app.pick_output_dir, size=10.8, radius=10)
        ui.button((x0 + p(368), y0 + p(96), p(98), p(36)), "Открыть", "ghost",
                  "external", 12, app.open_output_dir, size=10.8, radius=10)

        # ---- audio ----------------------------------------------------
        x0, y0 = p(546), p(148)
        ui.text(x0 + p(24), y0 + p(30), "Звук", 13.5, TEXT, "semibold")
        self._gain_row(ui, "system", "Системный звук", x0, y0, 74, settings.system_gain)
        self._gain_row(ui, "mic", "Микрофон", x0, y0, 146, settings.mic_gain)

        # ---- behaviour ------------------------------------------------
        x0, y0 = p(546), p(412)
        ui.text(x0 + p(24), y0 + p(30), "Поведение", 13.5, TEXT, "semibold")
        ui.text(x0 + p(24), y0 + p(68), "Обратный отсчёт", 11.5, MUTED)
        ui.segmented((x0 + p(322), y0 + p(52), p(140), p(32)), ["0", "3", "5"],
                     (0, 3, 5).index(settings.countdown), app.set_countdown,
                     size=11, radius=10)
        ui.text(x0 + p(24), y0 + p(106), "Скрывать окно при записи", 11.5, MUTED)
        ui.toggle(x0 + p(418), y0 + p(92), settings.hide_on_record,
                  lambda value: app.set_bool("hide_on_record", value))
        ui.text(x0 + p(24), y0 + p(144), "Горячие клавиши", 11.5, MUTED)
        ui.toggle(x0 + p(418), y0 + p(130), settings.hotkeys,
                  lambda value: app.set_bool("hotkeys", value))
        ui.text(x0 + p(24), y0 + p(182), "Открывать папку после записи", 11.5, MUTED)
        ui.toggle(x0 + p(418), y0 + p(168), settings.open_folder_after,
                  lambda value: app.set_bool("open_folder_after", value))

        # ---- footer ---------------------------------------------------
        cy = p(663)
        info = f"SR Pro 4.0 · FFmpeg {app.ffmpeg_version}"
        ui.text(p(26), cy, info, 10.5, FAINT, max_width=p(620))
        hw = app.hw_encoder
        ui.text(p(1034), cy, "Аппаратное ускорение: " + (hw or "нет"), 10.5, FAINT,
                anchor="e")

    def _gain_row(self, ui: UI, kind: str, label: str, x0: float, y0: float,
                  offset: float, value: float) -> None:
        app, t, p = self.app, self.app.t, self.app.t.px
        device = app.device(kind)
        ui.text(x0 + p(24), y0 + p(offset), label, 11.5,
                TEXT_SOFT if device else DIM, "semibold")
        value_item = ui.text(x0 + p(464), y0 + p(offset), f"{round(value * 100)}%",
                             11, MUTED if device else FAINT, "semibold", anchor="e")
        note = device.short_name if device else "Устройство не найдено"
        ui.text(x0 + p(24), y0 + p(offset + 16), note, 9.3,
                DIM if device else "#B98282", max_width=p(300))

        def change(v: float, item=value_item) -> None:
            app.set_gain(kind, v)
            try:
                ui.canvas.itemconfig(item, text=f"{round(v * 100)}%")
            except Exception:
                pass

        ui.slider((x0 + p(24), y0 + p(offset + 38), p(310), p(18)), value, change,
                  minimum=0.0, maximum=2.0, step=0.05, enabled=device is not None)
        ui.button((x0 + p(364), y0 + p(offset + 30), p(90), p(30)), "Тест", "secondary",
                  None, 12, lambda k=kind: app.test_audio(k), size=10.3, radius=9,
                  enabled=device is not None)
