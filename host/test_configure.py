import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import configure


class BluetoothConfigurationTests(unittest.TestCase):
    def test_custom_config_and_gateway_port(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "web-page.json"
            gateway = config.with_name("gateway.json")
            config.write_text(json.dumps({"mode": "local", "key": "demo-key-1234567890", "port": 3438}),
                              encoding="utf-8")
            gateway.write_text(json.dumps({"addr": "[::]:3439"}), encoding="utf-8")
            self.assertEqual(configure.find_browser_config(str(config)), (config, "demo-key-1234567890", 3438))
            self.assertEqual(configure.get_gateway_port(config.parent), 3439)
            gateway.write_text(json.dumps({"addr": "127.0.0.1:99999"}), encoding="utf-8")
            self.assertEqual(configure.get_gateway_port(config.parent), 3425)

    def test_configuration_window_only_shows_qr_and_status_and_starts_automatically(self):
        class Widget:
            def __init__(self, **options):
                self.options = options

            def pack(self, **_options):
                pass

            def bind(self, *_args):
                pass

        class Root:
            def __init__(self):
                self.callbacks = {}

            def title(self, _title):
                pass

            def minsize(self, *_size):
                pass

            def protocol(self, *_args):
                pass

            def after(self, delay, callback):
                self.callbacks[delay] = callback

            def after_idle(self, callback):
                self.callbacks["idle"] = callback

            def destroy(self):
                pass

        class StringVar:
            def __init__(self, value):
                self.value = value

            def set(self, value):
                self.value = value

        class Tk:
            BOTH = "both"
            W = "w"
            Canvas = lambda *_args, **options: Widget(**options)

        Tk.StringVar = StringVar

        class Ttk:
            Frame = lambda *_args, **options: Widget(**options)
            Label = lambda *_args, **options: Widget(**options)

        class QRCode:
            pass

        root = Root()
        with patch.object(configure.ConfigWindow, "start") as start:
            window = configure.ConfigWindow(root, Tk, Ttk, QRCode)

        self.assertEqual(set(root.callbacks), {80, "idle"})
        root.callbacks["idle"]()
        start.assert_called_once_with()

        window.worker_generation = 2
        configure._put_event(window.events, 1, "finished", None)
        window.poll()
        self.assertNotIn(3000, root.callbacks)

    def test_stop_cancels_the_running_session_and_waits_for_cleanup(self):
        stop = threading.Event()
        finished = []

        class Session:
            async def run(self):
                try:
                    stop.set()
                    await asyncio.Event().wait()
                finally:
                    finished.append(True)

        asyncio.run(configure.run_session(Session(), stop))
        self.assertEqual(finished, [True])
