@echo off
REM Launch the find-and-fix bot using the repo-local virtualenv.
REM Any extra arguments are passed straight through, e.g.:
REM   run.bat --once
REM   run.bat --work SPThreadContext
REM   run.bat --config some-other.json
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [run.bat] .venv not found. Create it first:
  echo     py -3.12 -m venv .venv
  echo     .venv\Scripts\python -m pip install -e ".[treesitter]"
  exit /b 1
)

REM Prefer a local SPO config, then the default, else built-in defaults.
set "CONFIG="
if exist "findfix.config.spo.json" set "CONFIG=findfix.config.spo.json"
if not defined CONFIG if exist "findfix.config.json" set "CONFIG=findfix.config.json"

if defined CONFIG (
  echo [run.bat] using config: %CONFIG%
  "%PY%" -m findfix --config "%CONFIG%" %*
) else (
  echo [run.bat] no config file found; using built-in defaults
  "%PY%" -m findfix %*
)

endlocal
