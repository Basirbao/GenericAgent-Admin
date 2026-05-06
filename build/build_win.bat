@echo off
rem Build a Windows installer (setup.exe) for GenericAgent-Admin.
rem
rem Uses Nuitka (compiles to native C) instead of PyInstaller. Trade-off:
rem ~10x slower build (5-15 min on a clean cache), but the resulting binary
rem doesn't share PyInstaller's bootloader signature that SentinelOne /
rem Defender flag as malicious. Inno Setup's [Files] source path is
rem unchanged, so installer.iss stays as-is.
rem
rem Prerequisites (one-time):
rem   * Python 3.11 or 3.12 with pip on PATH
rem   * Node.js LTS                                  (for `npm run build`)
rem   * Inno Setup 6+                                https://jrsoftware.org/isdl.php
rem   * MSVC Build Tools (Nuitka downloads MinGW automatically if missing)
rem   * nuitka (auto-installed if missing)
rem
rem Usage:
rem   build\build_win.bat
rem
rem Output:
rem   build\dist\GenericAgent-Admin\        onedir folder + .exe (run-in-place)
rem   build\GenericAgent-Admin-<ver>-Setup.exe   Inno Setup installer

setlocal enabledelayedexpansion

cd /d "%~dp0\.."

rem ── version from pyproject.toml ─────────────────────────────────
for /f "delims=" %%v in ('python -c "import tomllib;print(tomllib.load(open(\"pyproject.toml\",\"rb\"))[\"project\"][\"version\"])"') do set VERSION=%%v
echo Building GenericAgent-Admin %VERSION%

rem ── 1/4 build webui ────────────────────────────────────────────
echo.
echo ==^> [1/4] Building webui (npm)
pushd webui
if not exist node_modules (
    call npm ci || goto :error
)
call npm run build || goto :error
popd

rem ── 2/4 clean ──────────────────────────────────────────────────
echo.
echo ==^> [2/4] Cleaning previous build output
if exist build\build rmdir /s /q build\build
if exist build\dist rmdir /s /q build\dist
if exist build\GenericAgent-Admin-%VERSION%-Setup.exe del /q build\GenericAgent-Admin-%VERSION%-Setup.exe

rem ── 3/4 nuitka ─────────────────────────────────────────────────
rem We deliberately DO NOT use --enable-plugin=pywebview here. The
rem bundled plugin in Nuitka 4.0 has two bugs that bite us:
rem   * it actively excludes webview.platforms.win32 even though
rem     winforms.py imports it at module load (line 22)
rem   * any user --include-module/--include-package targeting webview
rem     is treated as a conflict with the plugin's exclusion list and
rem     aborts compilation
rem Listing webview + clr_loader + pythonnet + cffi by hand pulls in
rem exactly what the runtime needs, no plugin involved, no conflicts.
rem hiddenimports / data files / excludes mirror what build/admin.spec did
rem for PyInstaller. Keep them in sync if the project grows new runtime
rem dependencies (e.g. a new server.routes.* package would need adding via
rem --include-package=... since nuitka can't see string-imported modules
rem any better than PyInstaller could).
echo.
echo ==^> [3/4] Running nuitka
python -c "import nuitka" 2>nul
if errorlevel 1 (
    echo     nuitka missing - installing
    python -m pip install --upgrade nuitka ordered-set zstandard || goto :error
)
python -m nuitka launch_webui.pyw ^
    --standalone ^
    --assume-yes-for-downloads ^
    --windows-console-mode=disable ^
    --output-dir=build\dist ^
    --output-filename=GenericAgent-Admin.exe ^
    --disable-plugin=pywebview ^
    --include-package=server ^
    --include-package=uvicorn ^
    --include-package=apscheduler ^
    --include-package=websockets ^
    --include-package=pystray ^
    --include-package=webview ^
    --include-package-data=webview ^
    --include-package=clr_loader ^
    --include-package=pythonnet ^
    --include-package=cffi ^
    --include-data-dir=webui\dist=webui\dist ^
    --nofollow-import-to=tkinter ^
    --nofollow-import-to=test ^
    --nofollow-import-to=unittest || goto :error

rem Nuitka emits "<entry-script>.dist" — rename to match installer.iss's
rem expected source path (build\dist\GenericAgent-Admin\).
if exist build\dist\GenericAgent-Admin rmdir /s /q build\dist\GenericAgent-Admin
ren build\dist\launch_webui.dist GenericAgent-Admin || goto :error

if not exist "build\dist\GenericAgent-Admin\GenericAgent-Admin.exe" (
    echo ERROR: GenericAgent-Admin.exe not produced - check nuitka log above
    goto :error
)

rem ── 4/4 Inno Setup ─────────────────────────────────────────────
echo.
echo ==^> [4/4] Running Inno Setup compiler
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC where iscc >nul 2>nul && set "ISCC=iscc"

if not defined ISCC (
    echo     Inno Setup not found.
    echo     Install from: https://jrsoftware.org/isdl.php
    echo     The onedir build is still usable directly:
    echo         build\dist\GenericAgent-Admin\GenericAgent-Admin.exe
    exit /b 0
)

"%ISCC%" /Qp build\installer.iss || goto :error

echo.
echo OK.
echo    EXE:   build\dist\GenericAgent-Admin\GenericAgent-Admin.exe
echo    Setup: build\GenericAgent-Admin-%VERSION%-Setup.exe
exit /b 0

:error
echo.
echo BUILD FAILED. See messages above.
exit /b 1
