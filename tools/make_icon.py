"""Generate the application icon (multi-size .ico + .png preview).

The icon is drawn by the same code the UI uses for its logo, so the EXE icon
always matches the app. Run by build_exe.bat before PyInstaller.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from srp.ui.theme import app_icon_image  # noqa: E402

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    assets = ROOT / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    image = app_icon_image(256).convert("RGBA")
    ico_path = assets / "srpro.ico"
    png_path = assets / "srpro.png"

    image.save(png_path, "PNG")
    image.save(ico_path, format="ICO", sizes=ICO_SIZES)

    print(f"Icon written: {ico_path}")
    print(f"Preview:      {png_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
