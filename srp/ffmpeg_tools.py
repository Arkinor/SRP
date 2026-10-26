"""FFmpeg discovery, probing, thumbnails and final muxing."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import APP_DIR, QUALITY_PRESETS, RESOURCE_DIR

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_FFMPEG: Optional[str] = None
_HW_ENCODER: Optional[str] = None
_HW_CHECKED = False
_HW_LOCK = threading.Lock()

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def find() -> Optional[str]:
    global _FFMPEG
    if _FFMPEG and Path(_FFMPEG).exists():
        return _FFMPEG
    candidates = [
        os.environ.get("SRP_FFMPEG", ""),
        str(APP_DIR / "ffmpeg" / "ffmpeg.exe"),
        str(APP_DIR / "ffmpeg.exe"),
        str(RESOURCE_DIR / "ffmpeg" / "ffmpeg.exe"),
        str(RESOURCE_DIR / "ffmpeg.exe"),
        str(RESOURCE_DIR / "tools" / "ffmpeg.exe"),
    ]
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            _FFMPEG = candidate
            return _FFMPEG
    # development fallback: binary shipped inside imageio-ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).is_file():
            _FFMPEG = path
            return _FFMPEG
    except Exception:
        pass
    return None


def exists() -> bool:
    return find() is not None


def run(args: Iterable[str], timeout: float = 20.0) -> tuple[int, str]:
    exe = find()
    if not exe:
        return -1, "FFmpeg не найден"
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # pragma: no cover - defensive
        return -1, str(exc)


def version() -> str:
    rc, out = run(["-hide_banner", "-version"], timeout=10)
    if rc == 0 and out:
        first = out.splitlines()[0].strip()
        return first.replace("ffmpeg version ", "").split(" Copyright")[0]
    return "не найден"


@dataclass
class MediaInfo:
    duration: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0


_INFO_CACHE: dict[tuple[str, float], MediaInfo] = {}


def probe(path: str | Path, use_cache: bool = True) -> MediaInfo:
    path = str(path)
    try:
        stamp = os.path.getmtime(path)
    except Exception:
        stamp = 0.0
    key = (path, stamp)
    if use_cache and key in _INFO_CACHE:
        return _INFO_CACHE[key]
    rc, out = run(["-hide_banner", "-i", path], timeout=15)
    info = MediaInfo()
    match = _DURATION_RE.search(out)
    if match:
        h, m, s = match.groups()
        info.duration = int(h) * 3600 + int(m) * 60 + float(s)
    info.has_video = "Video:" in out
    info.has_audio = "Audio:" in out
    vid = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", out)
    if vid:
        info.width, info.height = int(vid.group(1)), int(vid.group(2))
    if info.duration or info.has_video:
        _INFO_CACHE[key] = info
    return info


def thumbnail(video: str | Path, out_path: str | Path, seconds: float = 0.6, width: int = 480) -> bool:
    """Extract a single JPEG frame. Runs in a worker thread."""
    out_path = Path(out_path)
    if out_path.exists() and out_path.stat().st_size > 0:
        return True
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rc, _ = run([
        "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(seconds, 0):.2f}",
        "-i", str(video), "-frames:v", "1",
        "-vf", f"scale={width}:-2:flags=bilinear", "-q:v", "4", str(out_path),
    ], timeout=25)
    if rc != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        rc, _ = run([
            "-y", "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out_path),
        ], timeout=30)
    return rc == 0 and out_path.exists() and out_path.stat().st_size > 0


def available_encoders() -> set[str]:
    rc, out = run(["-hide_banner", "-encoders"], timeout=15)
    found = set()
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and re.match(r"^[VAS][\.FSXBD]{5}$", parts[0]):
                found.add(parts[1])
    return found


def _test_encoder(name: str) -> bool:
    rc, _ = run([
        "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.15",
        "-an", "-c:v", name, "-f", "null", "-",
    ], timeout=25)
    return rc == 0


def hw_encoder(force_refresh: bool = False) -> Optional[str]:
    """Best hardware H.264 encoder that actually initialises on this machine."""
    global _HW_ENCODER, _HW_CHECKED
    with _HW_LOCK:
        if _HW_CHECKED and not force_refresh:
            return _HW_ENCODER
        _HW_CHECKED = True
        _HW_ENCODER = None
        try:
            encoders = available_encoders()
            for candidate in ("h264_nvenc", "h264_qsv", "h264_amf", "h264_mf"):
                if candidate in encoders and _test_encoder(candidate):
                    _HW_ENCODER = candidate
                    break
        except Exception:
            _HW_ENCODER = None
        return _HW_ENCODER


def encode_arguments(quality: str, encoder: str, width: int, height: int, fps: int) -> list[str]:
    preset = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["high"])
    if encoder == "auto":
        hw = hw_encoder()
        if hw:
            if hw == "h264_nvenc":
                return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "hq",
                        "-rc", "vbr", "-cq", str(preset["cq"]), "-bf", "0",
                        "-pix_fmt", "yuv420p", "-g", str(fps * 2)]
            if hw == "h264_qsv":
                return ["-c:v", "h264_qsv", "-preset", "veryfast",
                        "-global_quality", str(preset["cq"]), "-pix_fmt", "nv12",
                        "-g", str(fps * 2)]
            if hw == "h264_amf":
                return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp",
                        "-qp_i", str(preset["cq"]), "-qp_p", str(preset["cq"]),
                        "-pix_fmt", "yuv420p", "-g", str(fps * 2)]
            if hw == "h264_mf":
                return ["-c:v", "h264_mf", "-pix_fmt", "yuv420p", "-g", str(fps * 2)]
    return ["-c:v", "libx264", "-preset", preset["preset"], "-crf", str(preset["crf"]),
            "-pix_fmt", "yuv420p", "-g", str(fps * 2), "-threads", "0"]


def _delay_filter(offset: float) -> str:
    if offset > 0.002:
        return f"adelay={int(round(offset * 1000))}:all=1,"
    if offset < -0.002:
        return f"atrim=start={abs(offset):.3f},asetpts=PTS-STARTPTS,"
    return ""


def mux(
    video_path: str | Path,
    tracks: list[dict],
    output_path: str | Path,
    duration: float,
    quality: str,
    container: str = "mp4",
    progress: Optional[Callable[[float], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> tuple[bool, str]:
    """Combine the captured video with already time-aligned WAV tracks.

    Each track: {"path": str, "offset": seconds, "gain": float}
    The video is stream-copied, so this step is fast.
    """
    exe = find()
    if not exe:
        return False, "FFmpeg не найден. Скачайте ffmpeg.exe и положите рядом с программой."
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["-y", "-hide_banner", "-loglevel", "info", "-i", str(video_path)]
    filters: list[str] = []
    labels: list[str] = []
    for i, track in enumerate(tracks, start=1):
        args += ["-i", str(track["path"])]
        gain = float(track.get("gain", 1.0) or 0.0)
        offset = float(track.get("offset", 0.0) or 0.0)
        chain = (
            f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={gain:.3f},{_delay_filter(offset)}"
            f"asetpts=N/SR/TB[a{i}]"
        )
        filters.append(chain)
        labels.append(f"[a{i}]")

    if labels:
        if len(labels) == 1:
            filters.append(f"{labels[0]}apad,alimiter=limit=0.95[aout]")
        else:
            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=0,"
                f"alimiter=limit=0.95,apad[aout]"
            )
        args += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", "[aout]"]
        audio_kbps = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["high"])["audio_kbps"]
        args += ["-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "48000", "-ac", "2"]
    else:
        args += ["-map", "0:v:0", "-an"]

    args += ["-c:v", "copy"]
    if duration > 0:
        args += ["-t", f"{duration:.3f}"]
    if container == "mp4":
        args += ["-movflags", "+faststart"]
    args += [str(output_path)]

    started = time.time()
    try:
        proc = subprocess.Popen([exe, *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=_CREATE_NO_WINDOW)
    except Exception as exc:
        return False, str(exc)

    tail: list[str] = []
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
            match = _TIME_RE.search(line)
            if match and progress and duration > 0:
                h, m, s = match.groups()
                seconds = int(h) * 3600 + int(m) * 60 + float(s)
                try:
                    progress(min(seconds / duration, 1.0))
                except Exception:
                    pass
            if cancel is not None and cancel.is_set():
                proc.kill()
                return False, "Отменено"
    finally:
        rc = proc.wait()

    if rc == 0 and output_path.exists() and output_path.stat().st_size > 1000:
        return True, ""
    error = "\n".join(tail[-12:]) or f"FFmpeg завершился с кодом {rc}"
    try:
        output_path.unlink(missing_ok=True)
    except Exception:
        pass
    return False, error


def make_wav_from_raw(raw: bytes, path: Path, rate: int, channels: int, width: int = 2) -> None:
    import wave
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(raw)
