<div align="center">

# J PDF Reader

### A fast, modern, feature-rich PDF reader for Windows & macOS — with true dark mode and OCR.

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-0078d4?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![Qt](https://img.shields.io/badge/PyQt-6-41cd52?style=flat-square&logo=qt&logoColor=white)
![PDF Engine](https://img.shields.io/badge/PyMuPDF-fitz-1f6feb?style=flat-square)
![License](https://img.shields.io/badge/license-Personal-orange?style=flat-square)

</div>

---

## Highlights

> Built around a **background rendering thread**, **LRU pixmap cache**, and **lazy on-demand rendering** — the GUI stays responsive even on 1000+ page PDFs.

- Instant document open (no upfront rasterisation)
- Only visible pages + a small prefetch window are rendered
- Cached bitmaps make scrolling back/forward instantaneous
- Thumbnails stream in from a background thread — no UI freeze
- Resize / zoom / theme changes are debounced
- Cross-platform — runs natively on **Windows** and **macOS**

---

## Features at a glance

<table>
<tr>
<td valign="top" width="50%">

#### Reading
- Continuous, Single-Page & Two-Page (book) modes
- Zoom in/out, Fit Width, Fit Page, 100%, custom %
- `Ctrl/⌘ + Mouse Wheel` zoom
- Rotate left / right
- Auto-Scroll / Reading mode (with speed control)
- Fullscreen and Presentation Mode
- Resume last page per file

#### Navigation
- Page Thumbnails sidebar (background-generated)
- Outline / Table of Contents sidebar
- User Bookmarks sidebar (per-file, persisted)
- Go to page... dialog
- Recent files (last 10)

</td>
<td valign="top" width="50%">

#### Text & Search
- Full-text search with highlight + next/prev
- Click & drag text selection (auto-clears on Esc)
- Copy selection / Copy whole page
- Extract entire document text to .txt

#### OCR (Tesseract)
- OCR current page
- OCR a selected rectangle
- OCR whole document (with progress dialog)
- Multi-language: `eng`, `fra`, `deu`, `eng+fra`, …

#### Modern UI
- Light & Dark themes (true PDF pixel inversion)
- Sepia reading tint
- Rounded controls, accent-blue highlights
- Persistent window state & preferences
- Drag-and-drop a PDF anywhere

</td>
</tr>
</table>

---

## Quick Start

### Prerequisites

- **Python 3.10.1+** (3.11 or 3.12 recommended) — [download here](https://www.python.org/downloads/)
  - Windows: tick **"Add Python to PATH"** during install
  - macOS: install via the official `.pkg` or `brew install python@3.12`

> **Don't use Python 3.10.0** &mdash; that exact release shipped with a stdlib bug (`dis._get_const_info`) that crashes PyInstaller. Any later 3.10.x or any 3.11/3.12 works fine. (The build scripts auto-patch this version, but using 3.10.1+ is still cleaner.)

### Dependencies

| Package | Min version | Purpose |
|---------|-------------|---------|
| **PyQt6**   | `>=6.5.0`  | GUI framework (Qt6 bindings) |
| **PyMuPDF** | `>=1.23.0` | PDF rendering, search, text extraction, OCR (`fitz`) |
| **Pillow**  | `>=10.0.0` | PNG → ICO conversion for the build script |

---

### Setup (clone, create venv, install)

<details open>
<summary><b>Windows (PowerShell or CMD)</b></summary>

```powershell
:: 1. Clone or copy the project, then enter the folder
cd J_PDF_READER

:: 2. Create the virtual environment
python -m venv venv

:: 3. Activate it
.\venv\Scripts\activate

:: 4. Upgrade pip and install all dependencies
python -m pip install --upgrade pip
pip install "PyQt6>=6.5.0" "PyMuPDF>=1.23.0" "Pillow>=10.0.0"
```

To leave the venv later: `deactivate`

</details>

<details open>
<summary><b>macOS (Terminal — bash / zsh)</b></summary>

```bash
# 1. Clone or copy the project, then enter the folder
cd J_PDF_READER

# 2. Create the virtual environment
python3 -m venv venv

# 3. Activate it
source venv/bin/activate

# 4. Upgrade pip and install all dependencies
python -m pip install --upgrade pip
pip install "PyQt6>=6.5.0" "PyMuPDF>=1.23.0" "Pillow>=10.0.0"
```

To leave the venv later: `deactivate`

</details>

---

### Run the app

With the venv activated:

```bash
python pdf_reader.py
```

Open a specific PDF on launch:

```bash
# Windows
python pdf_reader.py "C:\path\to\file.pdf"

# macOS
python pdf_reader.py "/Users/you/Documents/file.pdf"
```

You can also drag & drop any PDF onto the window.

---

## Build a standalone app

PyInstaller bundles Python, Qt, and PyMuPDF into a single distributable. Build scripts live in **`build/`** and use **`build/App_icon.png`** as the application icon.

### Application icon

Drop a square PNG (1024×1024 recommended) named **`App_icon.png`** into the `build/` folder. The build scripts will:

- Convert it to **`App_icon.ico`** automatically on Windows (uses Pillow)
- Convert it to **`App_icon.icns`** automatically on macOS (uses native `sips` + `iconutil`)
- Embed it into the executable / `.app` bundle
- Make the app load it at runtime so it also appears in the **window title bar, taskbar, dock, and alt-tab**

If `App_icon.png` is missing, the build still succeeds — just without a custom icon.

### Build commands

> Make sure your venv is set up (see *Quick Start* above). The build scripts use the venv's Python automatically.

<details open>
<summary><b>Windows  →  <code>dist\JPDFReader.exe</code></b></summary>

From the project root:

```powershell
.\build\build_windows.bat
```

What it does:
1. Verifies the venv exists and uses a compatible Python (refuses 3.10.0)
2. Installs/upgrades **PyInstaller** and **Pillow** into the venv
3. Generates `build\App_icon.ico` from `build\App_icon.png`
4. Cleans previous builds (`build_temp\`, `dist\`, stale `.spec`)
5. Runs PyInstaller with `--onefile --windowed`, embedding the icon
6. **On success**, deletes the generated `App_icon.ico`, the `build_temp\` folder and the `.spec` file so the working tree stays clean
7. **On failure**, keeps `App_icon.ico` and prints a clear `BUILD FAILED` message so you can retry

Output: **`dist\JPDFReader.exe`** — a single ~80 MB executable. Copy it to any Windows machine and run it; no Python install required.

</details>

<details open>
<summary><b>macOS  →  <code>dist/JPDFReader.app</code></b></summary>

The macOS build script is named `build_mac.command` so it can be **double-clicked from Finder** (macOS will open Terminal and run it automatically).

From a terminal:

```bash
chmod +x build/build_mac.command   # one-time
./build/build_mac.command
```

Or just double-click `build/build_mac.command` in Finder.

What it does:
1. Verifies the venv exists and uses a compatible Python (refuses 3.10.0)
2. Installs/upgrades **PyInstaller** into the venv
3. Generates `build/App_icon.icns` from `build/App_icon.png` (via `sips` + `iconutil`)
4. Cleans previous builds (`build_temp/`, `dist/`, stale `.spec`)
5. Runs PyInstaller with `--windowed --osx-bundle-identifier com.jpdfreader.app`, embedding the icon
6. **On success**, deletes the generated `App_icon.icns`, the `build_temp/` folder and the `.spec` file so the working tree stays clean
7. **On failure**, keeps `App_icon.icns` and prints a clear `BUILD FAILED` message so you can retry

Output: **`dist/JPDFReader.app`** — drag into `/Applications` or run with `open dist/JPDFReader.app`.

> **First launch on macOS** &nbsp; The bundle is unsigned, so Gatekeeper may block it. Right-click the app → **Open** → **Open** to bypass once.
> Or from Terminal: `xattr -cr dist/JPDFReader.app && open dist/JPDFReader.app`

</details>

> **Tesseract is not bundled** &nbsp; OCR requires Tesseract to be installed separately on the user's machine — see [OCR section](#ocr-optical-character-recognition) below.

<details>
<summary><b>Manual PyInstaller command (advanced / cross-platform reference)</b></summary>

If you want to invoke PyInstaller directly (or your CI does), the equivalent commands are:

**Windows:**
```powershell
pyinstaller --noconfirm --clean --windowed --onefile `
    --name "JPDFReader" `
    --icon "build\App_icon.ico" `
    --add-data "build\App_icon.png;." `
    --workpath build_temp --specpath build_temp --distpath dist `
    --collect-submodules fitz --collect-submodules PyQt6 `
    pdf_reader.py
```

**macOS:**
```bash
pyinstaller --noconfirm --clean --windowed \
    --name "JPDFReader" \
    --osx-bundle-identifier com.jpdfreader.app \
    --icon "build/App_icon.icns" \
    --add-data "build/App_icon.png:." \
    --workpath build_temp --specpath build_temp --distpath dist \
    --collect-submodules fitz --collect-submodules PyQt6 \
    pdf_reader.py
```

</details>

---

## Keyboard Shortcuts

> On macOS, replace `Ctrl` with `⌘` (Cmd). PyQt maps shortcuts to the platform convention automatically.

| | |
|--|--|
| **Open / Close PDF**          | `Ctrl+O` / `Ctrl+W` |
| **Save copy / Print / Quit**  | `Ctrl+S` / `Ctrl+P` / `Ctrl+Q` |
| **Find / Next / Prev match**  | `Ctrl+F` / `F3` / `Shift+F3` |
| **Copy / Copy whole page**    | `Ctrl+C` / `Ctrl+Shift+C` |
| **Zoom in / out / 100%**      | `Ctrl++` / `Ctrl+-` / `Ctrl+0` |
| **Fit Width / Page**          | `Ctrl+1` / `Ctrl+2` |
| **Rotate left / right**       | `Ctrl+L` / `Ctrl+R` |
| **Dark / Sepia mode**         | `Ctrl+D` / `Ctrl+E` |
| **Fullscreen / Presentation** | `F11` / `F5` |
| **Previous / Next page**      | `PgUp` / `PgDown` or `←` / `→` |
| **Scroll page down / up**     | `Space` / `Shift+Space` |
| **First / Last page**         | `Ctrl+Home` / `Ctrl+End` |
| **Go to page…**               | `Ctrl+G` |
| **Add bookmark**              | `Ctrl+B` |
| **Toggle auto-scroll**        | `Ctrl+Shift+A` |
| **Auto-scroll faster / slower** | `Ctrl+Alt+=` / `Ctrl+Alt+-` |
| **Clear selection / search**  | `Esc` |
| **Zoom by mouse**             | `Ctrl + Wheel` |

---

## OCR (Optical Character Recognition)

Read text out of **scanned / image-based PDFs**. Uses [Tesseract](https://github.com/tesseract-ocr/tesseract) under the hood.

### Install Tesseract (one-time)

<details open>
<summary><b>Windows</b></summary>

1. Download installer: https://github.com/UB-Mannheim/tesseract/wiki
2. Install to the default location (`C:\Program Files\Tesseract-OCR\`)
3. Optionally select extra language packs during install
4. Restart J PDF Reader

</details>

<details open>
<summary><b>macOS</b></summary>

```bash
# Core engine + English
brew install tesseract

# Or with all languages bundled
brew install tesseract tesseract-lang
```

</details>

The app auto-detects Tesseract at the standard install paths on both platforms — **no PATH editing needed**.

### Using OCR

From the **Tools** menu or the **OCR** toolbar button:

| Command | What it does |
|--------|-------------|
| **OCR Current Page**       | Recognises text on the current page; shows it in a dialog with copy/save buttons |
| **OCR Selection**          | Drag a rectangle first, then run — perfect for grabbing a single paragraph or table cell |
| **OCR Whole Document...**  | OCR every page sequentially with a cancellable progress dialog; saves to `.txt` |
| **Set OCR Language...**    | e.g. `eng`, `fra`, `deu`, `spa`, or combinations like `eng+fra` |

> If a page already contains selectable text, the app offers to use it directly instead of running (slower) OCR.

---

## How Dark Mode works

True dark mode — not just a tinted background:

1. Each PDF page is rendered to a pixmap via PyMuPDF on the background thread.
2. A C-implemented per-pixel RGB inversion (`QImage.invertPixels`) is applied.
3. White backgrounds become deep black, dark text becomes light, while colored figures remain recognisable.
4. A modern dark Qt stylesheet wraps the rest of the UI.

**Sepia mode** is essentially free: it reuses the light bitmap and overlays a translucent warm tint at paint-time via `CompositionMode_Multiply` — no extra render needed.

---

## Project Layout

```
J_PDF_READER/
├── pdf_reader.py              # The application (single file, well-sectioned)
├── build/
│   ├── App_icon.png           # User-provided square PNG icon (1024x1024 recommended)
│   ├── make_icon.py           # PNG -> ICO converter (Windows; uses Pillow)
│   ├── _pyinstaller_runner.py # PyInstaller wrapper that patches Python 3.10.0 dis bug
│   ├── build_windows.bat      # Build dist\JPDFReader.exe
│   └── build_mac.command      # Build dist/JPDFReader.app (double-clickable on macOS)
├── venv/                      # Local virtual environment (git-ignored)
├── dist/                      # Build output (git-ignored)
├── README.md
└── .gitignore
```

Dependencies are installed manually via `pip install` — see [Prerequisites](#prerequisites) above. There is no `requirements.txt`.

---

## Tech Stack

| Layer            | Technology                                            |
|------------------|-------------------------------------------------------|
| Language         | Python 3.10+                                          |
| GUI              | PyQt6 — Qt6 bindings, native on Windows & macOS       |
| PDF engine       | PyMuPDF (`fitz`) — rendering, search, text, OCR       |
| OCR engine       | Tesseract (external; called via PyMuPDF)              |
| Packaging        | PyInstaller — single-file `.exe` / `.app` bundle      |

---

## Troubleshooting

<details>
<summary><b>Windows: "python is not recognized"</b></summary>

Reinstall Python from python.org and tick **"Add Python to PATH"**, or use the launcher: `py -m venv venv` instead of `python -m venv venv`.

</details>

<details>
<summary><b>macOS: "command not found: python"</b></summary>

macOS ships only `python3`. Use:
```bash
python3 -m venv venv
```
Once the venv is activated, plain `python` works inside it.

</details>

<details>
<summary><b>macOS Gatekeeper blocks the .app</b></summary>

The bundle isn't signed/notarized. Bypass once with:
```bash
xattr -cr dist/JPDFReader.app
open dist/JPDFReader.app
```
Or right-click the app → **Open** → **Open** in the warning dialog.

</details>

<details>
<summary><b>OCR fails with "Tesseract not found"</b></summary>

The auto-detector checks PATH plus standard install locations (`C:\Program Files\Tesseract-OCR\` on Windows, `/opt/homebrew/bin/` and `/usr/local/bin/` on macOS). If your install lives elsewhere, add that directory to PATH before launching the app.

</details>

---

## License

Personal / internal use.
