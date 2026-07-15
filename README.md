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

> Built around a **parallel multi-core render pool**, **LRU pixmap cache**, and **lazy on-demand rendering** — the GUI stays responsive even on 1000+ page PDFs.

- **Instant open even for huge books** — a 1760-page PDF opens in well under a second (page widgets build in the background, no upfront rasterisation)
- **Parallel across CPU cores** — page rendering, full-text search, whole-document OCR, and text extraction all fan out over a thread pool (each worker with its own PDF handle)
- **Crisp on HiDPI displays** — pages render at the screen's device pixel ratio (no blurry upscaling), and the UI scales correctly at any Windows/macOS display scale
- Only visible pages + a small prefetch window are rendered; cached bitmaps make scrolling instantaneous
- Thumbnails show real **page previews**, streamed in from background threads — no UI freeze
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
- Page Thumbnails sidebar with real **page previews** (lazy, background-generated)
- Outline / Table of Contents sidebar
- User Bookmarks sidebar (per-file, persisted)
- Go to page... dialog
- Recent files (up to 10 remembered; newest 5 shown on the start screen) — remove any entry with its **✕** button

</td>
<td valign="top" width="50%">

#### Text & Search
- Full-text search with highlight + next/prev (**parallelised** across cores)
- Click & drag text selection (auto-clears on Esc)
- Copy selection / Copy whole page
- Extract entire document text to .txt (**parallelised**)

#### OCR (Tesseract)
- OCR current page
- OCR a selected rectangle
- OCR whole document — **parallel**, cancellable progress dialog
- Multi-language: `eng`, `fra`, `deu`, `eng+fra`, …

#### Listen
- Read Aloud (offline, sentence-highlighted)
- **Create Audiobook from Pages** — pick pages via a preview grid and export a natural-sounding MP3/WAV using Microsoft neural voices (`edge-tts`; needs internet)
- **Whole Book by Chapters** — auto-split the entire book by its table of contents and export **one audio file per chapter** into a folder

#### Modern UI
- Light & Dark themes (true PDF pixel inversion)
- Sepia reading tint
- Rounded controls, accent-blue highlights
- Wrapping toolbar — controls flow onto extra rows when the window narrows (nothing is hidden behind a `>>` overflow menu)
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
| **pyttsx3** | `>=2.90`   | Offline Read-Aloud (system SAPI/NSSpeech voices) — *optional* |
| **edge-tts**| `>=6.1.0`  | Natural neural voices for the **Create Audiobook** feature (requires internet) — *optional* |

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

:: 5. (Optional) Read-Aloud + natural-voice Audiobook export
pip install pyttsx3 edge-tts
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

# 5. (Optional) Read-Aloud + natural-voice Audiobook export
pip install pyttsx3 edge-tts
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

PyInstaller bundles Python, Qt, and PyMuPDF into a distributable **folder** (`--onedir`, which launches instantly with no self-extraction). Build scripts live in **`build/`** and use **`build/App_icon.png`** as the application icon.

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
<summary><b>Windows  →  <code>dist\JPDFReader\JPDFReader.exe</code></b></summary>

From the project root:

```powershell
.\build\build_windows.bat
```

