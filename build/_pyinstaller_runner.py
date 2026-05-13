"""
PyInstaller wrapper.

Python 3.10.0 (the very first 3.10 release, October 2021) shipped with a bug
in the stdlib `dis._get_const_info` helper that triggers
`IndexError: tuple index out of range` whenever a tool walks bytecode -
including PyInstaller's module-graph analyzer.  The bug was fixed in
Python 3.10.1.

Rather than force a Python upgrade, this wrapper monkey-patches the bad
function with a defensive version, then hands control to PyInstaller.

For Python 3.10.1+ / 3.11 / 3.12 / 3.13 it's a no-op.
"""
from __future__ import annotations

import sys


def _patch_dis_get_const_info() -> None:
    """Make dis._get_const_info tolerate out-of-range indices."""
    if sys.version_info[:3] != (3, 10, 0):
        return  # only the broken release
    import dis

    original = getattr(dis, "_get_const_info", None)
    if original is None:
        return

    def _safe_get_const_info(const_index, const_list):
        try:
            return original(const_index, const_list)
        except IndexError:
            return None, "<unknown>"

    dis._get_const_info = _safe_get_const_info  # type: ignore[attr-defined]


def main() -> int:
    _patch_dis_get_const_info()
    from PyInstaller.__main__ import run
    run()  # PyInstaller calls sys.exit() internally on error
    return 0


if __name__ == "__main__":
    sys.exit(main())
