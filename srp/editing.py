"""Timeline model, preview streaming and export for the built-in video editor.

The editor is deliberately small: a project is an ordered list of *clips*, and
every clip is a ``[start, end]`` range of one source file.  Cutting a clip
creates another range of the same file, deleting a clip removes it from the
sequence and adding a video appends its full range.  Rendering re-encodes the
sequence into one file with FFmpeg.
"""
from __future__ import annotations

import io
import os
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from . import ffmpeg_tools, logger
from .config import QUALITY_PRESETS

LOG = logger.get()
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

MIN_CLIP = 0.12          # seconds a fragment must keep to stay usable
PREVIEW_FPS = 24


# ----------------------------------------------------------------------
# Project model
# ----------------------------------------------------------------------
@dataclass
class Clip:
    path: str
    start: float = 0.0
    end: float = 0.0
    source_duration: float = 0.0
    width: int = 0
    height: int = 0
    has_audio: bool = False

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def name(self) -> str:
        return Path(self.path).stem

    def copy(self) -> "Clip":
        return replace(self)


def make_clip(path: str | Path) -> Optional[Clip]:
    path = str(path)
    try:
        if not Path(path).is_file():
            return None
        info = ffmpeg_tools.probe(path)
    except Exception:
        LOG.debug("Не удалось прочитать %s", path, exc_info=True)
        return None
    if not info.has_video or info.duration <= 0.05:
        return None
    return Clip(path=path, start=0.0, end=info.duration,
                source_duration=info.duration, width=info.width,
                height=info.height, has_audio=info.has_audio)


def total_duration(clips: list[Clip]) -> float:
    return sum(clip.duration for clip in clips)


def locate(clips: list[Clip], time_value: float) -> tuple[int, float]:
    """Map a position on the project timeline to (clip index, local offset)."""
    if not clips:
        return 0, 0.0
    remaining = max(0.0, min(time_value, total_duration(clips) - 1e-3))
    for index, clip in enumerate(clips):
        if remaining < clip.duration or index == len(clips) - 1:
            return index, max(0.0, min(remaining, clip.duration))
        remaining -= clip.duration
    return len(clips) - 1, 0.0


def clip_span(clips: list[Clip], index: int) -> tuple[float, float]:
    start = sum(clip.duration for clip in clips[:index])
    return start, start + clips[index].duration


def video_size(clips: list[Clip], limit: int = 3840) -> tuple[int, int]:
    for clip in clips:
        if clip.width >= 16 and clip.height >= 16:
            width, height = clip.width, clip.height
            long_side = max(width, height)
            if long_side > limit:
                scale = limit / long_side
                width, height = int(width * scale), int(height * scale)
            return _even(width), _even(height)
    return 1280, 720


def preview_size(clips: list[Clip], width: int = 640) -> tuple[int, int]:
    source_w, source_h = video_size(clips)
    height = int(round(width * source_h / max(1, source_w)))
    return _even(width), _even(max(180, min(height, width)))


