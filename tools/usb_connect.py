#!/usr/bin/env python3
"""Mac USB supervisor: existing ADB authorization replaces manual phone pairing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parent.parent
PKG = 'com.ruiao.magpie.companion.galph'
COMPONENT = PKG + '/com.ruiao.magpie.MainActivity'
ORIGIN = 'https://127.0.0.1:43425'
SUMMARY_PORT, BROWSER_PORT = 'tcp:43425', 'tcp:43427'


def execute(argv, *, data=None, timeout=15):
    result = subprocess.run(argv, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout, check=False)
    if result.returncode:
        # ADB output could include a secret. Do not log raw commands or stderr.
        raise RuntimeError('A required local Android command failed (output suppressed).')
    return result.stdout.replace('\r', '')


def usb_devices(adb):
    result = []
    for line in execute([adb, 'devices', '-l']).splitlines()[1:]:
        fields = line.split()
        if len(fields) < 2 or fields[1] != 'device' or fields[0].startswith('emulator-'):
            continue
        serial = fields[0]
        # libusb ADB sometimes omits usb: in `devices -l`; get-devpath is the second check.
        devpath = execute([adb, '-s', serial, 'get-devpath']).strip()
        if not (any(field.startswith('usb:') for field in fields[2:]) or devpath.startswith('usb:')):
            continue
        if execute([adb, '-s', serial, 'shell', 'getprop', 'ro.kernel.qemu']).strip() != '1':
            result.append(serial)
    return result


def parse_handoff(raw):
    if len(raw) > 2048:
        raise ValueError('Invalid USB handoff size')
    pair = json.loads(raw)
    if (not isinstance(pair, dict) or pair.get('version') != 1 or pair.get('transport') != 'usb'
            or pair.get('origin') != ORIGIN or not isinstance(pair.get('pairing'), str)
            or not re.fullmatch(r'[0-9a-f]{64}:[A-Za-z0-9_-]{43}', pair['pairing'])
            or pair.get('fingerprint') != pair['pairing'][:64]):
        raise ValueError('Invalid USB handoff')
    return pair['pairing']


def expected_certificate():
    pem = (PROJECT / 'signing/magpie-galph-release-cert.pem').read_text()
    import ssl
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def verify_installed(adb, signer, serial, temp_dir):
    base = [adb, '-s', serial]
    output = execute(base + ['shell', 'pm', 'path', PKG])
    packages = [line[8:] for line in output.splitlines() if line.startswith('package:')]
    installed = next((path for path in packages if path.endswith('/base.apk')), None)
    if installed is None or not installed.startswith('/data/app/') or not re.fullmatch(r'[A-Za-z0-9/_.=+~-]+', installed):
        raise RuntimeError('The signed companion package was not found on the phone.')
    local_apk = Path(temp_dir) / 'installed-base.apk'
    execute(base + ['pull', installed, str(local_apk)], timeout=60)
    output = execute([signer, 'verify', '--print-certs', str(local_apk)], timeout=30)
    digests = re.findall(r'^Signer #\d+ certificate SHA-256 digest: ([0-9a-fA-F]{64})$', output, re.M)
    if len(digests) != 1 or digests[0].lower() != expected_certificate():
        raise RuntimeError('Installed companion signature does not match the dedicated project key.')
    local_apk.unlink()


def mapped_ports(adb, serial, ports):
    mappings = execute([adb, '-s', serial, 'reverse', '--list'])
    mapped = set()
    for line in mappings.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[-2] not in ports:
            continue
        if fields[-1] != fields[-2]:
            raise RuntimeError('A companion USB port is already mapped to a different service.')
        mapped.add(fields[-2])
    return mapped


def remove_mappings(adb, serial, ports):
    for port in ports:
        try:
            execute([adb, '-s', serial, 'reverse', '--remove', port])
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass


def connect(adb, signer, serial, credential, temp_dir, ports=(SUMMARY_PORT,)):
    verify_installed(adb, signer, serial, temp_dir)
    base = [adb, '-s', serial]
    existing = mapped_ports(adb, serial, ports)
    owned = set()
    try:
        for port in ports:
            if port not in existing:
                execute(base + ['reverse', '--no-rebind', port, port])
                owned.add(port)
        # Token is sent over the ADB shell's stdin, NOT in host argv, intent extras, logs or shared storage.
        command = 'content call --user 0 --uri content://' + PKG + '.usb --method pair --arg ' + credential + '\n'
        response = execute(base + ['shell'], data=command)
        if not re.search(r'\bresult=paired\b', response):
            raise RuntimeError('Android did not acknowledge the USB credential import.')
        response = execute(base + ['shell', 'am', 'start', '-W', '--activity-clear-top',
                                   '-n', COMPONENT, '-a', PKG + '.USB_PAIR_UPDATED'])
        if not re.search(r'^Status: ok\s*$', response, re.M):
            raise RuntimeError('The installed app did not start successfully.')
        print('Signed companion identified and launched via USB; no manual phone pairing required.', flush=True)
        return owned
    except BaseException:
        remove_mappings(adb, serial, owned)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-write', action='store_true')
    parser.add_argument('--full-browser', action='store_true', help='enable original Magpie UI and management on USB')
    parser.add_argument('--magpie-bin', default='magpie')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        print('A Mac host executor is required; no phone was contacted.', file=sys.stderr)
        return 1
    adb, signer = shutil.which('adb'), shutil.which('apksigner')
    if not adb or not signer:
        print('Verified ADB and apksigner are required; Android setup has not finished.', file=sys.stderr)
        return 1
    child = None
    mapping_owner = None
    owned_ports = set()
    ports = (SUMMARY_PORT, BROWSER_PORT) if args.full_browser else (SUMMARY_PORT,)
    stop = False

    def request_stop(_signum, _frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        with tempfile.TemporaryDirectory(prefix='magpie-usb-') as temp_dir:
            path = Path(temp_dir) / 'handoff.json'
            cmd = [sys.executable, '-I', str(PROJECT / 'host/bridge.py'), '--auto-browser-key', '--pairing-file', str(path)]
            if args.allow_write:
                cmd.extend(('--allow-write', '--magpie-bin', args.magpie_bin))
            if args.full_browser:
                cmd.append('--full-browser')
            child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 25
            while not path.exists():
                if child.poll() is not None or stop or time.monotonic() > deadline:
                    raise RuntimeError('Local Magpie bridge could not start. Existing Browser access must be enabled.')
                time.sleep(0.1)
            credential = parse_handoff(path.read_bytes())
            path.unlink()
            current, last_problem = None, ''
            # launchd starts a fresh bridge before the seven-day TLS certificate expires.
            renew_at = time.monotonic() + 6 * 24 * 60 * 60
            while not stop and time.monotonic() < renew_at:
                if child.poll() is not None:
                    raise RuntimeError('Local Magpie bridge stopped; USB session is no longer valid.')
                try:
                    devices = usb_devices(adb)
                    selected = os.environ.get('ANDROID_SERIAL')
                    if selected:
                        devices = [device for device in devices if device == selected]
                    if len(devices) > 1:
                        raise RuntimeError('More than one authorized USB phone is connected; refusing to choose a target.')
                    next_device = devices[0] if devices else None
                    lost_mapping = current is not None and current == next_device and len(mapped_ports(adb, current, ports)) != len(ports)
                    if next_device != current or lost_mapping:
                        if mapping_owner and not lost_mapping:
                            remove_mappings(adb, mapping_owner, owned_ports)
                            owned_ports.clear()
                            mapping_owner = None
                        current = None
                        if next_device:
                            owned_ports.update(connect(adb, signer, next_device, credential, temp_dir, ports))
                            current = next_device
                            mapping_owner = current if owned_ports else None
                        else:
                            print('Waiting for an authorized USB phone.', flush=True)
                    last_problem = ''
                except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
                    # Only controlled error messages above are printed; no raw credential/command payloads.
                    message = str(error) if isinstance(error, RuntimeError) else 'USB command could not be completed.'
                    if message != last_problem:
                        print(message, file=sys.stderr, flush=True)
                    last_problem = message
                time.sleep(2)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(str(error) if isinstance(error, RuntimeError) else 'USB supervisor failed.', file=sys.stderr)
        return 1
    finally:
        if mapping_owner:
            remove_mappings(adb, mapping_owner, owned_ports)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
