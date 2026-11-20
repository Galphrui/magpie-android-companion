#!/bin/bash
# No credentials or arbitrary commands are accepted via this entry point.
set -euo pipefail
PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$(uname -s)" == Darwin ]] || { echo 'This entry point needs a Mac host executor.' >&2; exit 1; }
ENV_FILE="$HOME/.config/magpie-android/env.sh"
if [[ -r "$ENV_FILE" ]]; then . "$ENV_FILE"; fi
exec python3 -I "$PROJECT/tools/usb_connect.py" "$@"
