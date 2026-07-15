#!/usr/bin/env bash
# ===========================================================================
#  Build a standalone macOS .app bundle of J PDF Reader.
#  Output: <project root>/dist/JPDFReader.app
#
#  This file uses the .command extension so it can be double-clicked from
#  Finder - macOS will open Terminal and run it automatically.
#
#  From a terminal you can also run it explicitly:
#      ./build/build_mac.command
#  (chmod +x build/build_mac.command once)
# ===========================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

if [ ! -x "venv/bin/python" ]; then
    cat <<'EOF'

ERROR: venv not found in project root.

Create it first:
    python3 -m venv venv
    source venv/bin/activate
    pip install PyQt6 PyMuPDF Pillow pyttsx3 edge-tts

See README.md for the full setup instructions.

EOF
    exit 1
fi

PY="venv/bin/python"

# Detect Python 3.10.0 just for an informational message; the wrapper script
# (build/_pyinstaller_runner.py) will monkey-patch the broken
# dis._get_const_info so the build can still succeed on 3.10.0.
if "$PY" -c "import sys; sys.exit(42 if sys.version_info[:3] == (3,10,0) else 0)" 2>/dev/null; then
    :
elif [ "$?" = "42" ]; then
    echo
    echo "NOTE: Python 3.10.0 detected - applying dis._get_const_info patch"
    echo "      at PyInstaller startup. (Upgrading to 3.10.1+ or 3.11+ is"
    echo "      still recommended.)"
    echo
fi

echo
echo "===== Installing/upgrading PyInstaller ====="
"$PY" -m pip install --quiet --upgrade pyinstaller

echo
echo "===== Generating build/App_icon.icns from App_icon.png ====="
ICON_PNG="$SCRIPT_DIR/App_icon.png"
ICNS="$SCRIPT_DIR/App_icon.icns"

if [ -f "$ICON_PNG" ]; then
    if ! command -v sips >/dev/null 2>&1 || ! command -v iconutil >/dev/null 2>&1; then
        echo "WARNING: sips/iconutil not available - skipping ICNS generation."
        ICNS=""
    else
        ICONSET_DIR="$SCRIPT_DIR/App_icon.iconset"
        rm -rf "$ICONSET_DIR"
        mkdir -p "$ICONSET_DIR"

        # Apple's required iconset sizes (1x and 2x retina variants)
        for s in 16 32 64 128 256 512; do
            sips -z $s $s "$ICON_PNG" \
                 --out "$ICONSET_DIR/icon_${s}x${s}.png" >/dev/null
            d=$((s * 2))
            sips -z $d $d "$ICON_PNG" \
                 --out "$ICONSET_DIR/icon_${s}x${s}@2x.png" >/dev/null
        done

        iconutil -c icns "$ICONSET_DIR" -o "$ICNS"
        rm -rf "$ICONSET_DIR"
        echo "Wrote $ICNS"
    fi
else
    echo "App_icon.png not found in build/ - building without custom icon."
    ICNS=""
fi

echo
echo "===== Cleaning previous build artifacts ====="
rm -rf build_temp dist JPDFReader.spec

# Use absolute paths so PyInstaller resolves --icon and --add-data correctly
# regardless of where the spec file is created.
ICON_FLAG=""
ADD_DATA_FLAG=""
[ -n "$ICNS" ] && [ -f "$ICNS" ] && ICON_FLAG="--icon=$ROOT/build/App_icon.icns"
[ -f "$ICON_PNG" ] && ADD_DATA_FLAG="--add-data=$ROOT/build/App_icon.png:."

echo
echo "===== Building JPDFReader.app ====="
set +e
"$PY" "$ROOT/build/_pyinstaller_runner.py" \
    --noconfirm \
    --clean \
    --windowed \
    --name "JPDFReader" \
    --osx-bundle-identifier com.jpdfreader.app \
    --workpath "$ROOT/build_temp" \
    --distpath "$ROOT/dist" \
    $ICON_FLAG \
    $ADD_DATA_FLAG \
    --collect-submodules fitz \
    --collect-submodules PyQt6 \
    --collect-submodules pyttsx3 \
    --collect-submodules edge_tts \
    --collect-submodules aiohttp \
    "$ROOT/pdf_reader.py"
BUILD_RC=$?
set -e

# ----------------------------------------------------------------------
#  Verify the .app exists; only then clean up and report success.
# ----------------------------------------------------------------------
APP="$ROOT/dist/JPDFReader.app"
if [ "$BUILD_RC" -ne 0 ] || [ ! -d "$APP" ]; then
    cat <<EOF

===== BUILD FAILED =====

PyInstaller did not produce dist/JPDFReader.app.
Scroll up for the underlying error.

The generated icon file build/App_icon.icns has been kept so you can retry.

EOF
    if [ -t 0 ]; then
        echo ""
        read -p "Press Enter to close..." _
    fi
    exit 1
fi

echo
echo "===== Cleaning up generated icon and intermediate files ====="
# Generated icon + iconset folder (mac only)
[ -f "$ROOT/build/App_icon.icns" ]     && rm -f "$ROOT/build/App_icon.icns"
[ -d "$ROOT/build/App_icon.iconset" ]  && rm -rf "$ROOT/build/App_icon.iconset"
# PyInstaller intermediates and spec
[ -d "$ROOT/build_temp" ]              && rm -rf "$ROOT/build_temp"
[ -f "$ROOT/JPDFReader.spec" ]         && rm -f "$ROOT/JPDFReader.spec"
# Python bytecode cache directories created during analysis
[ -d "$ROOT/__pycache__" ]             && rm -rf "$ROOT/__pycache__"
[ -d "$ROOT/build/__pycache__" ]       && rm -rf "$ROOT/build/__pycache__"
# Any stray PyInstaller log files (not always created, but be safe)
[ -f "$ROOT/warn-JPDFReader.txt" ]     && rm -f "$ROOT/warn-JPDFReader.txt"
[ -f "$ROOT/xref-JPDFReader.html" ]    && rm -f "$ROOT/xref-JPDFReader.html"

cat <<EOF

===== BUILD COMPLETE =====

Output:  $APP
Size:    $(du -sh "$APP" | cut -f1)

Open it:   open dist/JPDFReader.app
Or move it to /Applications.

(First launch may be blocked by Gatekeeper - the bundle is unsigned. Bypass with:
    xattr -cr dist/JPDFReader.app
or right-click the .app -> Open -> Open in the warning dialog.)
EOF

# Keep the Terminal window open if launched by double-click from Finder.
if [ -t 0 ]; then
    echo ""
    read -p "Press Enter to close..." _
fi
