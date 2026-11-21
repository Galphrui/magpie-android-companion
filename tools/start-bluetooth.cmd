@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PROJECT=%~dp0.."
set "REQUIREMENTS=%PROJECT%\host\requirements-bluetooth.txt"
set "VENV=%LOCALAPPDATA%\Magpie Android Companion\ble-venv"
set "VENV_PYTHON=%VENV%\Scripts\python.exe"
set "MARKER=%VENV%\magpie-requirements.txt"

if exist "%VENV_PYTHON%" (
  "%VENV_PYTHON%" -c "import sys, tkinter; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
  if not errorlevel 1 goto dependencies
)

py -3 -c "import sys, tkinter; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
  py -3 -m venv "%VENV%"
  if errorlevel 1 goto failed
  goto dependencies
)

python -c "import sys, tkinter; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 (
  python -m venv "%VENV%"
  if errorlevel 1 goto failed
  goto dependencies
)

echo 请安装带 Tk 的 Python 3.10 或更新版本，然后重新打开本程序。
goto failed

:dependencies
fc /b "%REQUIREMENTS%" "%MARKER%" >nul 2>&1
if errorlevel 1 (
  "%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%REQUIREMENTS%"
  if errorlevel 1 goto failed
  copy /y "%REQUIREMENTS%" "%MARKER%" >nul
  if errorlevel 1 goto failed
)

"%VENV_PYTHON%" "%PROJECT%\host\configure.py"
if errorlevel 1 goto failed
exit /b 0

:failed
echo Magpie 蓝牙配置未能启动。检查上述信息后重试。
pause
exit /b 1
