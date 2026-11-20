#!/usr/bin/env python3
"""Check the signed full Magpie view on one authorized USB phone."""
import argparse
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from usb_connect import COMPONENT, PKG, usb_devices

UI_PATH = "/data/local/tmp/magpie-browser-test.xml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    selected = os.environ.get("ANDROID_SERIAL")
    devices = [serial for serial in usb_devices("adb") if not selected or serial == selected]
    if args.rounds < 1 or len(devices) != 1:
        parser.error("select one authorized USB phone and a positive round count")

    def shell(*args):
        return subprocess.check_output(["adb", "-s", devices[0], "shell", *args], timeout=18, text=True)

    def find(label, classes):
        for _ in range(4):
            shell("rm", "-f", UI_PATH)
            if "dumped to" not in shell("uiautomator", "dump", UI_PATH):
                continue
            for node in ET.fromstring(shell("cat", UI_PATH)).iter("node"):
                if (node.get("package") == PKG and node.get("class").endswith(classes)
                        and (node.get("text") or node.get("content-desc")) == label):
                    bounds = [int(value) for value in re.findall(r"\d+", node.get("bounds"))]
                    if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
                        return bounds
        raise AssertionError(f"expected visible control: {label}")

    def tap(label, classes):
        left, top, right, bottom = find(label, classes)
        # Keep taps on the browser's bottom tabs clear of Android's gesture edge.
        y = top + (bottom - top) // (4 if label == "更多" else 2)
        shell("input", "tap", str((left + right) // 2), str(y))
        time.sleep(0.3)

    failures = 0
    try:
        for round_number in range(1, args.rounds + 1):
            try:
                shell("am", "force-stop", PKG)
                launch = shell("am", "start", "-W", "-n", COMPONENT)
                if not re.search(r"^Status: ok\s*$", launch, re.M):
                    raise AssertionError("app launch failed")
                find("magpie", "WebView")
                find("供应商", "Button")
                print(f"{round_number:02}/{args.rounds:02} PASS signed app opens Mac Web UI", flush=True)
            except (AssertionError, subprocess.SubprocessError, ET.ParseError) as exc:
                failures += 1
                print(f"{round_number:02}/{args.rounds:02} FAIL {exc}", flush=True)
        tap("更多", "Button")
        tap("用量", ("Button", "MenuItem"))
        find("立即重新读取用量", "Button")
        find("概览", "Button")
        find("请求", "Button")
        print("PASS real Mac Usage view and request tab", flush=True)
    finally:
        shell("rm", "-f", UI_PATH)
    print(f"Cold launches passed: {args.rounds - failures}/{args.rounds}", flush=True)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
