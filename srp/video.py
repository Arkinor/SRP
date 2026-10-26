"""Screen capture source, real cursor overlay and a timestamp-driven encoder.

The video timeline is derived from ``perf_counter`` timestamps instead of
being assumed to run at a constant rate.  Frames are placed on a fixed-FPS
grid and gaps are filled with the previous frame, which keeps the finished
file exactly as long as the real recording - the root cause of the old
audio/video desync is therefore gone.
"""
from __future__ import annotations

import ctypes
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mss
import numpy as np
from PIL import Image

from . import ffmpeg_tools, logger, winapi

LOG = logger.get()
_CREATE_NO_WINDOW = 0x08000000


@dataclass
class SourceSpec:
    mode: str
    rect: dict
    hwnd: int = 0
    title: str = ""
    subtitle: str = ""

    @property
    def width(self) -> int:
        return int(self.rect.get("width", 0))

    @property
    def height(self) -> int:
        return int(self.rect.get("height", 0))

    @property
    def label(self) -> str:
        if self.mode == "window":
            return f"Окно · {self.title[:40]}"
        if self.mode == "region":
            return f"Область · {self.width}×{self.height}"
        return f"Экран · {self.width}×{self.height}"


def even_size(width: int, height: int) -> tuple[int, int]:
    return max(2, int(width) - int(width) % 2), max(2, int(height) - int(height) % 2)


class Timeline:
    """Monotonic session clock where pause time does not exist."""

    def __init__(self) -> None:
        self.t0 = 0.0
        self._pause_started: Optional[float] = None
        self._paused_total = 0.0
        self._lock = threading.Lock()

    def start(self) -> float:
        with self._lock:
            self.t0 = time.perf_counter()
            self._pause_started = None
            self._paused_total = 0.0
            return self.t0

    def pause(self) -> None:
        with self._lock:
            if self._pause_started is None:
                self._pause_started = time.perf_counter()

    def resume(self) -> None:
        with self._lock:
            if self._pause_started is not None:
                self._paused_total += time.perf_counter() - self._pause_started
                self._pause_started = None

    def virtual(self, raw: Optional[float] = None) -> float:
        raw = time.perf_counter() if raw is None else raw
        with self._lock:
            frozen = self._pause_started is not None
            cut = self._paused_total
            pause_started = self._pause_started
        if frozen and pause_started is not None:
            raw = pause_started
        return max(0.0, raw - self.t0 - cut)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._pause_started is not None


