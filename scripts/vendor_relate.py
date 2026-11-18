"""Vendor the Relate graph database's WebAssembly build into the static demo (spec 13a).

    python scripts/vendor_relate.py           # download, verify, extract (skips if present)
    python scripts/vendor_relate.py --check   # only verify what is already there

The demo's "Explore the graph" section (`site/explore.js`, `site/graph-worker.js`) runs
Relate in the visitor's browser. Relate's browser build is the npm package
`@relate/relate-wasm`: a JavaScript loader (`relate.js`) and a 7.1 MB WebAssembly file
(`relate_bg.wasm`). It is not an npm dependency of the project (there is no npm here):
this script fetches exactly one pinned tarball from the npm registry and checks it.

Supply chain:

- **Pinned:** `VERSION` and the tarball URL are fixed here.
- **Verified:** the tarball's SHA-512 must equal `INTEGRITY`, the registry's own
  Subresource-Integrity value for that version, committed here. Each extracted file's
  SHA-256 must equal `FILES`. Any mismatch stops the script with exit code 1, and with it
  the Pages workflow (`.github/workflows/pages.yml`), which runs this script before
  uploading the site.
- **Only what the page needs** is extracted, by exact name (never the tarball's own
  paths), into `site/vendor/relate/`, which is git-ignored: the binary never enters git.
- **Licence:** the package is Apache-2.0 but ships no LICENSE file, so the licence text
  (`scripts/vendor/relate-wasm-LICENSE.txt`, from the same project's `relate-db` wheel)
  is copied next to the files it covers, and checked by its SHA-256 like the others.

Standard library only, so the Pages workflow runs it with the runner's own Python,
without installing the project. Nothing here talks to Wikimedia.
"""

import argparse
import base64
import hashlib
import io
import logging
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "site" / "vendor" / "relate"
LICENSE_SOURCE = ROOT / "scripts" / "vendor" / "relate-wasm-LICENSE.txt"

PACKAGE = "@relate/relate-wasm"
VERSION = "1.1.0"
TARBALL_URL = f"https://registry.npmjs.org/{PACKAGE}/-/relate-wasm-{VERSION}.tgz"
# The registry's `dist.integrity` for this version (Subresource Integrity format).
INTEGRITY = (
    "sha512-CVBCTnTRk7Wz7e/gWEV3ieESlK/yhHBurl/27K5HJ48a"
    "Je+AZNh94OOGRcLznr+nTm7NOokp2mPbxeRbKAzDIA=="
)
# Extracted files: path inside the tarball -> (path under site/vendor/relate/, SHA-256).
# relate.js imports the OPFS snippet by its relative path, so that path is kept.
FILES = {
    "package/relate.js": (
        "relate.js",
        "c14a229554cb0961605269072848b773ee49fd53272a75fca6272058d1bf2628",
    ),
    "package/relate_bg.wasm": (
        "relate_bg.wasm",
        "8c3ffba15745a685ab4a13cb15f61769a803534dcc02834ed374e3d8a5e7a970",
    ),
    "package/trap.js": (
        "trap.js",
        "d50e37f6163e14d8d990754f80fb5b0cc3257836313bef4474505bf1a27d832a",
    ),
    "package/snippets/relate-wasm-b80f57cef2af568f/js/opfs.js": (
        "snippets/relate-wasm-b80f57cef2af568f/js/opfs.js",
        "493a1c8d8a761ead191addd9701a2dd15276db250cda2323ad3659c1c9c7474e",
    ),
}
LICENSE_NAME = "LICENSE"
# SHA-256 of that licence text (LF line endings): byte for byte the LICENSE of the
# relate_db-1.1.0 wheel apart from its line endings ("Copyright 2026 The Relate Authors").
LICENSE_SHA256 = "ac87d12e27d69f09130a7b4178e48e995ab131e4b476aa188e58ffac7ed7fab3"
# What the vendored files may add to the site (tests/test_demo_site.py checks it too).
MAX_VENDOR_BYTES = 8 * 1024 * 1024
MAX_TARBALL_BYTES = 16 * 1024 * 1024
USER_AGENT = "wikipulse-vendor-script/1.0 (fetches one pinned npm tarball)"
TIMEOUT_S = 60

