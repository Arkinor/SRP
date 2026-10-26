"""Application shell: frameless glass window, views, recorder glue and hotkeys."""
from __future__ import annotations

import ctypes
import hashlib
import math
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageOps, ImageTk

from .. import audio, ffmpeg_tools, logger, video, winapi
from ..config import (
    APP_NAME, LOG_DIR, THUMB_DIR, VERSION, Settings, ensure_dirs,
)
from ..recorder import Recorder, build_source, capture_screenshot
from . import icons
from .kit import Panel, PopupItem, UI, build_background, tile_image
from .overlays import RegionSelector
from .theme import (
    ACCENT, ACCENT_LIGHT, BLUE, DIM, FAINT, GREEN, MUTED, PURPLE, RED, TEXT, TEXT_SOFT,
    WHITE, YELLOW, Theme, glow, logo, round_image, solid,
)
from .views import GalleryView, RecordView, SettingsView, W, H, fmt_elapsed, fmt_size

LOG = logger.get()
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class PreviewWorker(threading.Thread):
    """Grabs a downscaled snapshot of the current source for the preview card."""

    def __init__(self, app: "App"):
        super().__init__(name="Preview", daemon=True)
        self.app = app
        self.stop_event = threading.Event()
        self.request = threading.Event()
        self._last = 0.0

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.request.wait(timeout=0.12)
            requested = self.request.is_set()
            self.request.clear()
            if self.stop_event.is_set():
                break
            now = time.perf_counter()
            interval = 2.2 if self.app.state in ("recording", "paused") else 1.6
            if not requested and now - self._last < interval:
                continue
            if not self.app.preview_visible():
                self._last = now
                continue
            self._last = now
            try:
                image = self.app.capture_preview()
            except Exception:
                LOG.debug("Ошибка предпросмотра", exc_info=True)
                continue
            if image is not None and not self.stop_event.is_set():
                self.app.events.put(("preview", image))

    def close(self) -> None:
        self.stop_event.set()
        self.request.set()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = Settings.load()
        ensure_dirs()

        work = winapi.workarea()
        dpi = winapi.dpi_for_window(0)
        self.scale = max(0.80, min(1.55, dpi, (work[2] - 36) / W, (work[3] - 36) / H))
        self.t = Theme(self.scale)
        self.W, self.H = self.t.px(W), self.t.px(H)
        self.p = self.t.px

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.view_name = "record"
        self.state = "idle"
        self.menu_state: Optional[dict] = None
        self.confirm_state: Optional[dict] = None
        self.toast_state: Optional[dict] = None
        self.countdown_end = 0.0
        self.countdown_shown = -1
        self.finalize_progress = 0.0
        self._progress_card = None
        self.recorder: Optional[Recorder] = None
        self.preview: Optional[Image.Image] = None
        self.audio_devices: dict[str, list[audio.AudioDevice]] = {"system": [], "mic": []}
        self.audio_error = ""
        self.gallery: list[dict] = []
        self.gallery_scroll = 0
        self.gallery_dirty = False
        self._gallery_gen = 0
        self._last_gallery_build = 0.0
        self.ffmpeg_version = "…"
        self.hw_encoder: Optional[str] = None
        self._hw_ready = threading.Event()
        self.progress_subtitle = ""

        self._levels = {"system": 0.0, "mic": 0.0}
        self._hover = None
        self._active = None
        self._pressed_at = (0, 0)
        self._moved = False
        self._win_drag = None
        self._region_selector: Optional[RegionSelector] = None
        self._minimized_for_record = False
        self._quit_after_save = False
        self._save_job = None
        self._hotkeys: list = []
        self._hotkey_thread = None
        self._closing = False
        self._shot_busy = False
        self._last_start_error = ""

        self._logo = logo(self.p(38))
        self._logo64 = logo(64)
        self._icon_photo = ImageTk.PhotoImage(self._logo64)

        self._setup_window()
        self.views = {"record": RecordView(self), "gallery": GalleryView(self),
                      "settings": SettingsView(self)}
        self.current_view = self.views["record"]
        self.ui = UI(self.canvas, self.t, (self.W, self.H))
        self.backgrounds: dict[str, Image.Image] = {}

        self._start_preview()
        self.refresh_devices()
        self.refresh_gallery()
        self._detect_tools()
        self._install_hotkeys()

        self.build()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.root.after(60, self._tick)
        self.root.after(400, self._request_preview)
        LOG.info("SR Pro %s запущен, масштаб %.2f, окно %dx%d", VERSION, self.scale,
                 self.W, self.H)

    # ------------------------------------------------------------------
    # window
    def _setup_window(self) -> None:
        root = self.root
        root.withdraw()
        root.title(APP_NAME)
        root.overrideredirect(True)
        root.configure(bg="#05060B")
        root.attributes("-topmost", False)

        work = winapi.workarea()
        x = work[0] + max(0, (work[2] - self.W) // 2)
        y = work[1] + max(0, (work[3] - self.H) // 2)
        root.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self.canvas = tk.Canvas(root, width=self.W, height=self.H, bd=0,
                                highlightthickness=0, bg="#05060B")
        self.canvas.pack(fill="both", expand=True)
        root.update_idletasks()

        self.hwnd = winapi.get_hwnd(root)
        winapi.make_taskbar_window(self.hwnd)
        winapi.set_round_region(self.hwnd, self.W, self.H, self.p(26))
        try:
            root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        root.bind("<Escape>", self._on_escape)
        root.bind("<Map>", self._on_map)

    # ------------------------------------------------------------------
    # rendering
    def build(self) -> None:
        if self._closing:
            return
        ui = self.ui
        ui.clear()
        self._hover = None
        self._active = None
        try:
            self.canvas.config(cursor="arrow")
        except Exception:
            pass

        bg = self.backgrounds.get(self.view_name)
        if bg is None or bg.size != (self.W, self.H):
            bg = build_background(
                self.W, self.H, self.t, self.views[self.view_name].panels(),
                header_line=72, footer_line=622,
            )
            self.backgrounds[self.view_name] = bg
        ui.background(bg)
        self._draw_header()
        self.current_view = self.views[self.view_name]
        self.current_view.build(ui)

        if self.state == "starting":
            ui.wait_card("Готовим запись", "Подключаем видео и звуковые устройства…")
        elif self.state == "finalizing":
            self._progress_card = ui.progress_card(
                "Сохраняем запись",
                self.progress_subtitle or "Собираем видео и звук в один файл…",
            )
            self.progress_subtitle = ""
        elif self.state == "countdown":
            self._draw_countdown()
        if self.confirm_state:
            ui.modal(self.confirm_state["title"], self.confirm_state["message"],
                     self.confirm_state["buttons"])
        elif self.menu_state:
            state = self.menu_state
            ui.menu(state["anchor"], state["items"], self._menu_pick,
                    selected=state.get("selected"), title=state.get("title", ""),
                    offset=state.get("offset", 0), on_dismiss=self.dismiss_menu,
                    max_rows=8)
        if self.toast_state and self.toast_state["until"] > time.time():
            ui.toast(self.toast_state["text"], self.toast_state["icon"],
                     self.toast_state["color"])

    def _draw_header(self) -> None:
        ui, p = self.ui, self.p
        ui.draw_image(p(26), p(17), self._logo)
        ui.text(p(78), p(30), "SR Pro", 17.5, TEXT, "semibold")
        ui.text(p(79), p(47), "SCREEN RECORDER", 7.6, "#8F98AD")
        index = ("record", "gallery", "settings").index(self.view_name)
        ui.segmented((p(366), p(18), p(328), p(36)),
                     ["Запись", "Галерея", "Настройки"], index, self.switch_view,
                     size=11.2, radius=11)
        ui.icon_button(p(1019), p(36), 32, "close", self.request_close,
                       kind="ghost", icon_color=MUTED)
        ui.icon_button(p(981), p(36), 32, "minimize", self.minimize,
                       kind="ghost", icon_color=MUTED)

    def _draw_countdown(self) -> None:
        ui, p = self.ui, self.p
        ui.dim()
        cx, cy = self.W // 2, self.H // 2 - p(10)
        left = max(0.0, self.countdown_end - time.perf_counter())
        number = max(1, int(math.ceil(left)))
        diameter = p(170)
        halo = glow(diameter, diameter, (255, 95, 109), 0.38, blur=p(34))
        ui.draw_image(cx - halo.width / 2, cy - halo.height / 2, halo)
        ui.draw_image(cx - diameter / 2, cy - diameter / 2,
                      tile_image(diameter, diameter, diameter // 2, "dark"))
        ui.canvas.create_oval(cx - diameter / 2 + p(3), cy - diameter / 2 + p(3),
                              cx + diameter / 2 - p(3), cy + diameter / 2 - p(3),
                              outline=solid("#FF5F6D66"), width=p(1.5))
        ui.text(cx, cy + p(2), str(number), 62, TEXT, "light", anchor="center")
        ui.text(cx, cy - diameter / 2 - p(34), "Запись начнётся через",
                11.5, MUTED, "semibold", anchor="center")
        ui.button((cx - p(70), cy + diameter / 2 + p(28), p(140), p(40)),
                  "Отмена", "ghost", None, 13, self.cancel_countdown, size=11)

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
            self.root.geometry(f"+{self._win_drag[2] + dx}+{self._win_drag[3] + dy}")
            return
        if self._active is not None and self._active.on_drag is not None:
            try:
                self._active.on_drag(x, y)
            except Exception:
                pass
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
        self._pressed_at = (x, y)
        self._moved = False
        hit = self._hit_at(x, y)
        if hit is not None:
            self._active = hit
            if hit.on_press:
                try:
                    hit.on_press(x, y)
                except Exception:
                    LOG.debug("on_press", exc_info=True)
            return
        self._active = None
        if y < self.p(72) and self.state in ("idle", "recording", "paused"):
            self._win_drag = (x, y, self.root.winfo_x(), self.root.winfo_y())

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
                    LOG.debug("on_release", exc_info=True)
            if not self._moved and hit.contains(x, y) and hit.on_click:
                try:
                    hit.on_click()
                except Exception:
                    LOG.exception("Ошибка нажатия")
            self._hover = None

    def _on_wheel(self, event) -> None:
        if self.menu_state:
            items = self.menu_state["items"]
            maximum = max(0, len(items) - 8)
            delta = -1 if event.delta > 0 else 1
            offset = max(0, min(maximum, self.menu_state.get("offset", 0) + delta))
            if offset != self.menu_state.get("offset", 0):
                self.menu_state["offset"] = offset
                self.build()
            return
        if self.view_name != "gallery" or not self.gallery:
            return
        maximum = max(0, len(self.gallery) - GalleryView.VISIBLE)
        delta = -1 if event.delta > 0 else 1
        value = max(0, min(maximum, self.gallery_scroll + delta))
        if value != self.gallery_scroll:
            self.gallery_scroll = value
            self.build()

    def _on_escape(self, _event=None) -> None:
        if self.state == "countdown":
            self.cancel_countdown()
        elif self.confirm_state:
            self.dismiss_confirm()
        elif self.menu_state:
            self.dismiss_menu()

    def _on_map(self, _event=None) -> None:
        self.root.after(80, self._after_restore)

    def _after_restore(self) -> None:
        winapi.set_round_region(self.hwnd, self.W, self.H, self.p(26))
        self._request_preview()

    # ------------------------------------------------------------------
    # navigation / window actions
    def switch_view(self, index_or_name) -> None:
        name = (index_or_name if isinstance(index_or_name, str)
                else ("record", "gallery", "settings")[index_or_name])
        if name == self.view_name:
            return
        self.view_name = name
        self.menu_state = None
        self.confirm_state = None
        self.gallery_scroll = 0
        if name == "gallery":
            self.refresh_gallery()
        self._apply_capture_exclusion()
        self.build()
        if name == "record":
            self._request_preview()

    def minimize(self) -> None:
        winapi.minimize(self.hwnd)

    def request_close(self) -> None:
        if self._closing:
            return
        if self.state in ("starting", "finalizing"):
            self.toast("Дождитесь завершения операции", icon="clock", color=YELLOW)
            return
        if self.state in ("recording", "paused"):
            self.ask("Остановить запись?",
                     "Текущая запись будет остановлена и сохранена, после чего программа закроется.",
                     [("Отмена", "ghost", self.dismiss_confirm),
                      ("Остановить и выйти", "primary", self._close_during_record)])
            return
        self.close()

    def _close_during_record(self) -> None:
        self._quit_after_save = True
        self.dismiss_confirm()
        self.stop()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.save_now()
        self._remove_hotkeys()
        if self._preview is not None:
            self._preview.close()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # background info
    def _detect_tools(self) -> None:
        def worker() -> None:
            version = ffmpeg_tools.version()
            hw = None
            if self.settings.encoder == "auto":
                hw = ffmpeg_tools.hw_encoder()
            LOG.info("FFmpeg: %s (%s)", ffmpeg_tools.find() or "не найден", version)
            self.events.put(("about", (version, hw)))

        threading.Thread(target=worker, name="Detect", daemon=True).start()

    def refresh_devices(self) -> None:
        def worker() -> None:
            system, mics, error = audio.list_devices()
            self.events.put(("devices", (system, mics, error)))

        threading.Thread(target=worker, name="Devices", daemon=True).start()

    def _start_preview(self) -> None:
        self._preview = PreviewWorker(self)
        self._preview.start()

    def _request_preview(self) -> None:
        worker = getattr(self, "_preview", None)
        if worker is not None:
            worker.request.set()

    def preview_visible(self) -> bool:
        try:
            if winapi.user32.IsIconic(self.hwnd):
                return False
        except Exception:
            pass
        return self.view_name == "record"

    def capture_preview(self) -> Optional[Image.Image]:
        spec, _error = build_source(self.settings)
        if spec is None:
            return None
        image = video.preview_png(spec, width=max(440, self.p(604)))
        if image is None:
            return None
        target = (self.p(604), self.p(340))
        image = ImageOps.fit(image.convert("RGB"), target, Image.LANCZOS,
                             centering=(0.5, 0.5))
        return round_image(image, self.p(20), border=(255, 255, 255, 40), border_width=1)

    def _apply_capture_exclusion(self) -> None:
        excluded = (self.view_name == "record"
                    or self.state in ("recording", "paused", "starting", "countdown",
                                      "finalizing"))
        try:
            winapi.set_capture_excluded(self.hwnd, excluded)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # recording
    def elapsed(self) -> float:
        return self.recorder.elapsed() if self.recorder else 0.0

    def levels(self) -> dict:
        return dict(self._levels)

    def encoder_name(self) -> str:
        if self.settings.encoder == "software":
            return "libx264"
        if self.hw_encoder:
            return self.hw_encoder
        return "libx264" if self._hw_ready.is_set() else "H.264"

    def start(self) -> None:
        if self.state == "countdown":
            self.cancel_countdown()
            return
        if self.state != "idle":
            return
        if not ffmpeg_tools.find():
            self.toast("FFmpeg не найден рядом с программой", icon="warning", color=RED)
            return
        source, error = build_source(self.settings)
        if source is None:
            self.toast(error, icon="warning", color=RED)
            return
        self.settings.save()
        self.menu_state = None
        if self.settings.countdown > 0:
            self.countdown_end = time.perf_counter() + self.settings.countdown
            self.countdown_shown = -1
            self.state = "countdown"
            winapi.beep(760, 90)
            self.build()
        else:
            self._begin_recording()

    def cancel_countdown(self) -> None:
        if self.state != "countdown":
            return
        self.state = "idle"
        self.countdown_end = 0.0
        self.build()

    def _begin_recording(self) -> None:
        self.state = "starting"
        self.build()
        winapi.beep(980, 90)

        def worker() -> None:
            self._hw_ready.wait(timeout=3.0)
            try:
                recorder = Recorder(self.settings, on_event=self._recorder_event)
                self.recorder = recorder
                ok, error = recorder.start()
            except Exception as exc:  # pragma: no cover - defensive
                LOG.exception("Не удалось запустить запись")
                ok, error = False, str(exc)
            self.events.put(("start_result", (ok, error)))

        threading.Thread(target=worker, name="Start", daemon=True).start()

    def _recorder_event(self, kind: str, payload: object) -> None:
        self.events.put(("rec", kind, payload))

    def _on_start_result(self, ok: bool, error: str) -> None:
        if not ok:
            self.recorder = None
            self.state = "idle"
            self._apply_capture_exclusion()
            self.build()
            self.toast(error or "Не удалось начать запись", icon="warning", color=RED)
            winapi.beep(320, 160)
            return
        self.state = "recording"
        self._apply_capture_exclusion()
        self.build()
        self._request_preview()
        if self.settings.hide_on_record:
            self._minimized_for_record = True
            self.root.after(180, self.minimize)

    def pause(self) -> None:
        if self.state != "recording" or self.recorder is None:
            return
        self.recorder.pause()
        self.state = "paused"
        self.build()

    def resume(self) -> None:
        if self.state != "paused" or self.recorder is None:
            return
        self.recorder.resume()
        self.state = "recording"
        self.build()

    def stop(self) -> None:
        if self.state not in ("recording", "paused") or self.recorder is None:
            return
        recorder = self.recorder
        self.state = "finalizing"
        self.finalize_progress = 0.0
        self.progress_subtitle = "Собираем видео и звук в один файл…"
        recorder.stop()
        recorder.finalize_async(
            progress=lambda value: self.events.put(("progress", value)),
            done=lambda result, error: self.events.put(("finalized", (result, error))),
        )
        self.build()

    def _on_finalized(self, result, warning: str) -> None:
        self.state = "idle"
        self.finalize_progress = 0.0
        self.recorder = None
        if self._minimized_for_record:
            self._minimized_for_record = False
            winapi.restore(self.hwnd)
        self._apply_capture_exclusion()
        self.build()
        if result is None:
            self.toast(warning or "Не удалось сохранить запись", icon="warning", color=RED)
            winapi.beep(320, 160)
            if self._quit_after_save:
                self.close()
            return
        size = fmt_size(result.size)
        message = f"Сохранено · {fmt_elapsed(result.duration)} · {size}"
        if warning:
            self.toast(warning, icon="warning", color=YELLOW)
        else:
            self.toast(message, icon="check", color=GREEN)
        winapi.beep(1040, 90)
        self.refresh_gallery()
        if self.settings.open_folder_after:
            self.open_output_dir()
        if self._quit_after_save:
            self.close()

    # ------------------------------------------------------------------
    # audio
    def device(self, kind: str) -> Optional[audio.AudioDevice]:
        pool = self.audio_devices.get(kind) or []
        if not pool:
            return None
        device_id = (self.settings.system_device_id if kind == "system"
                     else self.settings.mic_device_id)
        if device_id:
            for device in pool:
                if device.id == device_id:
                    return device
        for device in pool:
            if device.is_default:
                return device
        return pool[0]

    def audio_summary(self, verbose: bool = False) -> str:
        if self.recorder is not None and self.state in ("recording", "paused"):
            captures = [c for c in self.recorder.audio_captures if c.rate and c.frames]
            if captures:
                rate = max(c.rate for c in captures)
                channels = max(c.channels for c in captures)
                return f"{rate / 1000:.0f} кГц · {channels} кан."
            return "Без звука"
        parts = []
        if self.settings.record_system and self.device("system"):
            parts.append("системный звук")
        if self.settings.record_mic and self.device("mic"):
            parts.append("микрофон")
        if not parts:
            return "Звук: без звука" if verbose else "Без звука"
        return ("Звук: " if verbose else "") + " + ".join(parts)

    def set_audio_enabled(self, kind: str, value: bool) -> None:
        if value and not self.device(kind):
            self.refresh_devices()
            self.toast("Устройство не найдено. Подключите его и обновите список.",
                       "warning", YELLOW)
            return
        if kind == "system":
            self.settings.record_system = value
            if value and not self.device("system"):
                self.refresh_devices()
        else:
            self.settings.record_mic = value
            if value and not self.device("mic"):
                self.refresh_devices()
        self._save_soon()
        self.build()

    def set_gain(self, kind: str, value: float) -> None:
        if kind == "system":
            self.settings.system_gain = float(value)
        else:
            self.settings.mic_gain = float(value)
        self._save_soon()

    def test_audio(self, kind: str) -> None:
        device = self.device(kind)
        if device is None:
            self.toast("Аудиоустройство не найдено", icon="warning", color=RED)
            return
        self.toast(f"Проверяем «{device.short_name}»…", icon="mic", color=BLUE, seconds=4)

        def worker() -> None:
            report = audio.capture_report(device, 2.2)
            self.events.put(("audio_test", (kind, report)))

        threading.Thread(target=worker, name="AudioTest", daemon=True).start()

    def open_device_menu(self, kind: str, anchor) -> None:
        pool = self.audio_devices.get(kind) or []
        if not pool:
            self.refresh_devices()
        items = [PopupItem("Не записывать", None, sub="Отключить этот источник")]
        current = self.device(kind)
        for device in pool:
            sub = device.name
            if device.is_default:
                sub += " · по умолчанию"
            if device.bluetooth:
                sub += " · Bluetooth"
            items.append(PopupItem(device.short_name, device.id, sub=sub,
                                   icon="speaker" if kind == "system" else "mic"))
        if not pool:
            items.append(PopupItem("Устройства не найдены", "__none__",
                                   sub="Подключите устройство и откройте меню снова",
                                   icon="warning"))

        def pick(index: int) -> None:
            item = items[index]
            if item.value == "__none__":
                return
            if item.value is None:
                if kind == "system":
                    self.settings.record_system = False
                else:
                    self.settings.record_mic = False
            else:
                if kind == "system":
                    self.settings.system_device_id = str(item.value)
                    self.settings.record_system = True
                else:
                    self.settings.mic_device_id = str(item.value)
                    self.settings.record_mic = True
            self.save_now()

        self.open_menu(anchor, items, current.id if current else None,
                       "Системный звук" if kind == "system" else "Микрофон", pick)

    # ------------------------------------------------------------------
    # menus
    def open_menu(self, anchor, items: list[PopupItem], selected, title: str,
                  on_pick: Callable[[int], None]) -> None:
        if not items:
            return
        self.confirm_state = None
        self.menu_state = {
            "anchor": anchor, "items": items, "selected": selected,
            "title": title, "on_pick": on_pick, "offset": 0,
        }
        self.build()

    def _menu_pick(self, index: int) -> None:
        state = self.menu_state
        self.menu_state = None
        if state and state.get("on_pick"):
            try:
                state["on_pick"](index)
            except Exception:
                LOG.exception("Ошибка выбора в меню")
        self.build()

    def dismiss_menu(self) -> None:
        if self.menu_state:
            self.menu_state = None
            self.build()

    def open_monitor_menu(self, anchor) -> None:
        monitors = winapi.monitors()
        items = []
        for monitor in monitors:
            sub = "Основной монитор" if monitor.primary else "Дополнительный монитор"
            items.append(PopupItem(f"Экран {monitor.index} · {monitor.width}×{monitor.height}",
                                   monitor.device, sub=sub, icon="monitor"))
        current = winapi.find_monitor(self.settings.monitor_name, self.settings.monitor_rect)

        def pick(index: int) -> None:
            monitor = monitors[index]
            self.settings.monitor_name = monitor.device
            self.settings.monitor_rect = monitor.rect
            self.settings.source_mode = "monitor"
            self.save_now()
            self._request_preview()

        self.open_menu(anchor, items, current.device if current else None,
                       "Монитор", pick)

    def open_window_menu(self, anchor) -> None:
        windows = winapi.list_windows()
        if not windows:
            self.toast("Не удалось найти открытые окна", icon="warning", color=YELLOW)
            return
        windows.sort(key=lambda w: w.title.lower())
        items = []
        for window in windows:
            sub = f"{window.process or 'Приложение'} · {window.rect['width']}×{window.rect['height']}"
            items.append(PopupItem(window.label, window.hwnd, sub=sub, icon="window"))

        def pick(index: int) -> None:
            window = windows[index]
            self.settings.window_hwnd = window.hwnd
            self.settings.window_title = window.title
            self.settings.source_mode = "window"
            self.save_now()
            self._request_preview()

        self.open_menu(anchor, items, self.settings.window_hwnd or None,
                       "Окна на экране", pick)

    # ------------------------------------------------------------------
    # settings callbacks
    def set_source_mode(self, index: int) -> None:
        mode = ("monitor", "region", "window")[index]
        self.settings.source_mode = mode
        self.save_now()
        self.build()
        if mode == "region" and not (self.settings.region or {}).get("width"):
            self.root.after(120, self.choose_region)
        elif mode == "window" and not self.settings.window_hwnd:
            self.build()
        self._request_preview()

    def set_fps(self, index: int) -> None:
        self.settings.fps = (24, 30, 60)[index]
        self.save_now()
        self.build()

    def set_quality(self, index: int) -> None:
        self.settings.quality = ("high", "balanced", "small")[index]
        self.save_now()
        self.build()

    def set_encoder(self, value: str) -> None:
        self.settings.encoder = value
        self.save_now()
        if value == "auto" and not self._hw_ready.is_set():
            self._detect_tools()
        self.build()

    def set_format(self, value: str) -> None:
        self.settings.format = value
        self.save_now()
        self.build()

    def set_countdown(self, index: int) -> None:
        self.settings.countdown = (0, 3, 5)[index]
        self.save_now()
        self.build()

    def set_bool(self, name: str, value: bool) -> None:
        setattr(self.settings, name, bool(value))
        self.save_now()
        if name == "hotkeys":
            self._install_hotkeys()
            self.build()

    def reset_settings(self) -> None:
        def apply() -> None:
            keep_dir = self.settings.video_dir
            self.settings = Settings()
            self.settings.video_dir = keep_dir
            self.settings.save()
            self._install_hotkeys()
            self.toast("Настройки сброшены", icon="refresh", color=BLUE)
            self.build()

        self.ask("Сбросить настройки?",
                 "Все параметры вернутся к значениям по умолчанию. Записи не будут затронуты.",
                 [("Отмена", "ghost", self.dismiss_confirm),
                  ("Сбросить", "primary", apply)])

    def _save_soon(self) -> None:
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(600, self.save_now)

    def save_now(self) -> None:
        self._save_job = None
        try:
            self.settings.save()
        except Exception:
            LOG.exception("Не удалось сохранить настройки")

    # ------------------------------------------------------------------
    # modal helpers
    def ask(self, title: str, message: str, buttons) -> None:
        self.menu_state = None
        wrapped = [(label, kind, self._wrap_confirm(callback))
                   for label, kind, callback in buttons]
        self.confirm_state = {"title": title, "message": message, "buttons": wrapped}
        self.build()

    def _wrap_confirm(self, callback: Callable) -> Callable:
        def run() -> None:
            self.confirm_state = None
            try:
                callback()
            finally:
                self.build()
        return run

    def dismiss_confirm(self) -> None:
        if self.confirm_state:
            self.confirm_state = None
            self.build()

    def toast(self, text: str, icon: str = "check", color: str = GREEN,
              seconds: float = 3.4) -> None:
        self.toast_state = {"text": text, "icon": icon, "color": color,
                            "until": time.time() + seconds}
        self.build()

    # ------------------------------------------------------------------
    # source helpers
    def source_caption(self) -> str:
        mode = self.settings.source_mode
        if mode == "monitor":
            monitor = winapi.find_monitor(self.settings.monitor_name,
                                          self.settings.monitor_rect)
            if monitor:
                return f"Экран {monitor.index} · {monitor.width}×{monitor.height}"
            return "Экран"
        if mode == "region":
            rect = self.settings.region or {}
            if rect.get("width"):
                return f"Область {rect['width']}×{rect['height']}"
            return "Область не выбрана"
        info = winapi.find_window(self.settings.window_hwnd, self.settings.window_title)
        return f"Окно · {(info.title if info else self.settings.window_title)[:38]}"

    def refresh_preview(self) -> None:
        self._request_preview()

    # ------------------------------------------------------------------
    # region / screenshot / dialogs
    def choose_region(self) -> None:
        if self.state not in ("idle",):
            self.toast("Сначала остановите запись", icon="warning", color=YELLOW)
            return
        if self._region_selector is not None:
            return
        self.menu_state = None
        self.root.withdraw()
        self.root.after(240, self._open_region_selector)

    def _open_region_selector(self) -> None:
        screenshot = None
        error = ""
        try:
            import mss

            left, top, width, height = winapi.virtual_screen()
            with mss.MSS() as sct:
                raw = np.asarray(sct.grab({"left": left, "top": top,
                                           "width": width, "height": height}))
            screenshot = Image.fromarray(raw[:, :, [2, 1, 0]], "RGB")
            origin = (left, top)
        except Exception as exc:
            error = str(exc)
        if screenshot is None:
            self._restore_after_region()
            self.toast(f"Не удалось снять экран: {error}", icon="warning", color=RED)
            return
        self._region_selector = RegionSelector(
            self.root, self.t, screenshot, origin, self._region_done,
            self._restore_after_region,
        )

    def _region_done(self, rect: dict) -> None:
        self._region_selector = None
        self.settings.region = rect
        self.settings.source_mode = "region"
        self.save_now()
        self._restore_after_region()
        self.toast(f"Область {rect['width']}×{rect['height']} выбрана", icon="crop",
                   color=GREEN)
        self._request_preview()

    def _restore_after_region(self) -> None:
        self._region_selector = None
        try:
            self.root.deiconify()
            winapi.restore(self.hwnd)
        except Exception:
            pass
        self._apply_capture_exclusion()
        self.build()
        self._request_preview()

    def screenshot(self) -> None:
        if self._shot_busy:
            return
        self._shot_busy = True
        self.toast("Делаем снимок…", icon="camera", color=BLUE, seconds=2.5)

        def worker() -> None:
            path, error = capture_screenshot(self.settings)
            self.events.put(("screenshot", (path, error)))

        threading.Thread(target=worker, name="Screenshot", daemon=True).start()

    def pick_output_dir(self) -> None:
        initial = self.settings.video_dir
        try:
            self.root.attributes("-topmost", True)
            path = filedialog.askdirectory(parent=self.root, initialdir=initial,
                                           title="Папка для записей", mustexist=False)
        except Exception:
            path = ""
        finally:
            try:
                self.root.attributes("-topmost", False)
            except Exception:
                pass
        if path:
            self.settings.video_dir = path
            self.save_now()
            self.refresh_gallery()
            self.build()
            self.toast("Папка изменена", icon="folder", color=GREEN)

    def open_output_dir(self) -> None:
        try:
            folder = Path(self.settings.video_dir)
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except Exception as exc:
            self.toast(f"Не удалось открыть папку: {exc}", icon="warning", color=RED)

    def reveal_file(self, path: str) -> None:
        try:
            subprocess.Popen(["explorer", "/select,", str(path)])
        except Exception:
            self.open_output_dir()

    def play_recording(self, path: str) -> None:
        try:
            os.startfile(str(path))
        except Exception as exc:
            self.toast(f"Не удалось открыть файл: {exc}", icon="warning", color=RED)

    def delete_recording(self, path: str, name: str) -> None:
        def apply() -> None:
            try:
                file_path = Path(path)
                mtime = file_path.stat().st_mtime
                cache = self._thumb_path(file_path, mtime)
                file_path.unlink()
                cache.unlink(missing_ok=True)
            except Exception as exc:
                self.toast(f"Не удалось удалить: {exc}", icon="warning", color=RED)
                return
            self.refresh_gallery()
            self.build()
            self.toast("Запись удалена", icon="trash", color=YELLOW)

        self.ask("Удалить запись?", f"«{name}» будет удалена без возможности восстановления.",
                 [("Отмена", "ghost", self.dismiss_confirm),
                  ("Удалить", "danger", apply)])

    # ------------------------------------------------------------------
    # gallery
    def refresh_gallery(self) -> None:
        self._gallery_gen += 1
        generation = self._gallery_gen

        def worker() -> None:
            folder = Path(self.settings.video_dir)
            files: list[Path] = []
            try:
                for pattern in ("*.mp4", "*.mkv", "*.mov", "*.webm"):
                    files.extend(folder.glob(pattern))
            except Exception:
                files = []
            entries = []
            for file in files:
                try:
                    stat = file.stat()
                except Exception:
                    continue
                entries.append({
                    "path": str(file), "name": file.stem, "size": stat.st_size,
                    "mtime": stat.st_mtime, "duration": 0.0, "thumb": None,
                })
            entries.sort(key=lambda item: item["mtime"], reverse=True)
            entries = entries[:120]
            if generation != self._gallery_gen:
                return
            self.events.put(("gallery_list", entries))
            for entry in entries[:40]:
                if generation != self._gallery_gen:
                    return
                try:
                    info = ffmpeg_tools.probe(entry["path"])
                    entry["duration"] = info.duration
                    self.events.put(("gallery_update", dict(entry)))
                except Exception:
                    pass
            for entry in entries[:40]:
                if generation != self._gallery_gen:
                    return
                try:
                    image = self._load_thumb(Path(entry["path"]), entry["mtime"])
                    if image is not None:
                        self.events.put(("gallery_thumb", (entry["path"], image)))
                except Exception:
                    LOG.debug("Не удалось сделать миниатюру", exc_info=True)

        threading.Thread(target=worker, name="Gallery", daemon=True).start()

    def _thumb_path(self, path: Path, mtime: float) -> Path:
        key = hashlib.md5(f"{path}|{int(mtime)}".encode("utf-8")).hexdigest()
        return THUMB_DIR / f"{key}.jpg"

    def _load_thumb(self, path: Path, mtime: float) -> Optional[Image.Image]:
        cache = self._thumb_path(path, mtime)
        if not cache.exists() or cache.stat().st_size == 0:
            if not ffmpeg_tools.thumbnail(path, cache, 0.7, 420):
                return None
        try:
            image = Image.open(cache).convert("RGB")
        except Exception:
            return None
        target = (self.p(118), self.p(66))
        image = ImageOps.fit(image, target, Image.LANCZOS, centering=(0.5, 0.4))
        return round_image(image, self.p(9), border=(255, 255, 255, 30), border_width=1)

    # ------------------------------------------------------------------
    # events/timers
    def _tick(self) -> None:
        if self._closing:
            return
        try:
            self._process_events()
            now = time.perf_counter()
            if self.state == "countdown":
                left = self.countdown_end - now
                number = max(1, int(math.ceil(left)))
                if left <= 0:
                    self._begin_recording()
                elif number != self.countdown_shown:
                    self.countdown_shown = number
                    self.build()
            if self.recorder is not None:
                for kind in ("system", "mic"):
                    try:
                        value = self.recorder.levels[kind].get()
                    except Exception:
                        value = 0.0
                    self._levels[kind] = max(value, self._levels[kind] * 0.82)
            else:
                for kind in self._levels:
                    self._levels[kind] *= 0.8
            self.current_view.tick(self.ui)
            if self.state == "finalizing" and self._progress_card is not None:
                self.ui.update_progress(self._progress_card, self.finalize_progress)
            if self.toast_state and self.toast_state["until"] <= time.time():
                self.toast_state = None
                self.build()
            if self.gallery_dirty and self.view_name == "gallery":
                if time.time() - self._last_gallery_build > 0.25:
                    self.gallery_dirty = False
                    self._last_gallery_build = time.time()
                    self.build()
        except Exception:
            LOG.exception("Ошибка в цикле интерфейса")
        finally:
            if not self._closing:
                self.root.after(60, self._tick)

    def _process_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            kind = event[0]
            if kind == "preview":
                self.preview = event[1]
                if self.view_name == "record" and self.current_view is not None:
                    self.current_view.set_preview(self.ui, event[1])
            elif kind == "start_result":
                self._on_start_result(*event[1])
            elif kind == "finalized":
                self._on_finalized(*event[1])
            elif kind == "progress":
                self.finalize_progress = float(event[1])
                if self.recorder is not None and self.finalize_progress < 1.0:
                    self.progress_subtitle = "Собираем видео и звук в один файл…"
            elif kind == "rec":
                self._on_recorder_event(event[1], event[2])
            elif kind == "devices":
                system, mics, error = event[1]
                self.audio_devices = {"system": system, "mic": mics}
                self.audio_error = error
                if self.view_name in ("record", "settings"):
                    self.build()
            elif kind == "about":
                self.ffmpeg_version, self.hw_encoder = event[1]
                self._hw_ready.set()
                if self.view_name == "settings":
                    self.build()
            elif kind == "audio_test":
                self._on_audio_test(*event[1])
            elif kind == "screenshot":
                self._on_screenshot(*event[1])
            elif kind == "gallery_list":
                self.gallery = event[1]
                maximum = max(0, len(self.gallery) - GalleryView.VISIBLE)
                self.gallery_scroll = min(self.gallery_scroll, maximum)
                self.gallery_dirty = True
            elif kind == "gallery_update":
                entry = event[1]
                for existing in self.gallery:
                    if existing["path"] == entry["path"]:
                        existing["duration"] = entry.get("duration", existing["duration"])
                        break
                self.gallery_dirty = True
            elif kind == "gallery_thumb":
                path, image = event[1]
                for existing in self.gallery:
                    if existing["path"] == path:
                        existing["thumb"] = image
                        break
                self.gallery_dirty = True
            elif kind == "hotkey":
                self._on_hotkey(event[1])
            elif kind == "toast":
                self.toast(*event[1])

    def _on_recorder_event(self, kind: str, payload) -> None:
        if kind == "paused":
            if self.state == "recording":
                self.state = "paused"
                self.build()
        elif kind == "resumed":
            if self.state == "paused":
                self.state = "recording"
                self.build()
        elif kind == "notification":
            self.toast(str(payload), icon="info", color=BLUE)
        elif kind == "error":
            self.toast(str(payload), icon="warning", color=RED)
        elif kind == "finalizing":
            pass

    def _on_audio_test(self, kind: str, report: dict) -> None:
        name = "Микрофон" if kind == "mic" else "Системный звук"
        if not report.get("ok"):
            self.toast(f"{name}: {report.get('error', 'ошибка')}", icon="warning", color=RED)
            return
        peak = report.get("peak", 0.0)
        db = 20 * math.log10(max(peak, 1e-4))
        level = max(0.0, min(1.0, (db + 60) / 60))
        state = "нет сигнала" if peak < 0.002 else f"{db:.0f} дБ"
        self.toast(f"{name}: {report.get('rate', 0) / 1000:.0f} кГц · "
                   f"{report.get('channels', 1)} кан. · {state}",
                   icon="check" if peak >= 0.002 else "info",
                   color=GREEN if peak >= 0.002 else YELLOW)

    def _on_screenshot(self, path, error: str) -> None:
        self._shot_busy = False
        if error or path is None:
            self.toast(f"Снимок не удался: {error}", icon="warning", color=RED)
            return
        self.toast(f"Снимок сохранён · {Path(path).name}", icon="camera", color=GREEN)

    # ------------------------------------------------------------------
    # hotkeys
    def _install_hotkeys(self) -> None:
        self._remove_hotkeys()
        if not self.settings.hotkeys:
            return
        try:
            import keyboard
        except Exception:
            LOG.warning("Модуль keyboard недоступен")
            return
        try:
            self._hotkeys = [
                keyboard.add_hotkey("ctrl+alt+r",
                                    lambda: self.events.put(("hotkey", "toggle")),
                                    suppress=False),
                keyboard.add_hotkey("ctrl+alt+p",
                                    lambda: self.events.put(("hotkey", "pause")),
                                    suppress=False),
                keyboard.add_hotkey("ctrl+alt+s",
                                    lambda: self.events.put(("hotkey", "shot")),
                                    suppress=False),
            ]
            LOG.info("Горячие клавиши включены")
        except Exception:
            LOG.exception("Не удалось включить горячие клавиши")
            self._hotkeys = []

    def _remove_hotkeys(self) -> None:
        if not self._hotkeys:
            return
        try:
            import keyboard

            for handle in self._hotkeys:
                try:
                    keyboard.remove_hotkey(handle)
                except Exception:
                    pass
        except Exception:
            pass
        self._hotkeys = []

    def _on_hotkey(self, action: str) -> None:
        if action == "toggle":
            if self.state in ("recording", "paused"):
                self.stop()
            elif self.state == "countdown":
                self.cancel_countdown()
            elif self.state == "idle":
                self.start()
        elif action == "pause":
            if self.state == "recording":
                self.pause()
            elif self.state == "paused":
                self.resume()
        elif action == "shot":
            if self.state in ("idle", "recording", "paused"):
                self.screenshot()


def run() -> None:
    winapi.enable_dpi_awareness()
    ensure_dirs()
    logger.setup(LOG_DIR)
    if not winapi.single_instance():
        winapi.activate_existing()
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "SRPro.ScreenRecorder.4")
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