# ----------------------------------------------------------------------
# Real mouse cursor (mss never captures it)
# ----------------------------------------------------------------------
class CursorOverlay:
    def __init__(self) -> None:
        self._cache: dict[int, tuple[np.ndarray, int, int]] = {}
        self._last_handle = 0
        self._failed = False

    def _extract(self, hcur: int) -> Optional[tuple[np.ndarray, int, int]]:
        try:
            import win32gui

            _icon, hx, hy, hbm_mask, hbm_color = win32gui.GetIconInfo(hcur)
            if not hbm_mask and not hbm_color:
                return None
            color = self._read_bitmap(hbm_color) if hbm_color else None
            mask = self._read_bitmap(hbm_mask) if hbm_mask else None
            for handle in (hbm_mask, hbm_color):
                if not handle:
                    continue
                try:
                    raw_handle = handle.Detach()
                except Exception:
                    raw_handle = int(handle)
                ctypes.windll.gdi32.DeleteObject(raw_handle)
            if color is None:
                return None
            height, width = color.shape[:2]
            alpha = color[:, :, 3].astype(np.float32) / 255.0
            if alpha.max() <= 0.01 and mask is not None:
                alpha = np.where(mask[:, :, 0] > 0, 0.0, 1.0).astype(np.float32)
            rgb = color[:, :, [2, 1, 0]].astype(np.float32)
            return rgb, alpha, hx, hy
        except Exception:
            LOG.debug("Не удалось получить изображение курсора", exc_info=True)
            return None

    @staticmethod
    def _read_bitmap(handle) -> Optional[np.ndarray]:
        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                        ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                        ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                        ("bmBits", ctypes.c_void_p)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                        ("biClrImportant", ctypes.c_uint32)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

        gdi32 = ctypes.windll.gdi32
        user32 = ctypes.windll.user32
        bm = BITMAP()
        if not gdi32.GetObjectW(int(handle), ctypes.sizeof(BITMAP), ctypes.byref(bm)):
            return None
        width, height = int(bm.bmWidth), int(bm.bmHeight)
        if width <= 0 or height <= 0 or width > 512 or height > 512:
            return None
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        buf = (ctypes.c_uint8 * (width * height * 4))()
        hdc = user32.GetDC(0)
        try:
            lines = gdi32.GetDIBits(hdc, int(handle), 0, height, buf,
                                    ctypes.byref(bmi), 0)
        finally:
            user32.ReleaseDC(0, hdc)
        if not lines:
            return None
        return np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 4).copy()

    def _sprite(self, hcur: int):
        if hcur in self._cache:
            return self._cache[hcur]
        extracted = self._extract(hcur)
        if extracted is None:
            return None
        if len(self._cache) > 24:
            self._cache.clear()
        self._cache[hcur] = extracted
        return extracted

    def draw(self, frame: np.ndarray, rect: dict) -> None:
        if self._failed:
            return
        try:
            import win32gui

            flags, hcur, (cx, cy) = win32gui.GetCursorInfo()
            if not (flags & 1) or not hcur:
                return
            sprite = self._sprite(int(hcur))
            if sprite is None:
                self._failed = True
                return
            rgb, alpha, hx, hy = sprite
            x = cx - hx - rect["left"]
            y = cy - hy - rect["top"]
            sh, sw = alpha.shape
            if x + sw <= 0 or y + sh <= 0 or x >= frame.shape[1] or y >= frame.shape[0]:
                return
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(frame.shape[1], x + sw), min(frame.shape[0], y + sh)
            sx0, sy0 = x0 - x, y0 - y
            sub_a = alpha[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0), None]
            sub_rgb = rgb[sy0:sy0 + (y1 - y0), sx0:sx0 + (x1 - x0)]
            region = frame[y0:y1, x0:x1, :3].astype(np.float32)
            frame[y0:y1, x0:x1, :3] = (region * (1.0 - sub_a) + sub_rgb * sub_a).astype(np.uint8)
        except Exception:
            self._failed = True
            LOG.debug("Курсор отключён", exc_info=True)


# ----------------------------------------------------------------------
# Capture source
# ----------------------------------------------------------------------
class SourceGrabber:
    def __init__(self, spec: SourceSpec):
        self.spec = spec
        self.out_w, self.out_h = even_size(spec.width, spec.height)
        self.rect = dict(spec.rect)
        self.rect["width"] = self.out_w
        self.rect["height"] = self.out_h
        self._local = threading.local()
        self._cursor = CursorOverlay()
        self._last_frame: Optional[np.ndarray] = None
        self._last_rect = dict(self.rect)
        self._window_lost = False

    def _sct(self):
        """mss must live in the thread that captures; keep one per thread."""
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = mss.mss()
            self._local.sct = sct
        return sct

    def close(self) -> None:
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:
                pass
            self._local.sct = None

    def _current_rect(self) -> Optional[dict]:
        if self.spec.mode != "window" or not self.spec.hwnd:
            return dict(self.rect)
        frame = winapi.window_frame(self.spec.hwnd)
        if not frame:
            self._window_lost = True
            return None
        self._window_lost = False
        self._last_rect = frame
        return frame

    def grab(self, with_cursor: bool = False) -> Optional[np.ndarray]:
        rect = self._current_rect()
        if rect is None:
            return None
        try:
            raw = np.asarray(self._sct().grab(rect))
        except Exception:
            LOG.debug("Ошибка захвата %s", rect, exc_info=True)
            time.sleep(0.05)
            return None

        frame = raw
        if raw.shape[1] != self.out_w or raw.shape[0] != self.out_h:
            image = Image.frombytes("RGB", (raw.shape[1], raw.shape[0]), raw[:, :, [2, 1, 0]].tobytes())
            image = image.resize((self.out_w, self.out_h), Image.BILINEAR)
            arr = np.asarray(image)
            frame = np.dstack([arr[:, :, ::-1], np.full((self.out_h, self.out_w, 1), 255, np.uint8)])
        elif raw.shape[1] > self.out_w or raw.shape[0] > self.out_h:
            frame = raw[:self.out_h, :self.out_w]
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)

        if with_cursor:
            self._cursor.draw(frame, {"left": rect["left"], "top": rect["top"]})
        self._last_frame = frame
        self._last_rect = rect
        return frame

    @property
    def window_lost(self) -> bool:
        return self._window_lost


