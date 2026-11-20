"""Isolated host-tool checks. These tests are NOT Android device acceptance."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


env = load('mac_env', ROOT / 'tools/configure_mac_env.py')
usb = load('usb_connect', ROOT / 'tools/usb_connect.py')
bridge = load('magpie_bridge', ROOT / 'host/bridge.py')


class HostToolsTests(unittest.TestCase):
    def test_profile_preserves_other_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            project = home / 'project'
            project.mkdir()
            java = home / 'JDK 17'
            sdk = home / 'Android SDK'
            manager = home / 'brew/cmdline-tools/latest/bin/sdkmanager'
            original = 'export KEEP_MY_SETTING=yes\n# Android SDK for Magpie Companion\nexport ANDROID_HOME=old\nexport ANDROID_SDK_ROOT=old\nexport PATH=old\nexport UNRELATED=keep\n'
            (home / '.zprofile').write_text(original)
            (project / 'local.properties').write_text('other.value=42\nsdk.dir=old\n')
            env.configure(home, sdk, java, manager, project)
            first = (home / '.zprofile').read_text()
            env.configure(home, sdk, java, manager, project)
            self.assertEqual((home / '.zprofile').read_text(), first)
            self.assertIn('export UNRELATED=keep', first)
            self.assertIn('export KEEP_MY_SETTING=yes', first)
            self.assertEqual((home / '.zprofile.pre-magpie-android.bak').read_text(), original)
            self.assertEqual(first.count(env.START), 1)
            self.assertIn('other.value=42', (project / 'local.properties').read_text())
            source = home / '.config/magpie-android/env.sh'
            output = subprocess.check_output(['/bin/bash', '-c', '. "$1"; printf "%s\\n%s\\n" "$JAVA_HOME" "$ANDROID_HOME"', 'check', str(source)], text=True)
            self.assertEqual(output.splitlines(), [str(java), str(sdk)])
            self.assertEqual(source.stat().st_mode & 0o777, 0o600)

    def test_unclosed_profile_block_is_not_silently_overwritten(self):
        with self.assertRaises(ValueError):
            env.merge_profile('hello\n' + env.START + '\nkeep me', 'replacement')

    def test_usb_handoff_rejects_network_origins_and_invalid_tokens(self):
        valid = dict(version=1, transport='usb', origin=usb.ORIGIN,
                     pairing='a' * 64 + ':' + 'B' * 43, fingerprint='a' * 64)
        self.assertEqual(usb.parse_handoff(json.dumps(valid).encode()), valid['pairing'])
        for key, value in [('origin', 'https://192.168.1.1:43425'), ('transport', 'lan'), ('pairing', 'bad'), ('fingerprint', 'c' * 64)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                usb.parse_handoff(json.dumps({**valid, key: value}).encode())

    def test_local_key_reader_keeps_original_string_contract(self):
        import io
        with patch.object(bridge.sys, 'stdin', io.StringIO('safe-local-browser-key\n')):
            self.assertEqual(bridge.read_key(True), 'safe-local-browser-key')

    def test_auto_key_reads_zero_default_port_without_modifying_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'web-page.json'
            original = json.dumps(dict(mode='local', port=0, key='safe-local-browser-key'))
            path.write_text(original)
            with patch.object(bridge, 'browser_config_candidates', return_value=(path,)):
                self.assertEqual(bridge.read_auto_key(), ('safe-local-browser-key', 3430))
            self.assertEqual(path.read_text(), original)

    def test_auto_key_handles_corrupt_and_disabled_configs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'web-page.json'
            for value in ([], None, {'mode': '', 'key': 'safe-local-browser-key'},
                          {'mode': 'local', 'key': 'safe-local-browser-key', 'port': True}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), patch.object(bridge, 'browser_config_candidates', return_value=(path,)), self.assertRaises(ValueError):
                    bridge.read_auto_key()

    def test_usb_pair_provider_failure_never_launches_app(self):
        calls = []
        def fake(argv, **kwargs):
            calls.append((argv, kwargs))
            if 'reverse' in argv:
                return ''
            if kwargs.get('data'):
                return 'Error while accessing provider: SecurityException'
            self.fail('Unexpected host command')
        with patch.object(usb, 'verify_installed'), patch.object(usb, 'execute', side_effect=fake):
            with self.assertRaises(RuntimeError):
                usb.connect('/adb', '/signer', 'phone', 'a' * 64 + ':' + 'B' * 43, '/tmp')
        self.assertTrue(any('--remove' in argv for argv, _ in calls))
        self.assertFalse(any('start' in argv for argv, _ in calls))
        self.assertFalse(any('B' * 43 in ' '.join(argv) for argv, _ in calls))

    def test_cli_uses_a_string_key_and_publishes_no_token_to_stdout(self):
        import io
        adapter_keys = []
        class FakeServer:
            def __init__(self, port, tls, token, adapter):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def serve_forever(self):
                raise KeyboardInterrupt()
        def adapter(port, gateway_port, key, token, cli):
            adapter_keys.append(key)
            return object()
        with tempfile.TemporaryDirectory() as folder, patch.object(bridge, 'BridgeServer', FakeServer), \
                patch.object(bridge, 'Magpie', side_effect=adapter), \
                patch.object(bridge, 'make_tls', return_value=(object(), 'a' * 64)), \
                patch.object(bridge.sys, 'stdin', io.StringIO('safe-local-browser-key\n')), \
                patch.object(bridge.sys, 'stdout', io.StringIO()) as stdout, \
                patch.object(bridge.sys, 'stderr', io.StringIO()):
            path = Path(folder) / 'pair.json'
            self.assertEqual(bridge.main(['--browser-key-stdin', '--pairing-file', str(path)]), 0)
            self.assertEqual(adapter_keys, ['safe-local-browser-key'])
            self.assertEqual(stdout.getvalue(), '')
            usb.parse_handoff(path.read_bytes())

    def test_shell_entrypoints_parse(self):
        for path in (ROOT / 'tools').glob('*.sh'):
            subprocess.run(['/bin/bash', '-n', str(path)], check=True)


if __name__ == '__main__':
    unittest.main()
