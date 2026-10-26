"""Logging with rotation, UTF-8 safety and crash capture."""
from __future__ import annotations

import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

_LOG: Optional[logging.Logger] = None
_CRASH_HOOK: Optional[Callable[[str], None]] = None


def setup(log_dir: Path, debug: bool = False) -> logging.Logger:
    global _LOG
    logger = logging.getLogger("srp")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    if logger.handlers:
        _LOG = logger
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", "%H:%M:%S")

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "srpro.log", maxBytes=1_500_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    except Exception:
        pass

    # A frozen windowed app has no stdout/stderr at all.
    if sys.stderr is not None and not getattr(sys, "frozen", False):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.setLevel(logging.INFO)
        logger.addHandler(stream)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    _install_excepthooks(logger)
    _LOG = logger
    return logger


def get() -> logging.Logger:
    global _LOG
    if _LOG is None:
        # Modules call get() at import time, before run() is able to set up
        # logging.  Resolve the real application log directory here instead of
        # depending on the current working directory (a shortcut/EXE can be
        # started from anywhere).
        try:
            from .config import LOG_DIR
        except Exception:  # pragma: no cover - defensive
            LOG_DIR = Path("logs")
        _LOG = setup(LOG_DIR)
    return _LOG


def set_crash_hook(hook: Callable[[str], None]) -> None:
    global _CRASH_HOOK
    _CRASH_HOOK = hook


def _install_excepthooks(logger: logging.Logger) -> None:
    def sys_hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.critical("Необработанное исключение:\n%s", text)
        if _CRASH_HOOK:
            try:
                _CRASH_HOOK(f"{exc_type.__name__}: {exc}")
            except Exception:
                pass

    def thread_hook(args):
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        logger.error("Ошибка в потоке %s:\n%s", getattr(args.thread, "name", "?"), text)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