def _even(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


# ----------------------------------------------------------------------
# FFmpeg filter helpers
# ----------------------------------------------------------------------
def _video_chain(index: int, clip: Clip, width: int, height: int, fps: int) -> str:
    return (
        f"[{index}:v]setpts=PTS-STARTPTS,fps={fps},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x000000,"
        f"setsar=1,format=yuv420p,trim=duration={clip.duration:.3f},"
        f"setpts=PTS-STARTPTS[v{index}]"
    )


def _audio_chain(index: int, clip: Clip) -> str:
    if clip.has_audio:
        return (
            f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=duration={clip.duration:.3f},asetpts=PTS-STARTPTS[a{index}]"
        )
    return (
        f"anullsrc=r=48000:cl=stereo,atrim=duration={clip.duration:.3f},"
        f"asetpts=PTS-STARTPTS[a{index}]"
    )


def build_filter(clips: list[Clip], width: int, height: int, fps: int,
                 with_audio: bool) -> str:
    parts: list[str] = []
    for index, clip in enumerate(clips):
        parts.append(_video_chain(index, clip, width, height, fps))
        if with_audio:
            parts.append(_audio_chain(index, clip))
    if len(clips) == 1:
        parts.append("[v0]null[vout]")
        if with_audio:
            parts.append("[a0]anull[aout]")
    else:
        labels = "".join(
            f"[v{i}][a{i}]" if with_audio else f"[v{i}]"
            for i in range(len(clips))
        )
        parts.append(
            f"{labels}concat=n={len(clips)}:v=1:a={1 if with_audio else 0}"
            f"[vout]{'[aout]' if with_audio else ''}"
        )
    return ";".join(parts)


def _input_arguments(clips: list[Clip]) -> list[str]:
    args: list[str] = []
    for clip in clips:
        args += ["-ss", f"{clip.start:.3f}", "-t", f"{clip.duration:.3f}",
                 "-i", clip.path]
    return args


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------
def render(clips: list[Clip], output: str | Path, quality: str = "high",
           with_audio: bool = True, fps: int = 30,
           progress: Optional[Callable[[float], None]] = None,
           cancel: Optional[threading.Event] = None) -> tuple[bool, str]:
    exe = ffmpeg_tools.find()
    if not exe:
        return False, "FFmpeg не найден"
    clips = [clip for clip in clips if clip.duration >= MIN_CLIP]
    if not clips:
        return False, "Нет фрагментов для сохранения"
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    width, height = video_size(clips)
    duration = total_duration(clips)
    filters = build_filter(clips, width, height, fps, with_audio)

    args = [exe, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1"]
    args += _input_arguments(clips)
    args += ["-filter_complex", filters, "-map", "[vout]"]
    if with_audio:
        args += ["-map", "[aout]"]
    args += ffmpeg_tools.encode_arguments(quality, "auto", width, height, fps)
    if with_audio:
        kbps = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["high"])["audio_kbps"]
        args += ["-c:a", "aac", "-b:a", f"{kbps}k", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-an"]
    if output.suffix.lower() in (".mp4", ".mov", ".m4v"):
        args += ["-movflags", "+faststart"]
    args += [str(output)]

    started = time.time()
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=CREATE_NO_WINDOW)
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)

    tail: list[str] = []

    def read_errors() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)

    errors = threading.Thread(target=read_errors, name="RenderLog", daemon=True)
    errors.start()

    cancelled = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            key, _sep, value = line.partition("=")
            if key in ("out_time_us", "out_time_ms") and progress and duration > 0:
                try:
                    seconds = int(value) / 1_000_000.0
                except ValueError:
                    seconds = -1.0
                if seconds >= 0:
                    try:
                        progress(min(seconds / duration, 1.0))
                    except Exception:
                        pass
            if cancel is not None and cancel.is_set():
                cancelled = True
                proc.kill()
                break
    finally:
        rc = proc.wait()
        errors.join(timeout=2.0)
    LOG.info("Монтаж %s: %.1f c за %.1f c", output.name, duration, time.time() - started)

    if cancelled:
        _safe_unlink(output)
        return False, "Отменено"
    if rc == 0 and output.exists() and output.stat().st_size > 1000:
        return True, ""
    error = "\n".join(tail[-12:]) or f"FFmpeg завершился с кодом {rc}"
    _safe_unlink(output)
    return False, error


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Frame extraction and preview stream
# ----------------------------------------------------------------------
def extract_frame(path: str, seconds: float, width: int = 720) -> Optional[Image.Image]:
    exe = ffmpeg_tools.find()
    if not exe:
        return None
    seconds = max(0.0, seconds)
    args = [exe, "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}",
            "-i", path, "-frames:v", "1", "-vf", f"scale={int(width)}:-2",
            "-f", "image2pipe", "-vcodec", "png", "-"]
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=25, creationflags=CREATE_NO_WINDOW)
    except Exception:
        LOG.debug("Не удалось извлечь кадр", exc_info=True)
        return None
    if not proc.stdout:
        return None
    try:
        image = Image.open(io.BytesIO(proc.stdout))
        image.load()
        return image.convert("RGB")
    except Exception:
        return None


