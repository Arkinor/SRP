"""Application paths, settings and shared constants."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "SR Pro"
APP_TAGLINE = "Screen Recorder"
VERSION = "4.0"

FROZEN = bool(getattr(sys, "frozen", False))
_RES_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOURCE_DIR = Path(_RES_DIR)
ASSETS_DIR = RESOURCE_DIR / "assets"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _base_dir() -> Path:
    if FROZEN:
        candidate = Path(sys.executable).resolve().parent
    else:
        candidate = Path(__file__).resolve().parent.parent
    if _writable(candidate):
        return candidate
    fallback = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "SRPro"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


APP_DIR = _base_dir()
CONFIG_FILE = APP_DIR / "settings.json"
LOG_DIR = APP_DIR / "logs"
CACHE_DIR = APP_DIR / ".cache"
THUMB_DIR = CACHE_DIR / "thumbs"
DEFAULT_VIDEO_DIR = APP_DIR / "Записи"
SNAPSHOTS_DIR = APP_DIR / "Скриншоты"
TEMP_DIR = Path(tempfile.gettempdir()) / "SRPro"


def ensure_dirs() -> None:
    for folder in (LOG_DIR, CACHE_DIR, THUMB_DIR, DEFAULT_VIDEO_DIR, SNAPSHOTS_DIR, TEMP_DIR):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


FPS_CHOICES = (24, 30, 60)
COUNTDOWN_CHOICES = (0, 3, 5)
QUALITY_CHOICES = ("high", "balanced", "small")
QUALITY_LABELS = {"high": "Высокое", "balanced": "Баланс", "small": "Компактно"}
ENCODER_CHOICES = ("auto", "software")
ENCODER_LABELS = {"auto": "Авто", "software": "Только CPU"}
SOURCE_LABELS = {"monitor": "Экран", "region": "Область", "window": "Окно"}

# encoder tuning per quality preset
QUALITY_PRESETS: dict[str, dict[str, Any]] = {
    "high": {"crf": 18, "cq": 20, "audio_kbps": 192, "preset": "ultrafast"},
    "balanced": {"crf": 22, "cq": 23, "audio_kbps": 160, "preset": "ultrafast"},
    "small": {"crf": 27, "cq": 27, "audio_kbps": 128, "preset": "ultrafast"},
}


@dataclass
class Settings:
    video_dir: str = str(DEFAULT_VIDEO_DIR)
    source_mode: str = "monitor"           # monitor | region | window
    monitor_name: str = ""                 # device name of the monitor
    monitor_rect: dict = field(default_factory=dict)
    region: dict = field(default_factory=dict)
    window_title: str = ""
    window_hwnd: int = 0
    fps: int = 30
    quality: str = "high"
    encoder: str = "auto"
    record_system: bool = True
    record_mic: bool = True
    system_device_id: str = ""
    mic_device_id: str = ""
    system_gain: float = 1.0
    mic_gain: float = 1.0
    show_cursor: bool = True
    countdown: int = 3
    hide_on_record: bool = False
    hotkeys: bool = True
    open_folder_after: bool = False
    format: str = "mp4"
    schema: int = 1

    # ---------- persistence ----------
    @classmethod
    def load(cls, path: Path = CONFIG_FILE) -> "Settings":
        data: dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        obj = cls()
        for key, value in clean.items():
            try:
                setattr(obj, key, value)
            except Exception:
                pass
        obj.sanitize()
        return obj

    def sanitize(self) -> None:
        if self.fps not in FPS_CHOICES:
            self.fps = 30
        if self.quality not in QUALITY_CHOICES:
            self.quality = "high"
        if self.encoder not in ENCODER_CHOICES:
            self.encoder = "auto"
        if self.source_mode not in SOURCE_LABELS:
            self.source_mode = "monitor"
        if self.countdown not in COUNTDOWN_CHOICES:
            self.countdown = 3
        if self.format not in ("mp4", "mkv"):
            self.format = "mp4"
        try:
            self.system_gain = float(min(max(self.system_gain, 0.0), 3.0))
            self.mic_gain = float(min(max(self.mic_gain, 0.0), 3.0))
        except Exception:
            self.system_gain = self.mic_gain = 1.0
        if not self.video_dir:
            self.video_dir = str(DEFAULT_VIDEO_DIR)

    def save(self, path: Path = CONFIG_FILE) -> bool:
        self.sanitize()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except Exception:
            return False

    @property
    def output_dir(self) -> Path:
        try:
            p = Path(self.video_dir)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:
            DEFAULT_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
            return DEFAULT_VIDEO_DIR
