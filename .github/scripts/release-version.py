"""Reject a release tag that doesn't match the package metadata."""

import os
from pathlib import Path

from packaging.version import Version


def release_version(project, tag):
    version = project["project"]["version"]
    parsed = Version(version)
    if version != str(parsed):
        raise ValueError(f"Use the normalized package version: {parsed}")
    if tag and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} must match package version v{version}")
    return version, parsed.is_prerelease  # also true for .dev versions


if __name__ == "__main__":
    import tomllib

    project = tomllib.loads(Path("pyproject.toml").read_text())
    version, prerelease = release_version(project, os.environ.get("RELEASE_TAG", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"version={version}\nprerelease={str(prerelease).lower()}\n")
    print(f"Package version: {version}")
