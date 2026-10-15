@echo off
setlocal EnableDelayedExpansion
title POD Renamer - Setup
cd /d "%~dp0"
set "HERE=%~dp0"
echo.
echo  ==========================================
echo    POD Renamer - one-time setup
echo  ==========================================
echo.

rem ---- 1. Find a normal 64-bit Python 3.9+ (not the free-threaded "t" build)
set "PY="
call :findpy
if not defined PY (
    echo  A suitable Python was not found. Installing Python 3.12 now ^(takes a minute or two^)...
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    call :findpy
)
if not defined PY (
    echo.
    echo  Could not find or install a suitable Python automatically.
    echo  Please install Python 3.12 from https://www.python.org/downloads/
    echo  ^(the normal installer - NOT the "free-threaded" option^), then run this setup again.
    echo.
    pause
    exit /b 1
)
echo  Using Python: !PY!

rem ---- 2. Install the bits it needs (ready-made packages only, no compiling)
echo.
echo  Installing the PDF reader and Windows text-recognition helpers...
"!PY!" -m pip install --upgrade --only-binary=:all: --quiet --disable-pip-version-check pymupdf pillow winocr
if errorlevel 1 (
    echo  Trying again for this user only...
    "!PY!" -m pip install --user --upgrade --only-binary=:all: --quiet --disable-pip-version-check pymupdf pillow winocr
)
"!PY!" -c "import pymupdf, PIL, tkinter, winocr" 2>nul
if errorlevel 1 (
    echo.
    echo  Something went wrong installing the helpers ^(maybe the work network blocks it^).
    echo  Ask Alex to have a look.
    pause
    exit /b 1
)

rem ---- 3. Check Windows can read text from pictures (OCR) ----------------
"!PY!" -c "from winrt.windows.media.ocr import OcrEngine; from winrt.windows.globalization import Language; import sys; sys.exit(0 if OcrEngine.is_language_supported(Language('en')) else 1)" 2>nul
if errorlevel 1 (
    echo.
    echo  NOTE: Windows' English text recognition pack was not found.
    echo  Scans from a copier set to "searchable PDF" will still work.
    echo  To fix it for normal scans, open PowerShell as Administrator and run:
    echo     Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
    echo.
)

rem ---- 4. Copy the program and make a desktop icon -----------------------
set "DEST=%LOCALAPPDATA%\POD Renamer"
if not exist "%DEST%" mkdir "%DEST%"
copy /y "pod_renamer.py" "%DEST%\pod_renamer.py" >nul
copy /y "pod.ico" "%DEST%\pod.ico" >nul

for %%F in ("!PY!") do set "PYW=%%~dpFpythonw.exe"
if not exist "!PYW!" set "PYW=!PY!"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Rename PODs.lnk'));" ^
  "$s.TargetPath=$env:PYW; $s.Arguments=('\"' + $env:DEST + '\pod_renamer.py\"'); $s.WorkingDirectory=$env:DEST;" ^
  "$s.IconLocation=(Join-Path $env:DEST 'pod.ico'); $s.Description='Rename scanned PODs to their invoice numbers'; $s.Save()"

echo.
echo  ==========================================
echo    All set^^!  There is now a "Rename PODs"
echo    icon on the desktop.
echo  ==========================================
echo.
pause
exit /b 0

rem ---- Python finder: tries specific versions first, skips unsuitable builds
:findpy
for %%v in (3.13 3.12 3.11 3.10 3.14) do (
    if not defined PY (
        for /f "delims=" %%p in ('py -%%v "%HERE%check_python.py" 2^>nul') do (
            if exist "%%p" set "PY=%%p"
        )
    )
)
if not defined PY (
    for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
        if not defined PY if exist "%%~d\python.exe" (
            for /f "delims=" %%p in ('""%%~d\python.exe" "%HERE%check_python.py" 2>nul"') do (
                if exist "%%p" set "PY=%%p"
            )
        )
    )
)
if not defined PY (
    for /f "delims=" %%p in ('python "%HERE%check_python.py" 2^>nul') do (
        if exist "%%p" set "PY=%%p"
    )
)
exit /b 0
