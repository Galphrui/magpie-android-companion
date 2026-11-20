#!/bin/bash
# Mac host only. Does not pretend that a connected workspace is host shell access.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
fail() { printf 'Setup error: %s\n' "$1" >&2; exit 1; }
[[ "$(uname -s)" == Darwin ]] || fail "This requires a command executor running on the Mac, not the Linux workspace."
for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  if [[ -x "$candidate" ]]; then export PATH="$(dirname "$candidate"):$PATH"; break; fi
done
command -v brew >/dev/null || fail "Homebrew is not installed. No unverified remote installer will be executed."
BREW_PREFIX="$(brew --prefix)"

# Match the wrapper daemon toolchain pinned in gradle/gradle-daemon-jvm.properties.
is_java21() {
  [[ -x "$1/bin/java" ]] || return 1
  "$1/bin/java" -XshowSettings:properties -version 2>&1 | grep -E '^[[:space:]]*java.specification.version = 21[[:space:]]*$' >/dev/null
}
SELECTED_JAVA="${JAVA_HOME:-}"
if ! is_java21 "$SELECTED_JAVA"; then
  SELECTED_JAVA="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
fi
if ! is_java21 "$SELECTED_JAVA"; then
  SELECTED_JAVA="$BREW_PREFIX/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home"
  if ! is_java21 "$SELECTED_JAVA"; then brew install openjdk@21; fi
fi
is_java21 "$SELECTED_JAVA" || fail "Java 21 verification failed."
export JAVA_HOME="$SELECTED_JAVA"
export PATH="$JAVA_HOME/bin:$BREW_PREFIX/bin:$PATH"
"$JAVA_HOME/bin/java" -version

if ! command -v python3 >/dev/null; then brew install python; fi
command -v python3 >/dev/null || fail "Python 3 installation failed."
command -v openssl >/dev/null || fail "OpenSSL is required for the local TLS bridge."

SDK_ROOT="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
if [[ -z "$SDK_ROOT" ]]; then
  SDK_ROOT="$(python3 -I - "$PROJECT/local.properties" <<'PY'
from pathlib import Path
import sys
try:
    for line in Path(sys.argv[1]).read_text().splitlines():
        if line.startswith('sdk.dir='):
            print(line[8:].replace('\\ ', ' ').replace('\\:', ':').replace('\\=', '=').replace('\\\\', '\\'))
            break
except FileNotFoundError:
    pass
PY
)"
fi
SDK_ROOT="${SDK_ROOT:-$HOME/Library/Android/sdk}"
SDK_ROOT="$(python3 -I -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$SDK_ROOT")"
SDKMANAGER=""
for candidate in "$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" "$BREW_PREFIX/bin/sdkmanager" "$BREW_PREFIX/share/android-commandlinetools/cmdline-tools/latest/bin/sdkmanager"; do
  if [[ -x "$candidate" ]]; then SDKMANAGER="$candidate"; break; fi
done
if [[ -z "$SDKMANAGER" ]]; then
  printf 'Installing verified Homebrew Android command-line tools...\n'
  brew install --cask android-commandlinetools
  SDKMANAGER="$BREW_PREFIX/share/android-commandlinetools/cmdline-tools/latest/bin/sdkmanager"
fi
[[ -x "$SDKMANAGER" ]] || fail "sdkmanager installation could not be verified."
export ANDROID_HOME="$SDK_ROOT" ANDROID_SDK_ROOT="$SDK_ROOT"
export PATH="$JAVA_HOME/bin:$SDK_ROOT/platform-tools:$SDK_ROOT/build-tools/36.0.0:$(dirname "$SDKMANAGER"):$BREW_PREFIX/bin:$PATH"
printf 'SDK: %s\nsdkmanager: %s\n' "$SDK_ROOT" "$SDKMANAGER"
"$SDKMANAGER" --sdk_root="$SDK_ROOT" --version
# Licensing is Google's SDK agreement, not a file-execution permission prompt.
"$SDKMANAGER" --sdk_root="$SDK_ROOT" --licenses
"$SDKMANAGER" --sdk_root="$SDK_ROOT" 'platform-tools' 'platforms;android-36' 'build-tools;36.0.0'
[[ -x "$SDK_ROOT/platform-tools/adb" && -x "$SDK_ROOT/build-tools/36.0.0/apksigner" && -f "$SDK_ROOT/platforms/android-36/android.jar" ]] || fail "The required SDK packages were not installed."
python3 -I "$PROJECT/tools/configure_mac_env.py" --sdk "$SDK_ROOT" --java "$JAVA_HOME" --manager "$SDKMANAGER" --project "$PROJECT"
command -v adb
adb version
"$SDK_ROOT/build-tools/36.0.0/apksigner" version

# Do not select a simulator or silently select one of several phones.
DEVICES=()
while IFS= read -r serial; do
  [[ -z "$serial" ]] || DEVICES+=("$serial")
done < <(adb devices | awk 'NR > 1 && $2 == "device" && $1 !~ /^emulator-/ {print $1}')
[[ ${#DEVICES[@]} -gt 0 ]] || fail "No authorized Android phone visible to this Mac's ADB."
if [[ ${#DEVICES[@]} -gt 1 && -z "${ANDROID_SERIAL:-}" ]]; then fail "Multiple devices; refusing to guess the target. Set ANDROID_SERIAL."; fi
export ANDROID_SERIAL="${ANDROID_SERIAL:-${DEVICES[0]}}"
[[ " ${DEVICES[*]} " == *" ${ANDROID_SERIAL} "* ]] || fail "Selected Android device is not online."
[[ "$(adb -s "$ANDROID_SERIAL" shell getprop ro.kernel.qemu | tr -d '\r')" != 1 ]] || fail "Target is an emulator, not a physical phone."
printf 'Physical target: %s\n' "$(adb -s "$ANDROID_SERIAL" shell getprop ro.product.model | tr -d '\r')"
if [[ "${MAGPIE_NO_INSTALL:-0}" == 1 ]]; then
  printf 'Environment configured. Build/install skipped explicitly.\n'
  exit 0
fi
# The user authorized build and installation; no redundant yes/no prompt.
exec /bin/bash "$PROJECT/tools/release_install.sh"
