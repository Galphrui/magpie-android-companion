#!/usr/bin/env python3
"""Pack the public CSS and JavaScript of an upstream Magpie checkout."""

import argparse
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ASSETS = "internal/gui/assets/"
ASSET_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:js|css)")
OUTPUT = Path(__file__).resolve().parents[1] / "app/web-assets.zip"


def write_entry(archive, name, data):
    entry = ZipInfo(f"magpie-web/{name}")
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    archive.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=9)


def build(checkout, output):
    git = ["git", "-C", str(checkout)]
    revision = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    source = subprocess.check_output(
        [*git, "archive", "--format=tar", revision, "LICENSE", ASSETS.rstrip("/")]
    )
    entries = 0
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        packed = Path(temporary) / output.name
        with tarfile.open(fileobj=BytesIO(source)) as upstream, ZipFile(packed, "w") as archive:
            with upstream.extractfile("LICENSE") as license_file:
                write_entry(archive, "LICENSE", license_file.read())
            write_entry(archive, "SOURCE", f"https://github.com/yetone/magpie/tree/{revision}\n".encode())
            for member in upstream:
                if not member.isfile() or not member.name.startswith(ASSETS):
                    continue
                path = member.name.removeprefix(ASSETS)
                if not ASSET_PATH.fullmatch(path):
                    continue
                with upstream.extractfile(member) as asset:
                    data = asset.read()
                write_entry(archive, f"{sha256(data).hexdigest()[:12]}/{path}", data)
                entries += 1
        if not entries:
            raise ValueError("the upstream checkout contains no browser assets")
        packed.replace(output)
    return entries


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="official upstream checkout at the desired release")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(f"Packed {build(args.checkout, args.output)} static assets into {args.output}")