def still_frame(clips: list[Clip], time_value: float, width: int = 720) -> Optional[Image.Image]:
    if not clips:
        return None
    index, local = locate(clips, time_value)
    clip = clips[index]
    source_time = min(clip.end - 0.04, clip.start + local)
    return extract_frame(clip.path, max(clip.start, source_time), width=width)


class PreviewStream:
    """Realtime preview of the current cut, decoded by FFmpeg into raw frames."""

    def __init__(self, clips: list[Clip], start_time: float, size: tuple[int, int],
                 fps: int = PREVIEW_FPS):
        self.clips = [clip.copy() for clip in clips]
        self.start_time = max(0.0, start_time)
        self.size = size
        self.fps = fps
        self.frames = 0
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _arguments(self) -> list[str]:
        exe = ffmpeg_tools.find() or "ffmpeg"
        index, local = locate(self.clips, self.start_time)
        selected = self.clips[index:]
        width, height = self.size
        args = [exe, "-hide_banner", "-loglevel", "error", "-nostdin"]
        for i, clip in enumerate(selected):
            offset = clip.start + (local if i == 0 else 0.0)
            length = max(MIN_CLIP, clip.end - offset)
            args += ["-re", "-ss", f"{offset:.3f}", "-t", f"{length:.3f}",
                     "-i", clip.path]
        # reuse the normal chains with a short copy of the selected clips
        parts = []
        for i, clip in enumerate(selected):
            offset = clip.start + (local if i == 0 else 0.0)
            length = max(MIN_CLIP, clip.end - offset)
            parts.append(
                f"[{i}:v]setpts=PTS-STARTPTS,scale={width}:{height}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x000000,"
                f"setsar=1,fps={self.fps},trim=duration={length:.3f},"
                f"setpts=PTS-STARTPTS[v{i}]"
            )
        if len(selected) == 1:
            parts.append("[v0]null[vout]")
        else:
            labels = "".join(f"[v{i}]" for i in range(len(selected)))
            parts.append(f"{labels}concat=n={len(selected)}:v=1:a=0[vout]")
        args += ["-filter_complex", ";".join(parts), "-map", "[vout]",
                 "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        return args

    def start(self, on_frame: Callable[[Image.Image, int], None],
              on_done: Callable[[], None]) -> None:
        try:
            self._proc = subprocess.Popen(
                self._arguments(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            LOG.exception("Не удалось запустить предпросмотр")
            on_done()
            return
        self._thread = threading.Thread(target=self._read, args=(on_frame, on_done),
                                        name="PreviewStream", daemon=True)
        self._thread.start()

    def _read(self, on_frame: Callable[[Image.Image, int], None],
              on_done: Callable[[], None]) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            on_done()
            return
        width, height = self.size
        frame_bytes = width * height * 3
        stream = proc.stdout
        try:
            while not self._stop.is_set():
                data = _read_exact(stream, frame_bytes)
                if data is None:
                    break
                image = Image.frombytes("RGB", (width, height), data)
                index = self.frames
                self.frames += 1
                try:
                    on_frame(image, index)
                except Exception:
                    LOG.debug("Ошибка кадра предпросмотра", exc_info=True)
                    break
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                stream.close()
            except Exception:
                pass
            on_done()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def _read_exact(stream, count: int) -> Optional[bytes]:
    chunks = bytearray()
    while len(chunks) < count:
        try:
            chunk = stream.read(count - len(chunks))
        except Exception:
            return None
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


# ----------------------------------------------------------------------
# Output naming
# ----------------------------------------------------------------------
def unique_output(path: str | Path) -> Path:
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({int(time.time())}){suffix}")
