"""WASAPI device discovery and synchronised audio capture.

Key points that make the result reliable:

* system sound is captured through the *loopback* endpoint of the selected
  output device (this is what makes Bluetooth headsets work),
* every source has its own native sample rate and channel count - WASAPI
  resamples for us and FFmpeg resamples later, so exotic Bluetooth formats
  (16 kHz mono hands-free) never break the pipeline,
* capture is timestamped with ``perf_counter`` and small device stalls are
  filled with silence, so the WAV stays aligned to the video timeline,
* while paused the stream keeps draining (no growing backlog on resume).
"""
from __future__ import annotations

import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import logger

try:
    import soundcard as sc
except Exception:  # pragma: no cover - soundcard should always import on Win
    sc = None

try:
    import pythoncom
except Exception:  # pragma: no cover
    pythoncom = None

LOG = logger.get()


@contextmanager
def _com_apartment():
    """Initialise COM on the *calling* thread.

    soundcard only initialises COM in the thread that imported it.  Device
    enumeration and the recording pipeline run in worker threads, so without
    this every worker thread fails with CO_E_NOTINITIALIZED (0x800401f0).
    """
    if pythoncom is None:
        yield
        return
    initialised = False
    try:
        pythoncom.CoInitialize()
        initialised = True
    except Exception:
        pass
    try:
        yield
    finally:
        if initialised:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

RATE_CANDIDATES = (48000, 44100, 32000, 16000, 8000)
BLUETOOTH_HINTS = ("bluetooth", "hands-free", "headset", "airpods", "hands free",
                   "беспровод", "наушник", "гарнитур", "bt ")


@dataclass
class AudioDevice:
    id: str
    name: str
    kind: str                 # "system" | "mic"
    channels: int = 2
    is_default: bool = False
    loopback: bool = False

    @property
    def short_name(self) -> str:
        name = self.name
        for prefix in ("Микрофон (", "Microphone (", "Динамики (", "Speakers (",
                       "Наушники (", "Headphones ("):
            if name.startswith(prefix) and name.endswith(")"):
                name = name[len(prefix):-1]
                break
        if len(name) > 42:
            name = name[:39] + "…"
        return name

    @property
    def bluetooth(self) -> bool:
        low = self.name.lower()
        return any(h in low for h in BLUETOOTH_HINTS)


@dataclass
class AudioLevel:
    peak: float = 0.0
    updated: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set(self, value: float) -> None:
        with self._lock:
            self.peak = value
            self.updated = time.time()

    def get(self) -> float:
        with self._lock:
            return self.peak

    def reset(self) -> None:
        self.set(0.0)


def list_devices() -> tuple[list[AudioDevice], list[AudioDevice], str]:
    """Return (system_audio_sources, microphones, error)."""
    if sc is None:
        return [], [], "Модуль soundcard недоступен"
    system: list[AudioDevice] = []
    mics: list[AudioDevice] = []
    with _com_apartment():
        try:
            speakers = {}
            try:
                speakers = {speaker.id: speaker for speaker in sc.all_speakers()}
            except Exception:
                speakers = {}
            default_speaker_id = ""
            try:
                default_speaker_id = sc.default_speaker().id
            except Exception:
                pass
            default_mic_id = ""
            try:
                default_mic_id = sc.default_microphone().id
            except Exception:
                pass

            loopbacks = {}
            all_mics = sc.all_microphones(include_loopback=True)
            for mic in all_mics:
                if getattr(mic, "isloopback", False):
                    loopbacks[mic.id] = mic
                else:
                    mics.append(AudioDevice(
                        id=mic.id, name=mic.name, kind="mic",
                        channels=int(getattr(mic, "channels", 1) or 1),
                        is_default=(mic.id == default_mic_id),
                    ))

            for speaker_id, speaker in speakers.items():
                loop = loopbacks.get(speaker_id)
                if loop is None:
                    continue
                system.append(AudioDevice(
                    id=loop.id, name=speaker.name, kind="system",
                    channels=int(getattr(loop, "channels", 2) or 2),
                    is_default=(speaker.id == default_speaker_id), loopback=True,
                ))

            system.sort(key=lambda d: (not d.is_default, d.name.lower()))
            mics.sort(key=lambda d: (not d.is_default, d.name.lower()))
            return system, mics, ""
        except Exception as exc:
            LOG.exception("Не удалось перечислить аудиоустройства")
            return system, mics, str(exc)


def find_device(device_id: str, kind: str) -> Optional[AudioDevice]:
    system, mics, _ = list_devices()
    pool = system if kind == "system" else mics
    if not pool:
        return None
    if device_id:
        for device in pool:
            if device.id == device_id:
                return device
    for device in pool:
        if device.is_default:
            return device
    return pool[0]


