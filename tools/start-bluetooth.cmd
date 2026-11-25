@echo off
rem 双击启动 Windows 版「Magpie 蓝牙配置」窗口。
rem 首次运行会在用户目录创建 Python 环境并安装依赖；之后直接打开窗口。
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PROJECT=%~dp0.."
set "REQUIREMENTS=%PROJECT%\host\requirements-bluetooth.txt"
set "VENV=%LOCALAPPDATA%\Magpie Android Companion\ble-venv"
set "VENV_PYTHON=%VENV%\Scripts\python.exe"
set "MARKER=%VENV%\magpie-requirements.txt"

if not exist "%REQUIREMENTS%" (
  echo 缺少文件：%REQUIREMENTS%
  goto failed
)

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

echo 请先安装带 Tk 的 Python 3.10 或更新版本（python.org 的 Windows 安装包，
echo 安装时勾选 tcl/tk 和 Add python to PATH），然后重新打开本程序。
goto failed

:dependencies
fc /b "%REQUIREMENTS%" "%MARKER%" >nul 2>&1
if errorlevel 1 (
  "%VENV_PYTHON%" -m pip install --disable-pip-version-check -r "%REQUIREMENTS%"
  if errorlevel 1 (
    echo 依赖下载失败。请检查网络后重试。
    goto failed
  )
  copy /y "%REQUIREMENTS%" "%MARKER%" >nul
  if errorlevel 1 goto failed
)

"%VENV_PYTHON%" "%PROJECT%\host\configure.py"
if errorlevel 1 goto failed
exit /b 0

:failed
echo.
echo Magpie 蓝牙配置未能启动。请截图以上信息后重试。
pause
exit /b 1
