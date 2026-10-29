@echo off
setlocal EnableExtensions

rem Paper2Lean launcher for the Lean_Work workspace.
set "WORK_ROOT=%~dp0.."
for %%I in ("%WORK_ROOT%") do set "WORK_ROOT=%%~fI"
set "PAPER2LEAN_SRC=%WORK_ROOT%\paper2lean\src"
set "PYTHON_EXE=%WORK_ROOT%\.tools\python-runtime\python.exe"
set "LEAN_PROJECT=%WORK_ROOT%\mathlib_project"
set "PAPER2LEAN_PIPELINE=paper2lean"
set "MEMORY_DIR=%WORK_ROOT%\paper2lean\memory"
set "PAPER_OUTPUT=%WORK_ROOT%\paper_runs"
set "PAPER2LEAN_PORT=8787"

if not exist "%PYTHON_EXE%" (
  echo Bundled Python runtime was not found:
  echo   %PYTHON_EXE%
  pause
  exit /b 1
)

if not exist "%WORK_ROOT%\paper2lean\run.py" (
  echo Paper2Lean launcher was not found:
  echo   %WORK_ROOT%\paper2lean\run.py
  pause
  exit /b 1
)

rem Load the project's local environment file when present.
if exist "%WORK_ROOT%\paper2lean\local_config.env" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%WORK_ROOT%\paper2lean\local_config.env") do (
    rem Accept normal KEY=VALUE lines. Legacy PowerShell $env: lines are
    rem intentionally ignored because this batch file supplies safe defaults.
    if not "%%A"=="" if not "%%A:~0,1%%"=="#" if not "%%A:~0,1%%"=="$" set "%%A=%%B"
  )
)

rem These values are the intended defaults for the local OpenAI-compatible endpoint.
if not defined LLM_BACKEND set "LLM_BACKEND=http"
if not defined LLM_HTTP_URL set "LLM_HTTP_URL=http://127.0.0.1:15721/v1"
if not defined LLM_HTTP_MODEL set "LLM_HTTP_MODEL=gpt-6.1-sol"

set "PYTHONPATH=%PAPER2LEAN_SRC%;%PYTHONPATH%"

echo ========================================
echo Paper2Lean Web UI
echo ========================================
echo Lean project: %LEAN_PROJECT%
echo LLM backend:  %LLM_BACKEND%
echo LLM endpoint: %LLM_HTTP_URL%
echo LLM model:    %LLM_HTTP_MODEL%
echo.
echo Starting independent Paper2Lean at http://127.0.0.1:8787 ...
echo Close this window to stop the server.
echo.

pushd "%WORK_ROOT%\paper2lean"
"%PYTHON_EXE%" run.py web
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
  echo.
  echo Paper2Lean stopped with exit code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