def _resolve_soundcard_device(device: AudioDevice):
    if sc is None:
        raise RuntimeError("soundcard недоступен")
    try:
        if device.kind == "system":
            return sc.get_microphone(id=device.id, include_loopback=True)
        return sc.get_microphone(id=device.id, include_loopback=False)
    except Exception:
        pass
    # the endpoint may have been re-created (Bluetooth profile switch) -> by name
    try:
        for candidate in sc.all_microphones(include_loopback=True):
            if candidate.name == device.name and bool(getattr(candidate, "isloopback", False)) == (device.kind == "system"):
                return candidate
    except Exception:
        pass
    raise RuntimeError(f"Устройство не найдено: {device.name}")


class _OpenedRecorder:
    def __init__(self, device: AudioDevice):
        self.device = device
        self.rate = 48000
        self.channels = 1
        self.block = 960
        self._recorder = None

    def open(self) -> None:
        last_error: Optional[Exception] = None
        soundcard_device = _resolve_soundcard_device(self.device)
        for rate in RATE_CANDIDATES:
            block = max(256, int(rate * 0.02))
            rec = None
            try:
                rec = soundcard_device.recorder(samplerate=rate, blocksize=block)
                rec.__enter__()
                probe = rec.record(numframes=min(block, 512))
                if probe is None or len(probe) == 0:
                    raise RuntimeError("устройство не отдаёт данные")
                self.rate = rate
                self.block = block
                self.channels = int(probe.shape[1]) if probe.ndim > 1 else 1
                self._recorder = rec
                LOG.info("Аудио открыто: %s @ %d Hz, %d ch", self.device.name, rate, self.channels)
                return
            except Exception as exc:
                last_error = exc
                LOG.warning("Не удалось открыть %s на %d Hz: %s", self.device.name, rate, exc)
                if rec is not None:
                    try:
                        rec.__exit__(None, None, None)
                    except Exception:
                        pass
        raise RuntimeError(f"Не удалось открыть устройство: {last_error}")

    def record(self, frames: int) -> np.ndarray:
        assert self._recorder is not None
        data = self._recorder.record(numframes=frames)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        return data

    def close(self) -> None:
        rec, self._recorder = self._recorder, None
        if rec is not None:
            try:
                rec.__exit__(None, None, None)
            except Exception:
                pass


