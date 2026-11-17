#!/usr/bin/env bash
# Keep incomplete uploads in a draft; a rerun can resume that draft.
set -euo pipefail
: "${RELEASE_TAG:?}" "${RELEASE_PRERELEASE:?}"

assets=(dist/*.whl dist/*.tar.gz dist/SHA256SUMS)
for asset in "${assets[@]}"; do
    test -f "$asset"
done

if draft=$(gh release view "$RELEASE_TAG" --json isDraft --jq .isDraft 2>/dev/null); then
    if [ "$draft" != true ]; then
        echo "Release $RELEASE_TAG is already published; use a new version to release changes." >&2
        exit 1
    fi
    gh release upload "$RELEASE_TAG" "${assets[@]}" --clobber
else
    gh release create "$RELEASE_TAG" "${assets[@]}" --verify-tag --draft \
        --title "Snappie $RELEASE_TAG" --generate-notes \
        --notes "Linux wheels for x86-64 and ARM64, built on Ubuntu 22.04. Install with Python 3.10+ on a compatible system (glibc 2.35+ and libwayland-client). KDE Plasma 6 on Wayland and the documented runtime packages are required. See README for installation. SHA256SUMS covers the attached wheels and source archive."
fi

latest=true
if [ "$RELEASE_PRERELEASE" = true ]; then
    latest=false
fi
gh release edit "$RELEASE_TAG" --draft=false --prerelease="$RELEASE_PRERELEASE" --latest="$latest"