What it does:
1. Verifies the venv exists and uses a compatible Python (refuses 3.10.0)
2. Installs/upgrades **PyInstaller** and **Pillow** into the venv
3. Generates `build\App_icon.ico` from `build\App_icon.png`
4. Cleans previous builds (`build_temp\`, `dist\`, stale `.spec`)
5. Runs PyInstaller with `--onedir --windowed`, bundling `edge-tts`/`pyttsx3` and embedding the icon
6. **On success**, deletes the generated `App_icon.ico`, the `build_temp\` folder and the `.spec` file so the working tree stays clean
7. **On failure**, keeps `App_icon.ico` and prints a clear `BUILD FAILED` message so you can retry

Output: **`dist\JPDFReader\`** — a folder containing `JPDFReader.exe` and its dependencies. **Distribute the whole folder** (e.g. zip it). Run `JPDFReader.exe` inside it; no Python install required, and it launches instantly with no extraction step.

> **Tip:** close any running `JPDFReader.exe` before rebuilding — a running instance locks the files in `dist\` and the build will fail with *"Access is denied"*.

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
5. Runs PyInstaller with `--windowed --osx-bundle-identifier com.jpdfreader.app`, bundling `edge-tts`/`pyttsx3` and embedding the icon
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
pyinstaller --noconfirm --clean --windowed --onedir `
    --name "JPDFReader" `
    --icon "build\App_icon.ico" `
    --add-data "build\App_icon.png;." `
    --workpath build_temp --specpath build_temp --distpath dist `
    --collect-submodules fitz --collect-submodules PyQt6 `
    --collect-submodules pyttsx3 --hidden-import pyttsx3.drivers `
    --hidden-import pyttsx3.drivers.sapi5 --hidden-import comtypes `
    --collect-submodules edge_tts --collect-submodules aiohttp `
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
    --collect-submodules pyttsx3 \
    --collect-submodules edge_tts --collect-submodules aiohttp \
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
| **OCR Whole Document...**  | OCRs every page **in parallel across CPU cores** with a cancellable progress dialog; saves to `.txt` in page order |
| **Set OCR Language...**    | e.g. `eng`, `fra`, `deu`, `spa`, or combinations like `eng+fra` |

> If a page already contains selectable text, the app offers to use it directly instead of running (slower) OCR.

---

## Audiobook Export (natural neural voices)

Turn any range of pages into a **natural-sounding audiobook** file. Unlike the offline
Read-Aloud feature (which uses robotic system voices), this uses Microsoft's **neural
text-to-speech** voices via [`edge-tts`](https://github.com/rany2/edge-tts).

### Requirements

```bash
pip install edge-tts
```

> **Requires an internet connection** — `edge-tts` streams synthesized audio from
> Microsoft's cloud service. It is free and needs no API key. If `edge-tts` isn't
> installed or you're offline, the app tells you clearly instead of failing silently.

### Using it

Open **Tools → Create Audiobook from Pages…** (or the **🎧 Audiobook** toolbar
button, or press `Ctrl/⌘+Shift+B`). A dialog opens with:

| Control | What it does |
|--------|-------------|
| **Page preview grid** | A scrollable grid of page thumbnails, each with a checkbox. The current page is preselected. |
| **Select All / Clear** | Toggle every page at once. |
| **Range** | Type e.g. `1-5, 8, 12` and click **Apply** to select those pages. |
| **Voice** | Pick from 300+ neural voices (accents/languages fetched live). Defaults to **Aria (US English)**; your choice is remembered. |
| **Speed** | Slow / Normal / Fast / Faster. |
| **Format** | **MP3** (default) or **WAV** (WAV conversion needs `ffmpeg` on PATH). |
| **Generate Audiobook…** | Choose an output file; a progress bar shows per-page synthesis. When finished you can open the file directly. |

Text is taken from each page's embedded text layer, falling back to OCR results
(enable **Auto-OCR Scanned Pages** in the Tools menu first for scanned PDFs).

### Whole Book by Chapters

If the PDF has a table of contents, the same dialog offers **Whole Book by
Chapters…** — it splits the entire book by its top-level TOC entries and exports
**one audio file per chapter**:

| Control | What it does |
|--------|-------------|
| **Chapter list** | A checkable list of the book's chapters with their page ranges. Numbered chapters (e.g. "Chapter 3") are pre-selected. |
| **Numbered / All / None** | Quickly choose which chapters to export. |
| **Whole Book by Chapters (N)…** | Extracts each selected chapter's text (in parallel), synthesizes each with the chosen voice/speed, and writes files into a new **`<Book Name> - Audiobook`** sub-folder in a folder you pick. Each file starts by reading the chapter title. |

If the PDF has no table of contents, this option is disabled and you can still
use **Create Audiobook from Pages** instead.

Works on both **Windows and macOS**.

---

## How Dark Mode works

True dark mode — not just a tinted background:

1. Each PDF page is rendered to a pixmap via PyMuPDF on a background render thread, at the screen's **device pixel ratio** so it stays crisp on HiDPI displays.
2. A C-implemented per-pixel RGB inversion (`QImage.invertPixels`) is applied.
3. White backgrounds become deep black, dark text becomes light, while colored figures remain recognisable.
4. A modern dark Qt stylesheet wraps the rest of the UI.

**Sepia mode** is essentially free: it reuses the light bitmap and overlays a translucent warm tint at paint-time via `CompositionMode_Multiply` — no extra render needed.

---

## Performance & Parallelism

The app is designed to stay responsive on very large documents and to use all your CPU cores:

- **Parallel render pool** — a pool of worker threads (up to `cores − 1`, capped at 6) renders pages concurrently. PyMuPDF isn't safe sharing one document across threads, so **each worker opens its own PDF handle**.
- **Chunked open** — opening a huge PDF builds the first pages immediately and creates the remaining page placeholders in the background, so the window is interactive right away (a 1760-page book opens in well under a second).
- **Parallel batch jobs** — full-text search, whole-document OCR, and text extraction fan out over a thread pool (`concurrent.futures`), each thread with its own PDF handle; results are reassembled in page order.
- **HiDPI-correct rendering** — pixmaps are rasterised at `zoom × devicePixelRatio` and tagged with the ratio, so pages are pixel-sharp and never upscaled/blurred. The UI uses Qt's `PassThrough` high-DPI rounding for clean scaling at 125% / 150% display scales.
- **Caching & laziness** — an LRU pixmap cache plus visible-only rendering keep scrolling instant; thumbnails and page-size refinement are computed lazily only for what's on screen.

All of this uses `os.cpu_count()`, `threading`, and `concurrent.futures` — identical behaviour on **Windows and macOS**.

---

## Project Layout

```
J_PDF_READER/
├── pdf_reader.py              # The application (single file, well-sectioned)
├── build/
│   ├── App_icon.png           # User-provided square PNG icon (1024x1024 recommended)
│   ├── make_icon.py           # PNG -> ICO converter (Windows; uses Pillow)
│   ├── _pyinstaller_runner.py # PyInstaller wrapper that patches Python 3.10.0 dis bug
│   ├── build_windows.bat      # Build dist\JPDFReader\ (folder with JPDFReader.exe)
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
| Read-Aloud (TTS) | pyttsx3 — offline system voices (SAPI5 / NSSpeech)    |
| Audiobook (TTS)  | edge-tts — Microsoft neural voices (online, free)     |
| Parallelism      | `threading` + `concurrent.futures` render/CPU pools   |
| Packaging        | PyInstaller — `--onedir` folder (`.exe`) / `.app` bundle |

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

<details>
<summary><b>Windows build fails with "Access is denied" in <code>dist\</code></b></summary>

A running instance of `JPDFReader.exe` locks the files being overwritten. Close the app (or `taskkill /IM JPDFReader.exe /F`) and rebuild.

</details>

<details>
<summary><b>Audiobook says edge-tts is missing / can't connect</b></summary>

Install it into the venv with `pip install edge-tts`. It streams neural-voice audio from Microsoft's free cloud service, so it **requires an internet connection** — if you're offline the dialog reports it instead of failing silently. WAV output additionally needs `ffmpeg` on PATH (MP3 does not).

</details>

---

## License

Personal / internal use.
