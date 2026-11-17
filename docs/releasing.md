# Releasing

The release workflow builds x86-64 and ARM64 wheels plus a source archive. Pushing
a `vVERSION` tag publishes a GitHub release only after the full test suite and
both installed-wheel checks pass. The tag must match `version` in `pyproject.toml`
exactly. Release candidates such as `0.2.0rc1` are marked as prereleases.

Update the package version, merge that change into `master`, then tag that commit.
For example, the first release uses:

```sh
git switch master
git pull --ff-only
git tag -a v0.1.0 -m "Snappie 0.1.0"
git push origin v0.1.0
```

The workflow attaches both wheels, one source archive and `SHA256SUMS`, with
generated release notes. Incomplete uploads stay in a draft that a rerun can
resume. Published assets are not overwritten; corrections need a new version.

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
