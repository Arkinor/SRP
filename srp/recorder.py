"""Recording session orchestration: sources + audio + encoder + final mux."""
from __future__ import annotations

import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image

from . import audio, ffmpeg_tools, logger, video, winapi
from .config import Settings, SNAPSHOTS_DIR, TEMP_DIR

LOG = logger.get()

EventCB = Callable[[str, object], None]


@dataclass
class TrackResult:
    path: str
    offset: float
    gain: float
    seconds: float


@dataclass
class RecordingResult:
    path: Path
    duration: float
    size: int
    has_audio: bool
    source_label: str
    started_at: datetime
    warning: str = ""


def clamp_rect(rect: dict) -> dict:
    left, top, width, height = winapi.virtual_screen()
    right, bottom = left + width, top + height
    x0 = max(left, int(rect.get("left", 0)))
    y0 = max(top, int(rect.get("top", 0)))
    x1 = min(right, x0 + max(2, int(rect.get("width", 2))))
    y1 = min(bottom, y0 + max(2, int(rect.get("height", 2))))
    return {"left": x0, "top": y0, "width": max(2, x1 - x0), "height": max(2, y1 - y0)}


def build_source(settings: Settings) -> tuple[Optional[video.SourceSpec], str]:
    mode = settings.source_mode
    if mode == "region":
        rect = settings.region or {}
        if rect.get("width", 0) < 8 or rect.get("height", 0) < 8:
            return None, "Область не выбрана"
        rect = clamp_rect(rect)
        return video.SourceSpec("region", rect, subtitle="Выбранная область"), ""
    if mode == "window":
        info = winapi.find_window(settings.window_hwnd, settings.window_title)
        if not info:
            return None, "Окно не найдено — выберите его заново"
        rect = clamp_rect(info.rect)
        if rect["width"] < 8 or rect["height"] < 8:
            return None, "Окно свёрнуто"
        settings.window_hwnd = info.hwnd
        settings.window_title = info.title
        return video.SourceSpec("window", rect, hwnd=info.hwnd, title=info.title), ""
    monitor = winapi.find_monitor(settings.monitor_name, settings.monitor_rect)
    if not monitor:
        return None, "Монитор не найден"
    settings.monitor_name = monitor.device
    settings.monitor_rect = monitor.rect
    return video.SourceSpec("monitor", monitor.rect, subtitle=monitor.label), ""