def preview_png(spec: SourceSpec, width: int = 640) -> Optional[Image.Image]:
    """Downscaled snapshot for the live preview (cheap, UI thread)."""
    grabber = None
    try:
        if spec.mode == "window" and spec.hwnd:
            current = winapi.window_frame(spec.hwnd)
            if not current:
                return None
            spec = SourceSpec("window", current, spec.hwnd, spec.title)
        grabber = SourceGrabber(spec)
        frame = grabber.grab(with_cursor=True)
        if frame is None:
            return None
        rgb = frame[:, :, [2, 1, 0]]
        image = Image.fromarray(rgb, "RGB")
        if image.width > width:
            height = int(width * image.height / image.width)
            image = image.resize((width, max(2, height)), Image.LANCZOS)
        return image
    except Exception:
        LOG.debug("Ошибка предпросмотра", exc_info=True)
        return None
    finally:
        if grabber is not None:
            grabber.close()


# ----------------------------------------------------------------------
# Encoder
# ----------------------------------------------------------------------
class VideoEncoder(threading.Thread):
    def __init__(self, path: Path, width: int, height: int, fps: int,
                 quality: str, encoder_setting: str, log_path: Path):
        super().__init__(name="VideoEncoder", daemon=True)
        self.path = Path(path)
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        self.encoder_setting = encoder_setting
        self.log_path = Path(log_path)
        self.queue: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=12)
        self.frames_written = 0
        self.failed = False
        self.error = ""
        self.finished = threading.Event()
        self._proc = None
        self._stderr: deque[str] = deque(maxlen=50)
        self._stop_reader = threading.Event()

    def put(self, frame: Optional[np.ndarray]) -> bool:
        """Blocking enqueue so the writer can count frames truthfully."""
        if frame is None:
            try:
                self.queue.put_nowait(None)
            except queue.Full:
                pass
            return True
        while not self.finished.is_set():
            try:
                self.queue.put(frame, timeout=0.5)
                return True
            except queue.Full:
                if self.failed:
                    return False
        return False

    def run(self) -> None:
        exe = ffmpeg_tools.find()
        if not exe:
            self.failed = True
            self.error = "FFmpeg не найден"
            self.finished.set()
            return
        args = [
            exe, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgra",
            "-s", f"{self.width}x{self.height}", "-r", str(self.fps), "-i", "-",
            "-an", *ffmpeg_tools.encode_arguments(
                self.quality, self.encoder_setting, self.width, self.height, self.fps),
            str(self.path),
        ]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._proc = subprocess.Popen(
                args, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as exc:
            self.failed = True
            self.error = str(exc)
            self.finished.set()
            return
        threading.Thread(target=self._read_stderr, daemon=True).start()

        stdin = self._proc.stdin
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                if self.failed:
                    continue
                try:
                    stdin.write(memoryview(item).cast("B"))
                except (BrokenPipeError, OSError) as exc:
                    self.failed = True
                    self.error = str(exc) or "FFmpeg остановился"
                    LOG.error("Видеокодер остановился: %s", self.error)
                    break
                self.frames_written += 1
        except Exception:
            self.failed = True
            LOG.exception("Ошибка потока кодирования видео")
        finally:
            try:
                stdin.close()
            except Exception:
                pass
            try:
                rc = self._proc.wait(timeout=25)
            except Exception:
                rc = -1
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._stop_reader.set()
            if rc not in (0,) and not self.failed:
                self.failed = True
                self.error = "\n".join(list(self._stderr)[-6:]) or f"FFmpeg код {rc}"
            if self.failed:
                LOG.error("Кодирование не удалось: %s", self.error)
            self.finished.set()

    def _read_stderr(self) -> None:
        try:
            assert self._proc is not None and self._proc.stderr is not None
            for raw in self._proc.stderr:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr.append(line)
        except Exception:
            pass


class VideoWriter(threading.Thread):
    def __init__(self, encoder: VideoEncoder, fps: int, timeline: Timeline):
        super().__init__(name="VideoWriter", daemon=True)
        self.encoder = encoder
        self.fps = fps
        self.timeline = timeline
        self.last_frame: Optional[np.ndarray] = None
        self.last_index = -1
        self.written = 0
        self.first_vt: Optional[float] = None
        self.finished = threading.Event()
        self.in_queue: "queue.Queue[Optional[tuple[np.ndarray, float]]]" = queue.Queue(maxsize=10)
        self.skipped = 0
        self.max_gap_frames = fps * 20

    def put(self, frame: np.ndarray, vt: float) -> None:
        try:
            self.in_queue.put_nowait((frame, vt))
        except queue.Full:
            self.skipped += 1

    def stop(self) -> None:
        try:
            self.in_queue.put_nowait(None)
        except queue.Full:
            try:
                self.in_queue.put(None, timeout=0.5)
            except queue.Full:
                pass

    def run(self) -> None:
        try:
            while True:
                try:
                    item = self.in_queue.get(timeout=0.25)
                except queue.Empty:
                    continue
                if item is None:
                    break
                frame, vt = item
                self._place(frame, vt)
        finally:
            self._finish()
            self.finished.set()

    def _place(self, frame: np.ndarray, vt: float) -> None:
        index = int(round(vt * self.fps))
        if index <= self.last_index:
            return
        if self.last_frame is None:
            self.encoder.put(frame)
            self.last_frame = frame
            self.last_index = index
            self.written = 1
            self.first_vt = vt
            return
        gap = index - self.last_index - 1
        if gap > self.max_gap_frames:
            LOG.warning("Пропуск слишком большого разрыва видео: %.1f c", gap / self.fps)
            gap = self.max_gap_frames
            index = self.last_index + 1 + gap
        for _ in range(gap):
            self.encoder.put(self.last_frame)
            self.written += 1
        self.encoder.put(frame)
        self.last_frame = frame
        self.last_index = index
        self.written += 1

    def _finish(self) -> None:
        duration = self.timeline.virtual()
        target = int(round(duration * self.fps))
        if self.last_frame is None:
            self.encoder.put(None)
            return
        target = max(target, self.last_index + 1)
        extra = target - (self.last_index + 1)
        if extra > self.max_gap_frames:
            extra = self.max_gap_frames
            target = self.last_index + 1 + extra
        for _ in range(extra):
            self.encoder.put(self.last_frame)
            self.written += 1
        self.last_index = target - 1
        self.encoder.put(None)

class VideoCapture(threading.Thread):
    def __init__(self, grabber: SourceGrabber, writer: VideoWriter, timeline: Timeline,
                 stop_event: threading.Event, pause_event: threading.Event,
                 show_cursor: bool, fps: int, start_event: threading.Event):
        super().__init__(name="VideoCapture", daemon=True)
        self.grabber = grabber
        self.writer = writer
        self.timeline = timeline
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.start_event = start_event
        self.show_cursor = show_cursor
        self.fps = fps
        self.capture_fps = 0.0
        self.dropped = 0
        self._times: deque[float] = deque(maxlen=90)

    def run(self) -> None:
        self.start_event.wait()
        interval = 1.0 / self.fps
        next_at = time.perf_counter()
        try:
            while not self.stop_event.is_set():
                now = time.perf_counter()
                if self.pause_event.is_set():
                    time.sleep(0.05)
                    next_at = time.perf_counter()
                    continue
                if now < next_at:
                    time.sleep(min(0.004, next_at - now))
                    continue
                vt = self.timeline.virtual(now)
                frame = self.grabber.grab(with_cursor=self.show_cursor)
                if frame is not None:
                    self.writer.put(frame, vt)
                    self._times.append(now)
                next_at += interval
                now2 = time.perf_counter()
                if next_at < now2 - interval:
                    next_at = now2
                if len(self._times) > 2:
                    span = self._times[-1] - self._times[0]
                    if span > 0:
                        self.capture_fps = (len(self._times) - 1) / span
        finally:
            self.writer.stop()
            try:
                self.grabber.close()
            except Exception:
                pass
