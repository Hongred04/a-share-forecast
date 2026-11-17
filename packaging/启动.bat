@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
set "PORT=8848"
set "MODS=fastapi,uvicorn,pandas,numpy,requests"
title A-share forecast

rem ===========================================================================
rem  1.  already running?  just open the browser
rem ===========================================================================
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo   Already running  -  http://127.0.0.1:%PORT%/
  start "" http://127.0.0.1:%PORT%/
  exit /b 0
)

if not exist "run.py" (
  echo.
  echo   [!] run.py not found. Keep this file inside the app folder.
  pause
  exit /b 1
)

rem ===========================================================================
rem  2.  find a python that already has the packages
rem      order: ASHARE_PY -> python.txt -> bundled runtime\python
rem             -> WorkBuddy vendor -> python on PATH -> py -3
rem ===========================================================================
set "PYEXE="
set "BARE="
set "SRC="

if defined ASHARE_PY call :probe "%ASHARE_PY%" "ASHARE_PY"
if not defined PYEXE if exist "python.txt" for /f "usebackq delims=" %%L in ("python.txt") do call :probe "%%~L" "python.txt"

rem --- the runtime shipped with this folder: no PATH, no install needed ---
call :probe "%~dp0runtime\python\python.exe" "bundled"

rem --- developer convenience: another python already on this machine ---
set "VENDOR=%USERPROFILE%\.workbuddy\binaries\python\versions"
if not defined PYEXE if exist "%VENDOR%" for /f "delims=" %%D in ('dir /b /ad /o-n "%VENDOR%" 2^>nul') do call :probe "%VENDOR%\%%D\python.exe" "vendor"

if not defined PYEXE call :probe "python" "PATH"
if not defined PYEXE call :probe "py -3" "py -3"

rem ---- still nothing: remember any python that at least starts, to offer install ----
if not defined PYEXE call :runs "python"
if not defined PYEXE call :runs "py -3"

if not defined PYEXE (
  if not defined BARE goto :nopy

  echo.
  echo   Python found:      !BARE!
  echo   Packages missing:  %MODS%
  echo.
  set "ANS="
  set /p "ANS=  Install them now? [Y/n] "
  if /i "!ANS!"=="n" goto :nopy

  echo.
  echo   Installing ... 1-3 minutes, needs internet.
  echo.
  !BARE! -m pip install --upgrade pip
  !BARE! -m pip install -r requirements.txt
  !BARE! -c "import %MODS%" >nul 2>nul
  if errorlevel 1 (
    echo.
    echo   [!] Install failed. Run this line in this folder by hand:
    echo       !BARE! -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
  set "PYEXE=!BARE!"
  set "SRC=installed"
)

echo.
echo   A-share forecast
echo   Python : %SRC%
if /i not "%SRC%"=="bundled" echo   Path   : %PYEXE%
echo   URL    : http://127.0.0.1:%PORT%/
echo.
echo   Keep this window open. Close it to stop the server.
echo.
start "" http://127.0.0.1:%PORT%/
%PYEXE% run.py serve
echo.
echo   Server stopped.
pause
exit /b 0

rem ===========================================================================
:nopy
echo.
if not exist "runtime\python\python.exe" (
  echo   [!] runtime\python\python.exe is missing.
  echo.
  echo   This folder is incomplete - the bundled Python runtime was not copied.
  echo   Re-copy the whole app folder, or install Python 3.12 from
  echo   https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
  echo   and run this file again.
) else (
  echo   [!] The bundled Python cannot import: %MODS%
  echo.
  echo   runtime\ was probably copied incompletely - it has no pip of its own.
  echo   Re-copy this whole folder, or install Python 3.12 from
  echo   https://www.python.org/downloads/ and run this file again.
)
echo.
pause
exit /b 1

rem ===========================================================================
rem  :probe <candidate> <label>   set PYEXE if the candidate can import everything
rem ===========================================================================
:probe
set "CAND=%~1"
set "SRCLBL=%~2"
if "%CAND%"=="" exit /b 1
if exist "%CAND%" (
  "%CAND%" -c "import %MODS%" >nul 2>nul
) else (
  %CAND% -c "import %MODS%" >nul 2>nul
)
if errorlevel 1 exit /b 1
set "PYEXE=%CAND%"
set "SRC=%SRCLBL%"
exit /b 0

rem ===========================================================================
rem  :runs <candidate>    remember the first candidate that can at least start
rem ===========================================================================
:runs
if defined BARE exit /b 1
set "CAND=%~1"
if "%CAND%"=="" exit /b 1
if exist "%CAND%" (
  "%CAND%" -c "import sys" >nul 2>nul
) else (
  %CAND% -c "import sys" >nul 2>nul
)
if errorlevel 1 exit /b 1
set "BARE=%CAND%"
exit /b 0
