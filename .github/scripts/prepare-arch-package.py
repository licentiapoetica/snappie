"""Prepare a checksum-verified makepkg build from the same sdist used for wheels."""

import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile
import tomllib

from packaging.version import Version


def arch_version(version):
    parsed = Version(version)
    # Arch stores epochs separately. A bare Python .dev release precedes alpha 0;
    # uppercase A sorts before normalized alpha/beta/rc suffixes in vercmp.
    value = str(parsed).split("!")[-1]
    public, separator, local = value.partition("+")
    if parsed.dev is not None and parsed.pre is None and parsed.post is None:
        public = public.replace(".dev", "Adev")
    else:
        public = public.replace(".dev", "dev")
    return public + separator + local, parsed.epoch


def prepare(archive, destination, expected_version):
    version = str(Version(expected_version))
    if version != expected_version or archive.name != f"snappie-{version}.tar.gz":
        raise ValueError("Archive name and normalized release version must match")
    with tarfile.open(archive) as source:
        try:
            stream = source.extractfile(f"snappie-{version}/pyproject.toml")
        except KeyError as e:
            raise ValueError("Source archive layout does not match release version") from e
        if stream is None:
            raise ValueError("Source archive has no package metadata")
        metadata = tomllib.loads(stream.read().decode())
    if metadata["project"]["version"] != version:
        raise ValueError("Source archive version does not match release version")
    pkgver, epoch = arch_version(version)
    recipe = (Path(__file__).resolve().parents[2] / "packaging/arch/PKGBUILD.in").read_text()
    for key, value in {"PKGVER": pkgver, "EPOCH": str(epoch), "SOURCE_VERSION": version,
                       "SOURCE_SHA256": hashlib.sha256(archive.read_bytes()).hexdigest()}.items():
        recipe = recipe.replace(f"@{key}@", value)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / archive.name
    if target.resolve() != archive.resolve():
        shutil.copyfile(archive, target)
    (destination / "PKGBUILD").write_text(recipe)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    prepare(args.archive, args.destination, args.version)
