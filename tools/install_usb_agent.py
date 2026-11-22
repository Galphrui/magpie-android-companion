#!/usr/bin/env python3
"""Install the per-user macOS USB supervisor LaunchAgent."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile

LABEL = "com.ruiao.magpie.android-usb"


def document(project, home):
    project = Path(project).resolve()
    home = Path(home).resolve()
    script = project / "tools/usb_start.sh"
    log = home / "Library/Logs/MagpieAndroidUSB.log"
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(script), "--full-browser"],
        "RunAtLoad": True,
        # Retry periodically if ADB, Magpie Browser access, or the phone is
        # temporarily unavailable; launchd skips the tick while the supervisor runs.
        "StartInterval": 60,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def write_agent(project, home):
    home = Path(home).resolve()
    folder = home / "Library/LaunchAgents"
    folder.mkdir(parents=True, exist_ok=True)
    (home / "Library/Logs").mkdir(parents=True, exist_ok=True)
    target = folder / f"{LABEL}.plist"
    fd, temporary = tempfile.mkstemp(prefix=f".{LABEL}-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as output:
            plistlib.dump(document(project, home), output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def bootstrap(path, uid, launchctl="launchctl"):
    domain = f"gui/{uid}"
    subprocess.run([launchctl, "bootout", domain, str(path)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
    subprocess.run([launchctl, "bootstrap", domain, str(path)],
                   stdin=subprocess.DEVNULL, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != "posix" or not Path("/System/Library/LaunchDaemons").exists():
        parser.error("the USB auto-connect agent can only be installed on macOS")
    path = write_agent(args.project, Path.home())
    try:
        bootstrap(path, os.getuid())
    except (OSError, subprocess.SubprocessError):
        parser.error("could not start the USB auto-connect LaunchAgent")
    print(f"Installed USB auto-connect agent: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
