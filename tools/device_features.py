#!/usr/bin/env python3
"""Exercise the running Mac bridge and the signed app on a physical USB phone."""
import argparse
import os
import re
import subprocess
import xml.etree.ElementTree as ET

from usb_connect import COMPONENT, PKG, usb_devices

UI_FILE = "/data/local/tmp/magpie-feature-check.xml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    selected = os.environ.get("ANDROID_SERIAL")
    phones = [phone for phone in usb_devices("adb") if not selected or phone == selected]
    if len(phones) != 1:
        parser.error("select exactly one authorized USB phone")
    adb = ["adb", "-s", phones[0]]

    def shell(*args):
        return subprocess.check_output(adb + ["shell", *args], text=True, timeout=25)

    def read_ui():
        shell("uiautomator", "dump", UI_FILE)
        root = ET.fromstring(shell("cat", UI_FILE))
        return [n for n in root.iter("node") if n.get("package") == PKG]

    def expect(description, match):
        for _ in range(4):
            nodes = read_ui()
            for node in nodes:
                if match(node):
                    return nodes, node
        raise AssertionError(description)

    def text(value):
        return lambda node: node.get("text") == value

    def tap(node):
        left, top, right, bottom = map(int, re.findall(r"\d+", node.get("bounds")))
        shell("input", "tap", str((left + right) // 2), str((top + bottom) // 2))

    def navigate(nodes, page):
        tap(next(node for node in nodes if node.get("content-desc") == page))

    failures = 0
    try:
        for round_number in range(1, args.rounds + 1):
            try:
                shell("am", "force-stop", PKG)
                start = shell("am", "start", "-W", "-n", COMPONENT)
                assert re.search(r"^Status: ok\s*$", start, re.M), "cold launch failed"
                nodes, _ = expect("USB never became live", text("USB 已连接"))
                nodes, _ = expect("desktop agent rows missing", text("Claude Code"))
                navigate(nodes, "网关")
                nodes, _ = expect("live gateway status missing", text("可用"))
                assert any(text("进行中的请求")(node) for node in nodes), "gateway count missing"
                navigate(nodes, "路由")
                nodes, _ = expect("routing page missing", text("路由"))
                assert any(
                    any(status in node.get("text", "") for status in
                        ("路由摘要", "路由记录", "成功", "失败", "进行中")) for node in nodes
                ), "routing results or empty state missing"
                navigate(nodes, "设置")
                nodes, _ = expect("currency write control missing", text("更改电脑用量货币"))
                assert any(text("电脑用量货币")(node) for node in nodes), "settings data missing"
                assert any("屏幕方向 · " in node.get("text", "") for node in nodes), "orientation control missing"
                shell("input", "keyevent", "KEYCODE_HOME")
                shell("am", "start", "-W", "-n", COMPONENT)
                expect("USB did not recover after resume", text("USB 已连接"))
                print(f"{round_number:02d}/{args.rounds:02d} PASS cold launch, USB data, four pages, resume", flush=True)
            except (AssertionError, StopIteration, subprocess.SubprocessError, ET.ParseError) as error:
                failures += 1
                print(f"{round_number:02d}/{args.rounds:02d} FAIL {error}", flush=True)
    finally:
        shell("rm", "-f", UI_FILE)
    print(f"{args.rounds - failures}/{args.rounds} passed", flush=True)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
