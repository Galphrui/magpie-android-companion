#!/usr/bin/env bash
# Lifecycle smoke only: not an end-to-end feature acceptance suite.
set -euo pipefail
[[ "$(uname -s)" == Darwin ]] || { echo "Run on the Mac connected to the phone." >&2; exit 1; }
command -v adb >/dev/null || { echo "ADB missing; run setup_mac_android.sh first." >&2; exit 1; }
DEVICES=()
while read -r serial state rest; do
  if [[ "$state" == device && "$serial" != emulator-* ]]; then DEVICES+=("$serial"); fi
done < <(adb devices -l)
[[ ${#DEVICES[@]} -gt 0 ]] || { echo "No authorized device detected." >&2; exit 1; }
if [[ ${#DEVICES[@]} -gt 1 && -z "${ANDROID_SERIAL:-}" ]]; then
  echo "Set ANDROID_SERIAL to the intended physical phone." >&2; exit 1
fi
SERIAL="${ANDROID_SERIAL:-${DEVICES[0]}}"
[[ " ${DEVICES[*]} " == *" ${SERIAL} "* ]] || { echo "Selected device is not authorized." >&2; exit 1; }
[[ "$(adb -s "$SERIAL" shell getprop ro.kernel.qemu | tr -d '\r')" != 1 ]] || { echo "Emulators are not accepted." >&2; exit 1; }
# Require an actual USB transport, not an arbitrary remote ADB connection.
adb devices -l | awk -v id="$SERIAL" '$1 == id && /usb:/ {found=1} END {exit !found}' || {
  echo "Cannot confirm a USB transport. Verify adb devices -l before accepting a physical-device test." >&2; exit 1;
}
PKG=com.ruiao.magpie.companion.galph
COMPONENT="$PKG/com.ruiao.magpie.MainActivity"
LOG="${TMPDIR:-/tmp}/magpie-android-smoke-$(date +%Y%m%d-%H%M%S).log"
umask 077
: > "$LOG"
fail=0
launch() {
  local output
  output="$(adb -s "$SERIAL" shell am start -W -n "$COMPONENT")" || return 1
  printf '%s\n' "$output" >> "$LOG"
  grep -Eq '^Status: ok[[:space:]]*$' <<< "$output" || return 1
  sleep 2
  PID="$(adb -s "$SERIAL" shell pidof "$PKG" | tr -d '\r')" || return 1
  [[ "$PID" =~ ^[0-9]+$ ]] || return 1
  adb -s "$SERIAL" shell dumpsys activity activities | grep -E 'ResumedActivity|mResumedActivity' | grep -F "$COMPONENT" >> "$LOG"
}
check_crash() {
  local records
  records="$(adb -s "$SERIAL" logcat -b crash --pid="$PID" -d -v brief -t 200)" || return 1
  if grep -Eq 'FATAL EXCEPTION|Fatal signal|ANR in' <<< "$records"; then
    printf '%s\n' "$records" >> "$LOG"; return 1
  fi
}
for round in $(seq 1 20); do
  printf '=== lifecycle cycle %02d/20 ===\n' "$round" | tee -a "$LOG"
  result=PASS
  adb -s "$SERIAL" shell am force-stop "$PKG" >> "$LOG" 2>&1 || result=FAIL
  if launch; then
    check_crash || result=FAIL
    adb -s "$SERIAL" shell input keyevent KEYCODE_HOME >> "$LOG" 2>&1 || result=FAIL
    sleep 1
    if launch; then check_crash || result=FAIL; else result=FAIL; fi
  else result=FAIL; fi
  printf 'cycle %02d: %s\n' "$round" "$result" | tee -a "$LOG"
  [[ "$result" == PASS ]] || fail=1
done
printf 'Lifecycle smoke finished, exit=%s. Log: %s\nFeature acceptance is NOT established by this script.\n' "$fail" "$LOG"
exit "$fail"