def _to_int16(data: np.ndarray) -> bytes:
    return (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class AudioCapture(threading.Thread):
    """Captures one device into a WAV file on the shared session timeline."""

    def __init__(self, device: AudioDevice, path: Path, start_event: threading.Event,
                 stop_event: threading.Event, pause_event: threading.Event,
                 level: AudioLevel, timeline=None,
                 notify: Optional[Callable[[str], None]] = None):
        super().__init__(name=f"Audio-{device.kind}", daemon=True)
        self.device = device
        self.path = Path(path)
        self.start_event = start_event
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.level = level
        self.timeline = timeline
        self.notify = notify or (lambda *_: None)
        self.ready = threading.Event()
        self.failed = False
        self.error = ""
        self.rate = 0
        self.channels = 0
        self.first_ts: Optional[float] = None
        self.first_vt: float = 0.0
        self.frames = 0
        self.reconnects = 0

    # ------------------------------------------------------------------
    def run(self) -> None:
        if pythoncom is not None:
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
        opened = _OpenedRecorder(self.device)
        try:
            try:
                opened.open()
            except Exception as exc:
                self.failed = True
                self.error = str(exc)
                LOG.error("Аудиопоток %s не запущен: %s", self.device.name, exc)
                self.ready.set()
                return
            self.ready.set()

            # Drain until the session start so no stale frames are kept.
            while not self.start_event.is_set() and not self.stop_event.is_set():
                try:
                    opened.record(opened.block)
                except Exception:
                    break

            if self.stop_event.is_set():
                return

            self._capture(opened)
        except Exception:
            self.failed = True
            LOG.exception("Сбой аудиопотока %s", self.device.name)
        finally:
            opened.close()
            if pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def _capture(self, opened: _OpenedRecorder) -> None:
        wf: Optional[wave.Wave_write] = None
        # Wall-clock anchoring: the WASAPI stream is continuous, so the
        # *cumulative* sample count must track elapsed time.  Comparing every
        # single block (as the naive approach does) turns scheduler jitter
        # into audible gaps, so only real stalls > 50 ms are padded.
        anchor_ts: Optional[float] = None
        anchor_frames = 0
        silence_cache: dict[int, bytes] = {}
        tolerance = 0.05

        try:
            while not self.stop_event.is_set():
                try:
                    data = opened.record(opened.block)
                except Exception as exc:
                    LOG.warning("Ошибка чтения %s: %s", self.device.name, exc)
                    opened.close()
                    if not self._reconnect(opened):
                        self.failed = True
                        self.error = str(exc)
                        self.notify(f"Источник звука отключился: {self.device.short_name}")
                        break
                    anchor_ts = None
                    continue

                now = time.perf_counter()
                frames = len(data)
                block_start = now - frames / opened.rate

                # meters must also move while paused (device still drains)
                self.level.set(float(np.max(np.abs(data))) if frames else 0.0)

                if self.pause_event.is_set():
                    anchor_ts = None
                    continue

                if wf is None:
                    self.rate = opened.rate
                    self.channels = data.shape[1]
                    self.first_ts = block_start
                    self.first_vt = self.timeline.virtual(block_start) if self.timeline else 0.0
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    wf = wave.open(str(self.path), "wb")
                    wf.setnchannels(self.channels)
                    wf.setsampwidth(2)
                    wf.setframerate(self.rate)

                if wf.getnchannels() != data.shape[1]:
                    if data.shape[1] > wf.getnchannels():
                        data = data[:, :wf.getnchannels()]
                    else:
                        pad = np.zeros((frames, wf.getnchannels() - data.shape[1]), dtype=data.dtype)
                        data = np.concatenate([data, pad], axis=1)

                if anchor_ts is None:
                    # first block (or first after resume): no correction yet
                    anchor_ts = now
                    anchor_frames = self.frames + frames
                else:
                    expected = anchor_frames + (now - anchor_ts) * self.rate
                    error = expected - self.frames
                    if error > tolerance * self.rate:
                        silence_frames = int(min(error, 2.0 * self.rate))
                        payload = silence_cache.get(silence_frames)
                        if payload is None:
                            payload = bytes(silence_frames * self.channels * 2)
                            if len(silence_cache) > 24:
                                silence_cache.clear()
                            silence_cache[silence_frames] = payload
                        wf.writeframes(payload)
                        self.frames += silence_frames
                        LOG.debug("Аудио %s: вставлено %.0f мс тишины",
                                  self.device.kind, silence_frames / self.rate * 1000)
                    elif error < -tolerance * self.rate:
                        drop = min(int(-error), frames)
                        if drop > 0:
                            data = data[drop:]
                            frames = len(data)
                        LOG.debug("Аудио %s: отброшено %.0f мс",
                                  self.device.kind, drop / self.rate * 1000)

                if frames:
                    wf.writeframes(_to_int16(data))
                    self.frames += frames
        finally:
            if wf is not None:
                try:
                    wf.close()
                except Exception:
                    pass
            self.level.set(0.0)
            LOG.info("Аудио %s: %.1f c, %d Гц, %d канал(ов), файл %s",
                     self.device.short_name, self.frames / max(self.rate, 1),
                     self.rate, self.channels, self.path.name)

    def _reconnect(self, opened: _OpenedRecorder) -> bool:
        for attempt in range(4):
            if self.stop_event.is_set():
                return False
            time.sleep(0.35 + attempt * 0.35)
            try:
                opened.open()
                if opened.rate == self.rate or self.rate == 0:
                    self.reconnects += 1
                    LOG.info("Аудиоустройство восстановлено: %s", self.device.name)
                    return True
                LOG.error("Устройство вернулось с другим форматом (%d Гц) — запись источника остановлена",
                          opened.rate)
                return False
            except Exception as exc:
                LOG.debug("Повторное открытие %s: %s", self.device.name, exc)
        return False


def capture_report(device: AudioDevice, seconds: float = 2.5) -> dict:
    """Record a short sample and report level/format (used by the device test)."""
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
    opened = _OpenedRecorder(device)
    try:
        opened.open()
        chunks = []
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            chunks.append(opened.record(opened.block))
        data = np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 1))
        peak = float(np.max(np.abs(data))) if len(data) else 0.0
        rms = float(np.sqrt(np.mean(np.square(data)))) if len(data) else 0.0
        return {
            "ok": True, "rate": opened.rate, "channels": opened.channels,
            "peak": peak, "rms": rms, "seconds": len(data) / max(opened.rate, 1),
            "error": "",
        }
    except Exception as exc:
        return {"ok": False, "rate": 0, "channels": 0, "peak": 0.0, "rms": 0.0,
                "seconds": 0.0, "error": str(exc)}
    finally:
        opened.close()
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
