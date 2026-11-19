"""Reject a release tag that doesn't match the package version (snappie/__init__.py)."""

import ast
import os
from pathlib import Path

from packaging.version import Version


def package_version(source):
    """__version__ from the module's source, without importing it."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["__version__"]:
            return ast.literal_eval(node.value)
    raise ValueError("no __version__ in snappie/__init__.py")


def release_version(version, tag):
    parsed = Version(version)
    if version != str(parsed):
        raise ValueError(f"Use the normalized package version: {parsed}")
    if tag and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} must match package version v{version}")
    return version, parsed.is_prerelease  # also true for .dev versions


if __name__ == "__main__":
    version = package_version(Path("snappie/__init__.py").read_text())
    version, prerelease = release_version(version, os.environ.get("RELEASE_TAG", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"version={version}\nprerelease={str(prerelease).lower()}\n")
    print(f"Package version: {version}")
