@echo off
REM ===========================================================================
REM  Build a standalone Windows .exe of J PDF Reader.
REM  Output: <project root>\dist\JPDFReader.exe (single file, no console).
REM
REM  Run from any cwd:   build\build_windows.bat
REM ===========================================================================

setlocal enableextensions
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%.."

if not exist "venv\Scripts\python.exe" (
    echo.
    echo ERROR: venv not found in project root.
    echo.
    echo Create it first:
    echo     python -m venv venv
    echo     .\venv\Scripts\activate
    echo     pip install PyQt6 PyMuPDF Pillow
    echo.
    echo See README.md for the full setup instructions.
    echo.
    exit /b 1
)

set "PY=venv\Scripts\python.exe"
set "ROOT=%CD%"

REM Detect Python 3.10.0 just for an informational message; the wrapper
REM script (build\_pyinstaller_runner.py) will monkey-patch the broken
REM dis._get_const_info so the build can still succeed.
"%PY%" -c "import sys; sys.exit(42 if sys.version_info[:3] == (3,10,0) else 0)" >nul 2>&1
if "%ERRORLEVEL%"=="42" (
    echo.
    echo NOTE: Python 3.10.0 detected - applying dis._get_const_info patch
    echo       at PyInstaller startup. ^(Upgrading to 3.10.1+ or 3.11+ is
    echo       still recommended.^)
    echo.
)

echo.
echo ===== Installing/upgrading PyInstaller + Pillow =====
"%PY%" -m pip install --quiet --upgrade pyinstaller pillow
if errorlevel 1 (
    echo ERROR: failed to install PyInstaller / Pillow.
    exit /b 1
)

echo.
echo ===== Generating build\App_icon.ico from App_icon.png =====
"%PY%" build\make_icon.py

echo.
echo ===== Cleaning previous build artifacts =====
if exist "build_temp"        rmdir /s /q "build_temp"
if exist "dist"              rmdir /s /q "dist"
if exist "JPDFReader.spec"   del /q "JPDFReader.spec"

REM Build absolute paths for --icon and --add-data so PyInstaller resolves
REM them correctly regardless of where the .spec file is generated.
set "ICON_FLAG="
set "ADD_DATA_FLAG="
if exist "%ROOT%\build\App_icon.ico" set "ICON_FLAG=--icon=%ROOT%\build\App_icon.ico"
if exist "%ROOT%\build\App_icon.png" set "ADD_DATA_FLAG=--add-data=%ROOT%\build\App_icon.png;."

echo.
echo ===== Building JPDFReader.exe (one-file, windowed) =====
"%PY%" "%ROOT%\build\_pyinstaller_runner.py" ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --onefile ^
    --name "JPDFReader" ^
    --workpath "%ROOT%\build_temp" ^
    --distpath "%ROOT%\dist" ^
    %ICON_FLAG% ^
    %ADD_DATA_FLAG% ^
    --collect-submodules fitz ^
    --collect-submodules PyQt6 ^
    "%ROOT%\pdf_reader.py"

set "BUILD_RC=%ERRORLEVEL%"

REM ----------------------------------------------------------------------
REM  Verify the .exe exists; only then clean up and report success.
REM ----------------------------------------------------------------------
if not "%BUILD_RC%"=="0" goto :build_failed
if not exist "%ROOT%\dist\JPDFReader.exe" goto :build_failed

echo.
echo ===== Cleaning up generated icon and intermediate files =====
REM Generated icon
if exist "%ROOT%\build\App_icon.ico"      del /q "%ROOT%\build\App_icon.ico"
REM PyInstaller intermediates and spec
if exist "%ROOT%\build_temp"              rmdir /s /q "%ROOT%\build_temp"
if exist "%ROOT%\JPDFReader.spec"         del /q "%ROOT%\JPDFReader.spec"
REM Python bytecode cache directories created during analysis
if exist "%ROOT%\__pycache__"             rmdir /s /q "%ROOT%\__pycache__"
if exist "%ROOT%\build\__pycache__"       rmdir /s /q "%ROOT%\build\__pycache__"
REM Any stray PyInstaller log file (not always created, but be safe)
if exist "%ROOT%\warn-JPDFReader.txt"     del /q "%ROOT%\warn-JPDFReader.txt"
if exist "%ROOT%\xref-JPDFReader.html"    del /q "%ROOT%\xref-JPDFReader.html"

echo.
echo ===== BUILD COMPLETE =====
echo.
echo Output:  %ROOT%\dist\JPDFReader.exe
for %%F in ("%ROOT%\dist\JPDFReader.exe") do echo Size:    %%~zF bytes
echo.
echo Copy that single .exe to any Windows machine and run it.
echo (Tesseract is only required for OCR features; not bundled.)
echo.
endlocal
exit /b 0

:build_failed
echo.
echo ===== BUILD FAILED =====
echo.
echo PyInstaller did not produce dist\JPDFReader.exe.
echo Scroll up for the underlying error.
echo.
echo The generated icon file build\App_icon.ico has been kept so you can retry.
echo.
endlocal
exit /b 1
