"""Release helpers for .github/workflows/release.yml (spec 09c). Standard library only.

    python scripts/release.py version --tag v0.2.0          (or v0.2.0-rc.1)
        Checks that the tag is `v` + the version in pyproject.toml, optionally with a
        pre-release part, and prints the version (0.2.0, or 0.2.0-rc.1). A release is
        cut by bumping that version, then tagging. `--prerelease` prints true or false.

    python scripts/release.py package-chart --version 0.2.0 \
        --image-repository ghcr.io/<owner>/wikipulse --out dist
        Packages charts/wikipulse as wikipulse-0.2.0.tgz: chart version and appVersion
        set to the release version, and the default image set to the release's image in
        the registry, so `helm install oci://.../wikipulse --version 0.2.0` runs exactly
        the images built for that release. The chart folder itself is not changed.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "charts" / "wikipulse"
# Semantic versions: 1.2.3, optionally with a pre-release part (1.2.3-rc.1).
VERSION = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def project_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    return tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"]


def version_from_tag(tag: str, expected: str) -> str:
    """`v0.2.0` -> `0.2.0`; `v0.2.0-rc.1` -> `0.2.0-rc.1` (a pre-release of 0.2.0).

    The release part (before the first "-") must equal `expected`, the version in
    pyproject.toml; the full version, pre-release part included, is what the images,
    the chart and the GitHub Release are called.
    """
    if not tag.startswith("v") or not VERSION.match(tag[1:]):
        raise SystemExit(f"Tag {tag!r} is not a release tag like v1.2.3 or v1.2.3-rc.1")
    version = tag[1:]
    release_part = version.partition("-")[0]
    if release_part != expected:
        raise SystemExit(
            f"Tag {tag} doesn't match the version in pyproject.toml ({expected}): bump "
            "the version, commit, then tag that commit"
        )
    return version


def is_prerelease(version: str) -> bool:
    """`0.2.0-rc.1` is a pre-release: no floating `0.2` tag, marked as such on GitHub."""
    return "-" in version


def set_chart_versions(chart_yaml: str, version: str) -> str:
    """Chart.yaml with `version` and `appVersion` set to the release version."""
    text, versions = re.subn(r"(?m)^version: .*$", f"version: {version}", chart_yaml)
    text, apps = re.subn(r"(?m)^appVersion: .*$", f'appVersion: "{version}"', text)
    if versions != 1 or apps != 1:
        raise SystemExit("Chart.yaml needs exactly one `version:` and one `appVersion:` line")
    return text


def set_default_image(values_yaml: str, repository: str, tag: str) -> str:
    """values.yaml with the `image:` block's repository and tag replaced, comments kept."""
    block = re.search(r"(?ms)^image:\n(?:(?:  .*)?\n)+", values_yaml)
    if block is None:
        raise SystemExit("values.yaml has no top-level `image:` block")
    old = block.group(0)
    new, repos = re.subn(r"(?m)^  repository: .*$", f"  repository: {repository}", old)
    new, tags = re.subn(r"(?m)^  tag: .*$", f'  tag: "{tag}"', new)
    if repos != 1 or tags != 1:
        raise SystemExit("The `image:` block needs one `repository:` and one `tag:` line")
    return values_yaml.replace(old, new, 1)


def package_chart(version: str, repository: str, out: Path) -> Path:
    """`helm package` a copy of the chart with the release's versions and image."""
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        copy = Path(temp) / CHART.name
        shutil.copytree(CHART, copy)
        chart_yaml, values_yaml = copy / "Chart.yaml", copy / "values.yaml"
        chart_yaml.write_text(set_chart_versions(chart_yaml.read_text("utf-8"), version), "utf-8")
        values_yaml.write_text(
            set_default_image(values_yaml.read_text("utf-8"), repository, version), "utf-8"
        )
        subprocess.run(["helm", "lint", "--strict", str(copy)], check=True)
        subprocess.run(["helm", "package", str(copy), "--destination", str(out)], check=True)
    package = out / f"{CHART.name}-{version}.tgz"
    if not package.is_file():
        raise SystemExit(f"helm package didn't write {package}")
    return package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version", help="check a tag, print its version")
    version.add_argument("--tag", required=True)
    version.add_argument(
        "--prerelease", action="store_true", help="print true/false: is it a pre-release?"
    )
    chart = commands.add_parser("package-chart", help="package the chart for a release")
    chart.add_argument("--version", required=True)
    chart.add_argument("--image-repository", required=True)
    chart.add_argument("--out", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    if args.command == "version":
        version_text = version_from_tag(args.tag, project_version())
        print(str(is_prerelease(version_text)).lower() if args.prerelease else version_text)
    else:
        if not VERSION.match(args.version):
            raise SystemExit(f"Not a release version: {args.version!r}")
        print(package_chart(args.version, args.image_repository.lower(), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