logger = logging.getLogger("vendor_relate")


class VendorError(RuntimeError):
    """The download or a check failed; nothing must be served."""


def sri_sha512(data: bytes) -> str:
    """The Subresource-Integrity string of `data` ("sha512-<base64>")."""
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_tarball(data: bytes, integrity: str = INTEGRITY) -> None:
    """Raise `VendorError` unless `data` is exactly the pinned tarball."""
    actual = sri_sha512(data)
    if actual != integrity:
        raise VendorError(f"integrity mismatch for {PACKAGE}@{VERSION}: {actual} != {integrity}")


def extract(data: bytes, target: Path, files: dict = FILES) -> int:
    """Write the named members of the (verified) tarball under `target`; their total size.

    Members are looked up by exact name and written to paths from `files`, never to
    the tarball's own paths, so a crafted archive can't write elsewhere."""
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member_name, (relative, digest) in files.items():
            member = archive.getmember(member_name)
            if not member.isfile():
                raise VendorError(f"{member_name} is not a regular file")
            handle = archive.extractfile(member)
            assert handle is not None
            content = handle.read()
            if sha256(content) != digest:
                raise VendorError(f"{member_name}: SHA-256 {sha256(content)} != {digest}")
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            total += len(content)
    return total


def problems(target: Path = VENDOR_DIR, files: dict = FILES) -> list[str]:
    """What is missing or different in an existing vendored copy (empty: all good)."""
    found = []
    for relative, digest in files.values():
        path = target / relative
        if not path.is_file():
            found.append(f"missing {relative}")
        elif sha256(path.read_bytes()) != digest:
            found.append(f"{relative} differs from the pinned file")
    licence = target / LICENSE_NAME
    if not licence.is_file():
        found.append(f"missing {LICENSE_NAME}")
    elif sha256(licence.read_bytes()) != LICENSE_SHA256:
        found.append(f"{LICENSE_NAME} differs from the pinned licence text")
    return found


def vendored_bytes(target: Path = VENDOR_DIR) -> int:
    return sum(f.stat().st_size for f in target.rglob("*") if f.is_file())


def download(url: str = TARBALL_URL) -> bytes:
    """The tarball's bytes (one request, a size cap, a descriptive User-Agent)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
        data = response.read(MAX_TARBALL_BYTES + 1)
    if len(data) > MAX_TARBALL_BYTES:
        raise VendorError(f"{url} is larger than {MAX_TARBALL_BYTES} bytes")
    return data


def vendor(target: Path = VENDOR_DIR) -> None:
    """Download, verify and extract into a fresh `target`."""
    logger.info("Downloading %s@%s", PACKAGE, VERSION)
    data = download()
    verify_tarball(data)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    extract(data, target)
    shutil.copyfile(LICENSE_SOURCE, target / LICENSE_NAME)


def check_size(target: Path = VENDOR_DIR) -> int:
    size = vendored_bytes(target)
    if size > MAX_VENDOR_BYTES:
        raise VendorError(f"vendored files are {size} bytes, above {MAX_VENDOR_BYTES}")
    return size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Verify only; never download.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        found = problems()
        if found and args.check:
            raise VendorError("; ".join(found))
        if found:
            vendor()
            found = problems()
            if found:
                raise VendorError("; ".join(found))
        else:
            logger.info("Already vendored and verified: %s", VENDOR_DIR)
        size = check_size()
    except (VendorError, OSError, tarfile.TarError, KeyError) as error:
        logger.error("Vendoring %s@%s failed: %s", PACKAGE, VERSION, error)
        return 1
    logger.info("%s@%s in %s: %.1f MB, verified", PACKAGE, VERSION, VENDOR_DIR, size / 1e6)
    return 0


if __name__ == "__main__":
    sys.exit(main())
