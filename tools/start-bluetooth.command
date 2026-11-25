#!/bin/bash
# 双击启动 Mac 版「Magpie 蓝牙配置」窗口。
# 首次运行会在用户目录创建 Python 环境并安装依赖；之后直接打开窗口。
set -euo pipefail

project="$(cd "$(dirname "$0")/.." && pwd)"
requirements="$project/host/requirements-bluetooth.txt"
venv="${HOME:?}/Library/Application Support/Magpie Android Companion/ble-venv"

pause_on_error() {
  printf '\n启动失败：%s\n' "$1" >&2
  printf '请截图以上信息。按回车键关闭此窗口…' >&2
  read -r _ || true
  exit 1
}

suitable_python() {
  "$1" -c 'import sys, tkinter; from pathlib import Path; binary = Path(sys.base_prefix, "Resources/Python.app/Contents/MacOS/Python"); sys.exit(sys.version_info < (3, 10) or not binary.is_file())' >/dev/null 2>&1
}

if ! suitable_python "$venv/bin/python"; then
  python=python3
  if ! suitable_python "$python"; then
    python=/Library/Frameworks/Python.framework/Versions/Current/bin/python3
  fi
  if ! suitable_python "$python"; then
    pause_on_error '请先安装带 Tk 的 Python 3.10 或更新版本（python.org 的 macOS 安装包），然后重新打开本程序。'
  fi
  "$python" -m venv "$venv" || pause_on_error '无法创建 Python 环境。'
fi

marker="$venv/magpie-requirements.txt"
if ! cmp -s "$requirements" "$marker"; then
  "$venv/bin/python" -m pip install --disable-pip-version-check -r "$requirements" \
    || pause_on_error '依赖下载失败。请检查网络后重试。'
  cp "$requirements" "$marker"
fi

app="$(dirname "$venv")/Magpie Bluetooth.app"
# macOS 把蓝牙权限归到原生 App 包上；用 shell 包装直接跑 Python 会导致系统授权归属错误。
"$venv/bin/python" - "$app" <<'PY' || pause_on_error '无法生成 Magpie Bluetooth.app。'
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig

app = Path(sys.argv[1])
binary = app / "Contents/MacOS/MagpieBluetooth"
plist = app / "Contents/Info.plist"
environment = {"PYTHONHOME": sys.base_prefix, "PYTHONPATH": sysconfig.get_path("purelib")}
for variable in ("MAGPIE_CONFIG_DIR", "XDG_CONFIG_HOME"):
    if os.environ.get(variable):
        environment[variable] = os.environ[variable]
metadata = plistlib.dumps({
    "CFBundleIdentifier": "com.ruiao.magpie.companion.bluetooth",
    "CFBundleName": "Magpie Bluetooth",
    "CFBundleDisplayName": "Magpie 蓝牙配置",
    "CFBundlePackageType": "APPL",
    "CFBundleExecutable": binary.name,
    "CFBundleVersion": sys.version.split()[0],
    "NSBluetoothAlwaysUsageDescription": "通过低功耗蓝牙连接您配对的 Magpie Android 伴侣端。",
    "LSEnvironment": environment,
})
if (not binary.is_file() or not plist.is_file() or plist.read_bytes() != metadata
        or subprocess.run(["/usr/bin/codesign", "--verify", str(app)], capture_output=True).returncode):
    binary.parent.mkdir(parents=True, exist_ok=True)
    source = Path(sys.base_prefix, "Resources/Python.app/Contents/MacOS/Python")
    temporary = binary.with_suffix(".new")
    shutil.copy2(source, temporary)
    temporary.replace(binary)
    plist.write_bytes(metadata)
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], check=True)
PY

exec /usr/bin/open -a "$app" --args -B -s "$project/host/configure.py"
