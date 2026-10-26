"""System tray icon with a native context menu.

The tray runs in its own thread because pystray blocks.  Every menu action is
posted to the Tk application through the regular event queue, so no Tk call is
ever made from the tray thread.
"""
from __future__ import annotations

from typing import Optional

from PIL import Image, ImageDraw

from .. import logger

LOG = logger.get()

try:  # pystray is optional: without it the app simply keeps its window.
    import pystray
    from pystray import Menu, MenuItem
except Exception:  # pragma: no cover - defensive
    pystray = None  # type: ignore[assignment]
    Menu = MenuItem = None  # type: ignore[assignment]


def _badge(image: Image.Image) -> Image.Image:
    """Small red dot in the corner marking an active recording."""
    img = image.copy().convert("RGBA")
    draw = ImageDraw.Draw(img)
    size = max(10, int(img.width * 0.34))
    r = size / 2
    cx = img.width - r - size * 0.10
    cy = img.height - r - size * 0.10
    draw.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2),
                 fill=(8, 9, 14, 255))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 64, 88, 255))
    return img


class TrayIcon:
    def __init__(self, app):
        self.app = app
        self._icon = None
        self._thread = None
        self._idle_image: Optional[Image.Image] = None
        self._record_image: Optional[Image.Image] = None
        self._recording = False
        self._title = ""
        self._hint_shown = False
        if pystray is None:
            LOG.warning("pystray не установлен — значок в трее недоступен")
            return
        base = getattr(app, "_logo64", None)
        if base is None:
            from .theme import app_icon_image
            base = app_icon_image(64)
        self._idle_image = base.convert("RGBA")
        self._record_image = _badge(base)
        try:
            self._icon = pystray.Icon("srpro", self._idle_image, "SR Pro", self._menu())
        except Exception:
            LOG.exception("Не удалось создать значок в трее")
            self._icon = None

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._icon is not None

    def _menu(self):
        if Menu is None:
            return None

        def text(value):
            return lambda _item: value

        return Menu(
            MenuItem(lambda _item: "Открыть SR Pro", lambda: self._post("show"),
                     default=True),
            Menu.SEPARATOR,
            MenuItem(self._record_label, lambda: self._post("toggle")),
            MenuItem(self._pause_label, lambda: self._post("pause"),
                     enabled=lambda _item: self.app.state in ("recording", "paused")),
            MenuItem(text("Снимок экрана"), lambda: self._post("shot")),
            Menu.SEPARATOR,
            MenuItem(text("Настройки"), lambda: self._post("settings")),
            MenuItem(text("Выход"), lambda: self._post("quit")),
        )

    def _record_label(self, _item=None) -> str:
        return ("Остановить запись" if self.app.state in ("recording", "paused")
                else "Начать запись")

    def _pause_label(self, _item=None) -> str:
        return "Продолжить" if self.app.state == "paused" else "Пауза"

    def _post(self, action: str) -> None:
        try:
            self.app.events.put(("tray", action))
        except Exception:
            LOG.exception("Не удалось передать действие из трея")

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._icon is None or self._thread is not None:
            return
        self._thread = __import__("threading").Thread(
            target=self._run, name="Tray", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            LOG.exception("Значок в трее остановлен с ошибкой")

    def stop(self) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.visible = False
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass
        self._thread = None

    def notify(self, message: str, title: str = "SR Pro", once: bool = False) -> None:
        if self._icon is None:
            return
        if once:
            if self._hint_shown:
                return
            self._hint_shown = True
        try:
            self._icon.notify(message, title)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def update(self, recording: bool, title: str = "") -> None:
        """Refresh the icon and tooltip; safe to call from the Tk thread."""
        icon = self._icon
        if icon is None:
            return
        if recording != self._recording:
            self._recording = recording
            try:
                icon.icon = self._record_image if recording else self._idle_image
            except Exception:
                pass
        if title and title != self._title:
            self._title = title
            try:
                icon.title = title
            except Exception:
                pass
        try:
            icon.update_menu()
        except Exception:
            pass