class Recorder:
    """One recording session. Thread-safe for start/stop/pause calls from Tk."""

    def __init__(self, settings: Settings, on_event: Optional[EventCB] = None):
        self.settings = settings
        self.on_event = on_event or (lambda *_: None)
        self.state = "idle"
        self.error = ""
        self.source: Optional[video.SourceSpec] = None
        self.timeline = video.Timeline()
        self.levels = {"system": audio.AudioLevel(), "mic": audio.AudioLevel()}
        self.audio_captures: list[audio.AudioCapture] = []
        self.audio_devices: dict[str, audio.AudioDevice] = {}
        self.grabber: Optional[video.SourceGrabber] = None
        self.encoder: Optional[video.VideoEncoder] = None
        self.writer: Optional[video.VideoWriter] = None
        self.capture: Optional[video.VideoCapture] = None
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.session_dir: Optional[Path] = None
        self.started_at: Optional[datetime] = None
        self.result: Optional[RecordingResult] = None
        self._finalize_thread: Optional[threading.Thread] = None
        self._last_warning = ""
        self._frozen_elapsed = 0.0
        self.video_path: Optional[Path] = None
        self.encoder_log: Optional[Path] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def recording(self) -> bool:
        return self.state in ("recording", "paused", "stopping")

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        if self.state in ("finalizing", "done", "error"):
            return self._frozen_elapsed
        return self.timeline.virtual()

    def _emit(self, kind: str, payload: object = None) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:
            LOG.exception("Ошибка обработчика события записи")

    # ------------------------------------------------------------------
    def start(self) -> tuple[bool, str]:
        with self._lock:
            if self.recording:
                return False, "Запись уже идёт"
            self.state = "preparing"
        self.error = ""
        self.result = None
        self.audio_captures.clear()
        self.audio_devices.clear()
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.timeline = video.Timeline()
        self._frozen_elapsed = 0.0

        source, error = build_source(self.settings)
        if source is None:
            self.state = "idle"
            return False, error
        self.source = source
        self.started_at = datetime.now()
        stamp = self.started_at.strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.session_dir = TEMP_DIR / f"session_{stamp}"
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.state = "idle"
            return False, f"Нет доступа к временной папке: {exc}"

        if not ffmpeg_tools.find():
            self.state = "idle"
            return False, "FFmpeg не найден рядом с программой"

        # ---- audio devices: open first, this also settles BT profile switches
        wanted: list[tuple[str, str, Path]] = []
        if self.settings.record_system:
            device = audio.find_device(self.settings.system_device_id, "system")
            if device:
                self.audio_devices["system"] = device
                wanted.append(("system", device.id, self.session_dir / "system.wav"))
            else:
                self._warn("Системный звук недоступен — запись пойдёт без него")
        if self.settings.record_mic:
            device = audio.find_device(self.settings.mic_device_id, "mic")
            if device:
                self.audio_devices["mic"] = device
                wanted.append(("mic", device.id, self.session_dir / "microphone.wav"))
            else:
                self._warn("Микрофон не найден — запись пойдёт без него")

        for kind, _device_id, path in wanted:
            capture = audio.AudioCapture(
                device=self.audio_devices[kind], path=path,
                start_event=self.start_event, stop_event=self.stop_event,
                pause_event=self.pause_event, level=self.levels[kind],
                timeline=self.timeline,
                notify=lambda message: self._emit("notification", message),
            )
            self.audio_captures.append(capture)
            capture.start()

        deadline = time.time() + 4.0
        for capture in self.audio_captures:
            capture.ready.wait(timeout=max(0.2, deadline - time.time()))
            if capture.failed or not capture.ready.is_set():
                self._warn(f"Не удалось открыть: {capture.device.short_name}")
                self.audio_devices.pop(capture.device.kind, None)

        # ---- video pipeline
        try:
            self.grabber = video.SourceGrabber(source)
        except Exception as exc:
            self._abort_audio()
            self.state = "idle"
            return False, f"Не удалось захватить источник: {exc}"

        self.video_path = self.session_dir / "video.mkv"
        self.encoder_log = self.session_dir / "encoder.log"
        fps = max(1, int(self.settings.fps))
        self.encoder = video.VideoEncoder(
            self.video_path, self.grabber.out_w, self.grabber.out_h, fps,
            self.settings.quality, self.settings.encoder, self.encoder_log,
        )
        self.writer = video.VideoWriter(self.encoder, fps, self.timeline)
        self.capture = video.VideoCapture(
            self.grabber, self.writer, self.timeline, self.stop_event,
            self.pause_event, self.settings.show_cursor, fps, self.start_event,
        )

        self.encoder.start()
        self.writer.start()
        self.capture.start()

        # tiny settle window so the encoder can spawn before the clock starts
        time.sleep(0.08)
        if self.encoder.failed:
            self._abort_all()
            self.state = "idle"
            return False, f"Кодек не запустился: {self.encoder.error}"

        with self._lock:
            self.timeline.start()
            self.started_at = datetime.now()
            self.state = "recording"
        self.start_event.set()
        winapi.prevent_sleep(True)
        LOG.info("Запись началась: %s, звук: %s", source.label,
                 ", ".join(d.short_name for d in self.audio_devices.values()) or "нет")
        self._emit("started", {"source": source, "fps": fps})
        return True, ""

    def _warn(self, message: str) -> None:
        self._last_warning = message
        LOG.warning(message)
        self._emit("notification", message)

    def _abort_audio(self) -> None:
        self.stop_event.set()
        for capture in self.audio_captures:
            try:
                capture.join(timeout=1.0)
            except Exception:
                pass

    def _abort_all(self) -> None:
        self.stop_event.set()
        self.start_event.set()
        for capture in self.audio_captures:
            capture.stop_event.set()
        try:
            if self.writer is not None:
                self.writer.stop()
        except Exception:
            pass
        try:
            if self.encoder is not None:
                self.encoder.put(None)
        except Exception:
            pass
        for thread in (self.capture, self.writer, self.encoder):
            if thread is not None:
                try:
                    thread.join(timeout=2.0)
                except Exception:
                    pass
        if self.grabber is not None:
            self.grabber.close()

    # ------------------------------------------------------------------
    def pause(self) -> None:
        if self.state != "recording":
            return
        self.pause_event.set()
        self.timeline.pause()
        with self._lock:
            self.state = "paused"
        self._emit("paused", None)

    def resume(self) -> None:
        if self.state != "paused":
            return
        with self._lock:
            self.state = "recording"
        self.timeline.resume()
        self.pause_event.clear()
        self._emit("resumed", None)

    def stop(self) -> bool:
        if not self.recording or self.state == "stopping":
            return False
        with self._lock:
            self.state = "stopping"
            self._frozen_elapsed = self.timeline.virtual()
        self.pause_event.clear()
        self.stop_event.set()
        self.start_event.set()
        if self.timeline.paused:
            self.timeline.resume()
        winapi.prevent_sleep(False)
        LOG.info("Остановка записи через %.1f c", self._frozen_elapsed)
        return True

    def finalize_async(self, progress: Optional[Callable[[float], None]] = None,
                       done: Optional[Callable[[Optional[RecordingResult], str], None]] = None) -> None:
        def worker() -> None:
            result, error = self._finalize(progress)
            if done:
                try:
                    done(result, error)
                except Exception:
                    LOG.exception("Ошибка callback завершения")

        self._finalize_thread = threading.Thread(target=worker, name="Finalize", daemon=True)
        self._finalize_thread.start()

    # ------------------------------------------------------------------
    def _finalize(self, progress: Optional[Callable[[float], None]]) -> tuple[Optional[RecordingResult], str]:
        if self.state not in ("stopping", "finalizing"):
            return None, "Запись не была запущена"
        with self._lock:
            self.state = "finalizing"
        self._emit("finalizing", None)

        if self.capture is not None:
            self.capture.join(timeout=4.0)
        if self.writer is not None:
            self.writer.join(timeout=20.0)
        if self.encoder is not None:
            self.encoder.join(timeout=40.0)
        for capture in self.audio_captures:
            capture.join(timeout=6.0)
        if self.grabber is not None:
            self.grabber.close()

        fps = max(1, int(self.settings.fps))
        written = self.writer.written if self.writer else 0
        duration = written / fps
        if duration < 0.15 or written < 2:
            self.state = "error"
            LOG.error("Не удалось захватить ни одного кадра (записано %d кадров)", written)
            self._emit("error", "Не удалось захватить ни одного кадра")
            self._cleanup(keep=True)
            return None, "Не удалось захватить ни одного кадра"
        if self.encoder is not None and self.encoder.failed:
            self.state = "error"
            message = f"Ошибка кодирования: {self.encoder.error[:300]}"
            LOG.error(message)
            self._emit("error", message)
            self._cleanup(keep=True)
            return None, message

        tracks: list[dict] = []
        audio_seconds = 0.0
        for capture in self.audio_captures:
            if capture.failed and capture.frames <= 0:
                continue
            if not capture.path.exists() or capture.path.stat().st_size < 2048:
                continue
            if capture.frames < max(1000, capture.rate // 20):
                continue
            video_start = self.writer.first_vt if self.writer and self.writer.first_vt is not None else 0.0
            offset = capture.first_vt - video_start
            gain = self.settings.system_gain if capture.device.kind == "system" else self.settings.mic_gain
            tracks.append({"path": str(capture.path), "offset": offset, "gain": gain})
            audio_seconds = max(audio_seconds, capture.frames / max(capture.rate, 1))
            LOG.info("Аудиодорожка %s: %.2f c, смещение %+.3f c, усиление %.2f",
                     capture.device.kind, capture.frames / max(capture.rate, 1), offset, gain)

        output_dir = self.settings.output_dir
        stamp = (self.started_at or datetime.now()).strftime("%Y-%m-%d %H-%M-%S")
        output = self._unique_path(output_dir / f"Запись {stamp}.{self.settings.format}")

        def report(fraction: float) -> None:
            if progress:
                try:
                    progress(fraction)
                except Exception:
                    pass

        self._emit("muxing", None)
        ok, error = ffmpeg_tools.mux(
            self.video_path, tracks, output, duration, self.settings.quality,
            container=self.settings.format, progress=report,
        )
        warning = ""
        if not ok and tracks:
            LOG.error("Микширование не удалось: %s", error)
            warning = "Звук не удалось добавить — сохранено видео без звука"
            ok, error = ffmpeg_tools.mux(
                self.video_path, [], output, duration, self.settings.quality,
                container=self.settings.format, progress=report,
            )
            if ok:
                tracks = []
        if not ok:
            self.state = "error"
            self._emit("error", f"Ошибка сохранения: {error[:300]}")
            self._cleanup(keep=True)
            return None, f"Ошибка сохранения: {error[:300]}"

        result = RecordingResult(
            path=output, duration=duration, size=output.stat().st_size,
            has_audio=bool(tracks), source_label=self.source.label if self.source else "",
            started_at=self.started_at or datetime.now(), warning=warning,
        )
        self.result = result
        self.state = "done"
        self._cleanup(keep=False)
        LOG.info("Готово: %s (%.1f c, %.1f МБ)", output, duration, result.size / 1048576)
        self._emit("done", result)
        return result, warning

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(2, 1000):
            candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{path.stem}_{int(time.time())}{path.suffix}")

    def _cleanup(self, keep: bool = False) -> None:
        if not self.session_dir:
            return
        if keep:
            LOG.warning("Временные файлы сохранены: %s", self.session_dir)
            return
        try:
            shutil.rmtree(self.session_dir, ignore_errors=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def stats(self) -> dict:
        return {
            "capture_fps": self.capture.capture_fps if self.capture else 0.0,
            "dropped": self.capture.dropped if self.capture else 0,
            "frames": self.writer.written if self.writer else 0,
            "encoder": self.encoder.error if self.encoder and self.encoder.failed else "",
        }


def capture_screenshot(settings: Settings) -> tuple[Optional[Path], str]:
    source, error = build_source(settings)
    if source is None:
        return None, error
    grabber = None
    try:
        grabber = video.SourceGrabber(source)
        frame = grabber.grab(with_cursor=True)
        if frame is None:
            return None, "Не удалось захватить экран"
        image = Image.fromarray(frame[:, :, [2, 1, 0]], "RGB")
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        path = SNAPSHOTS_DIR / f"Снимок {stamp}.png"
        counter = 2
        while path.exists():
            path = SNAPSHOTS_DIR / f"Снимок {stamp} ({counter}).png"
            counter += 1
        image.save(path, "PNG", optimize=True)
        return path, ""
    except Exception as exc:
        LOG.exception("Ошибка скриншота")
        return None, str(exc)
    finally:
        if grabber is not None:
            grabber.close()
