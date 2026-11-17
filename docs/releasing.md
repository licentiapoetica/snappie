# Releasing

The release workflow builds x86-64 and ARM64 wheels, Arch Linux x86-64 packages,
and a source archive. Pushing
a `vVERSION` tag publishes a GitHub release only after the full test suite and
both installed-wheel checks and the Arch install check pass. The tag must match `version` in `pyproject.toml`
exactly. Release candidates such as `0.2.0rc1` are marked as prereleases.

Update the package version, merge that change into `master`, then tag that commit.
For example, the first release uses:

```sh
git switch master
git pull --ff-only
git tag -a v0.1.0 -m "Snappie 0.1.0"
git push origin v0.1.0
```

The workflow attaches both wheels, both Arch packages, one source archive and `SHA256SUMS`, with
generated release notes. Incomplete uploads stay in a draft that a rerun can
resume. Published assets are not overwritten; corrections need a new version.
Builds for different tags can run in parallel. Publish jobs queue across tags so
the Latest lookup and update run one at a time. Version comparisons use Python's
PEP 440 ordering, including local versions, postreleases and epochs.

To test the process before tagging, run **Actions → release → Run workflow**.
Manual runs build and test the packages and provide downloadable workflow
artifacts without publishing a release. Workflow pull requests also check both
architectures. All automation uses GitHub's built-in token; no publishing secret
or PyPI account is needed.

The workflow and its scripts:

- `.github/workflows/release.yml` builds on Ubuntu 22.04 (x86-64 and ARM64), tags the wheels
  `manylinux_2_35_…` (the build system's glibc), and publishes after `tests.yml` passes.
- `.github/scripts/release-version.py` rejects a tag that doesn't match the package version.
- `.github/scripts/check-wheel.py` checks a wheel installed outside the checkout, with no build
  tools on `PATH`. `tests.yml` runs it too.
- `.github/scripts/publish-release.sh` uploads to a draft first and publishes it last. Only the
  highest stable version becomes Latest, so a patch for an older series doesn't replace it.

## Arch packages

The `arch` job uses the official `archlinux:base-devel` container and the same
source archive as the wheels. It renders `packaging/arch/PKGBUILD.in` with the
release version and the archive's SHA-256, then runs makepkg as an unprivileged
user. The split packages are:

- `snappie`: Python app, compiled capture helper, launcher and icon.
- `snappie-purpose`: compiled KF6 Purpose plugin and its D-Bus activation service.

The workflow installs both packages and checks the installed files, native library
links and helper setup with build tools absent from PATH. The package checksums
are included in `SHA256SUMS`. Manual runs and packaging PRs upload the same Arch
packages as workflow artifacts without publishing a release. Older published
releases retain their existing assets.

To rebuild locally on Arch, install `base-devel`, the recipe's `makedepends`, and
download the release source archive. From a checkout containing the recipe:

```sh
python .github/scripts/prepare-arch-package.py ~/Downloads/snappie-0.1.0.tar.gz \
    /tmp/snappie-package --version 0.1.0
cd /tmp/snappie-package
makepkg --syncdeps
```

Use the version and archive you want to build. Arch's `epoch` is stored separately
from `pkgver`; development versions are translated to keep them before final
versions in pacman's ordering. Packages depend on the build interpreter's Python
minor version because its site-packages path includes that version.
