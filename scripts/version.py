"""Keep LAN BRIDGE release versions synchronized from the root VERSION file."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"


def read_versions() -> dict[str, str]:
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init = (ROOT / "code_cn_bridge" / "__init__.py").read_text(encoding="utf-8")
    return {
        "VERSION": VERSION_FILE.read_text(encoding="utf-8").strip(),
        "pyproject.toml": re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE).group(1),
        "code_cn_bridge/__init__.py": re.search(r'^__version__ = "([^"]+)"$', init, re.MULTILINE).group(1),
        "desktop/package.json": str(package["version"]),
        "desktop/package-lock.json": str(lock["version"]),
        "desktop/package-lock.json#root": str(lock["packages"][""]["version"]),
    }


def replace_once(path: Path, pattern: str, replacement: str, *, count: int = 1) -> None:
    with path.open("r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    updated, replaced = re.subn(pattern, replacement, text, count=count, flags=re.MULTILINE)
    if replaced != count:
        raise RuntimeError(f"expected {count} version field(s) in {path}, found {replaced}")
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(updated)


def set_version(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8")
    replace_once(ROOT / "pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"')
    replace_once(
        ROOT / "code_cn_bridge" / "__init__.py",
        r'^__version__ = "[^"]+"',
        f'__version__ = "{version}"',
    )
    replace_once(
        ROOT / "desktop" / "package.json",
        r'^(  "version": )"[^"]+",',
        rf'\1"{version}",',
    )
    replace_once(
        ROOT / "desktop" / "package-lock.json",
        r'^(\s+"version": )"[^"]+",',
        rf'\1"{version}",',
        count=2,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if any release version differs from VERSION")
    parser.add_argument("--set", metavar="VERSION", help="synchronize release metadata to MAJOR.MINOR.PATCH")
    args = parser.parse_args()
    if args.check == bool(args.set):
        parser.error("choose exactly one of --check or --set")
    if args.set:
        set_version(args.set)
    versions = read_versions()
    expected = versions["VERSION"]
    mismatches = {name: value for name, value in versions.items() if value != expected}
    if mismatches:
        for name, value in mismatches.items():
            print(f"{name}: {value} (expected {expected})")
        return 1
    print(f"LAN BRIDGE version metadata is synchronized at {expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
