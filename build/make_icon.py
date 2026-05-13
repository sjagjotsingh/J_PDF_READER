"""
Convert build/App_icon.png to a Windows .ico file with multiple sizes.

Used by build_windows.bat. macOS uses native `sips` + `iconutil` instead.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, "App_icon.png")
    out = os.path.join(here, "App_icon.ico")

    if not os.path.isfile(src):
        print(
            f"App_icon.png not found in {here} - skipping icon generation.",
            file=sys.stderr,
        )
        return 0  # not an error; build proceeds without an icon

    try:
        from PIL import Image
    except ImportError:
        print(
            "Pillow not installed in this environment - skipping icon generation.",
            file=sys.stderr,
        )
        return 0

    img = Image.open(src).convert("RGBA")

    # Crop to square if not already, taking the largest centered square.
    w, h = img.size
    if w != h:
        s = min(w, h)
        left = (w - s) // 2
        top = (h - s) // 2
        img = img.crop((left, top, left + s, top + s))

    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    img.save(out, format="ICO", sizes=sizes)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
