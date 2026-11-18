"""scripts/vendor_relate.py: the pinned, verified Relate WebAssembly build (spec 13a).

No network: tarballs are built in memory.
"""

import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "vendor_relate", ROOT / "scripts" / "vendor_relate.py"
)
assert spec is not None and spec.loader is not None
vendor_relate = importlib.util.module_from_spec(spec)
sys.modules["vendor_relate"] = vendor_relate
spec.loader.exec_module(vendor_relate)


def tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_the_version_and_its_integrity_are_pinned() -> None:
    assert vendor_relate.VERSION == "1.1.0"
    assert vendor_relate.TARBALL_URL.endswith("/relate-wasm-1.1.0.tgz")
    assert vendor_relate.TARBALL_URL.startswith("https://registry.npmjs.org/@relate/")
    assert vendor_relate.INTEGRITY.startswith("sha512-") and len(vendor_relate.INTEGRITY) == 95
    assert {"relate.js", "relate_bg.wasm"} <= {f for f, _ in vendor_relate.FILES.values()}
    licence = vendor_relate.LICENSE_SOURCE.read_text(encoding="utf-8")
    assert "Apache License" in licence[:200] and "Version 2.0" in licence[:200]


def test_a_tarball_that_is_not_the_pinned_one_is_refused() -> None:
    data = tarball({"package/relate.js": b"x"})
    with pytest.raises(vendor_relate.VendorError, match="integrity mismatch"):
        vendor_relate.verify_tarball(data)
    vendor_relate.verify_tarball(data, vendor_relate.sri_sha512(data))  # its own value passes


def test_only_named_files_are_written_and_each_is_checked(tmp_path: Path) -> None:
    good, extra = b"export default 1;", b"not wanted"
    data = tarball({"package/a.js": good, "package/../../evil.js": extra})
    files = {"package/a.js": ("a.js", sha(good))}
    assert vendor_relate.extract(data, tmp_path, files) == len(good)
    assert [p.name for p in tmp_path.rglob("*")] == ["a.js"]
    with pytest.raises(vendor_relate.VendorError, match="SHA-256"):
        vendor_relate.extract(data, tmp_path, {"package/a.js": ("a.js", sha(extra))})


def test_problems_list_what_is_missing_or_changed(tmp_path: Path) -> None:
    files = {"package/a.js": ("a.js", sha(b"1"))}
    assert "missing a.js" in vendor_relate.problems(tmp_path, files)
    (tmp_path / "a.js").write_bytes(b"2")
    (tmp_path / "LICENSE").write_text("Apache", encoding="utf-8")
    assert vendor_relate.problems(tmp_path, files) == [
        "a.js differs from the pinned file",
        "LICENSE differs from the pinned licence text",
    ]
    (tmp_path / "a.js").write_bytes(b"1")
    (tmp_path / "LICENSE").write_bytes(vendor_relate.LICENSE_SOURCE.read_bytes())
    assert vendor_relate.problems(tmp_path, files) == []


def test_the_committed_licence_is_the_pinned_text() -> None:
    licence = vendor_relate.LICENSE_SOURCE.read_bytes()
    assert vendor_relate.sha256(licence) == vendor_relate.LICENSE_SHA256


def test_check_mode_never_downloads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(vendor_relate, "VENDOR_DIR", tmp_path / "missing")
    monkeypatch.setattr(vendor_relate, "download", lambda *a: pytest.fail("downloaded"))
    monkeypatch.setattr(
        vendor_relate, "problems", lambda *a, **k: ["missing relate.js"]
    )  # nothing vendored
    assert vendor_relate.main(["--check"]) == 1
