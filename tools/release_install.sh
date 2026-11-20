#!/usr/bin/env bash
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
KEYSTORE="$PROJECT/signing/magpie-galph-release.p12"
ALIAS="magpie-galph-release"

if [[ ! -f "$KEYSTORE" ]]; then
  echo "Keystore missing at $KEYSTORE; obtain it from its protected backup." >&2
  exit 1
fi
if ! command -v adb >/dev/null || ! command -v java >/dev/null || [[ ! -x "$PROJECT/gradlew" ]]; then
  echo "Run tools/setup_mac_android.sh in Terminal on the Mac to configure ADB, Java, and the Android SDK." >&2
  exit 1
fi
if [[ "$(uname -s)" != Darwin ]]; then
  echo "For physical-device installation this script must run on the Mac with the phone connected." >&2
  exit 1
fi
JAVA_VERSION="$(java -version 2>&1 | head -1)"
if [[ "$JAVA_VERSION" != *'version "21.'* && "$JAVA_VERSION" != *'openjdk version "21.'* ]]; then
  echo "This Android Gradle project requires Java 21. Run tools/setup_mac_android.sh first." >&2
  exit 1
fi
# Ensure the current Gradle/Android toolchain matches the configured Mac SDK.
SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/Library/Android/sdk}}"
[[ -d "$SDK_ROOT/platforms/android-36" ]] || { echo "Android API 36 is missing. Run tools/setup_mac_android.sh first." >&2; exit 1; }
[[ -x "$SDK_ROOT/build-tools/36.0.0/apksigner" ]] || { echo "Android build-tools 36.0.0 are missing. Run tools/setup_mac_android.sh first." >&2; exit 1; }
export PATH="$SDK_ROOT/platform-tools:$SDK_ROOT/build-tools/36.0.0:$PATH"
export MAGPIE_SIGNING_PASSWORD
if [[ -z "${MAGPIE_SIGNING_PASSWORD:-}" ]]; then
  [[ -r /dev/tty ]] || { echo "Signing password must be entered in an interactive Mac terminal; refusing to read it from a pipe." >&2; exit 1; }
  read -r -s -p "Signing keystore password: " MAGPIE_SIGNING_PASSWORD </dev/tty
  echo
  export MAGPIE_SIGNING_PASSWORD
fi

printf 'Connected Android devices:\n'
adb devices -l
DEVICES=()
while IFS= read -r SERIAL_LINE; do
  DEVICE_SERIAL="$(printf '%s\n' "$SERIAL_LINE" | awk '$2 == "device" {print $1}')"
  [[ -n "$DEVICE_SERIAL" && "$DEVICE_SERIAL" != emulator-* ]] && DEVICES+=("$DEVICE_SERIAL")
done < <(adb devices | awk 'NR > 1 {print $1, $2}')
if [[ ${#DEVICES[@]} -eq 0 ]]; then
  echo "No authorized Android device is visible to Mac ADB." >&2
  exit 1
fi
if [[ ${#DEVICES[@]} -gt 1 && -z "${ANDROID_SERIAL:-}" ]]; then
  echo "Multiple devices found. Set ANDROID_SERIAL to the intended serial and rerun." >&2
  exit 1
fi
SERIAL="${ANDROID_SERIAL:-${DEVICES[0]}}"
if [[ " ${DEVICES[*]} " != *" ${SERIAL} "* ]]; then
  echo "Selected serial is not an authorized online physical Android device." >&2
  exit 1
fi
MODEL="$(adb -s "$SERIAL" shell getprop ro.product.model | tr -d '\r')"
QEMU="$(adb -s "$SERIAL" shell getprop ro.kernel.qemu | tr -d '\r')"
if [[ -z "$MODEL" || "$QEMU" == "1" ]]; then echo "Target does not appear to be a physical phone." >&2; exit 1; fi
printf 'Target phone: %s (%s)\n' "$MODEL" "$SERIAL"
printf 'Building signed release for %s ...\n' "$SERIAL"
cd "$PROJECT"
./gradlew --no-daemon --stacktrace :app:assembleRelease
APK="$PROJECT/app/build/outputs/apk/release/app-release.apk"
if [[ ! -f "$APK" ]]; then echo "Gradle finished without the expected release APK." >&2; exit 1; fi
command -v apksigner >/dev/null || { echo "apksigner from Android SDK build-tools is required for verification." >&2; exit 1; }
apksigner verify --verbose --print-certs "$APK"
EXPECTED="$(keytool -exportcert -rfc -alias "$ALIAS" -keystore "$KEYSTORE" -storetype PKCS12 -storepass:env MAGPIE_SIGNING_PASSWORD 2>/dev/null | openssl x509 -outform DER | openssl dgst -sha256 | awk '{print $2}')"
ACTUAL="$(apksigner verify --print-certs "$APK" | awk -F': ' '/Signer #1 certificate SHA-256 digest/ {print $2; exit}')"
if [[ "$(printf '%s' "$ACTUAL" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "$EXPECTED" | tr '[:upper:]' '[:lower:]')" ]]; then
  echo "APK certificate does not match the project release key." >&2
  exit 1
fi
adb -s "$SERIAL" install -r "$APK"
PKG="$(sed -n 's/.*applicationId = "\([^"]*\)".*/\1/p' "$PROJECT/app/build.gradle.kts" | head -1)"
COMPONENT="$PKG/com.ruiao.magpie.MainActivity"
START="$(adb -s "$SERIAL" shell am start -W -n "$COMPONENT")"
printf '%s\n' "$START"
if ! grep -Eq '^Status: ok[[:space:]]*$' <<< "$START" || ! adb -s "$SERIAL" shell pidof "$PKG" >/dev/null; then
  echo "APK installation succeeded, but app launch could not be verified." >&2
  exit 1
fi
printf '\nRelease APK installed and launched: %s\nDevice: %s\nCertificate SHA-256: %s\n' "$APK" "$SERIAL" "$ACTUAL"
