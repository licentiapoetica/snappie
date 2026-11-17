#!/usr/bin/env bash
# Keep incomplete uploads in a draft; a rerun can resume that draft.
set -euo pipefail
: "${RELEASE_TAG:?}" "${RELEASE_PRERELEASE:?}"

assets=(dist/*.whl dist/*.tar.gz dist/SHA256SUMS)
for asset in "${assets[@]}"; do
    test -f "$asset"
done

errors=$(mktemp)
trap 'rm -f "$errors"' EXIT

# Prints a field of a release (the latest one without a tag). Returns 1 only when gh says the
# release doesn't exist and 2 for any other failure (network, rate limit, auth), so a hiccup
# can't be mistaken for "missing" and create a second draft. Callers run it in a subshell.
release_field() {
    local field=$1
    shift
    if gh release view "$@" --json "$field" --jq ".$field" 2>"$errors"; then
        return 0
    fi
    if grep -qx "release not found" "$errors"; then
        return 1
    fi
    cat "$errors" >&2
    return 2
}

notes="Linux wheels for x86-64 and ARM64, built on Ubuntu 22.04. Install with Python 3.10+ "
notes+="on a system with glibc 2.35+ and libwayland-client. KDE Plasma 6 on Wayland and the "
notes+="documented runtime packages are required; see the README for installation. "
notes+="SHA256SUMS covers the attached wheels and source archive."

found=0
draft=$(release_field isDraft "$RELEASE_TAG") || found=$?
if [ "$found" = 0 ]; then
    if [ "$draft" != true ]; then
        echo "Release $RELEASE_TAG is already published; use a new version to release changes." >&2
        exit 1
    fi
    gh release upload "$RELEASE_TAG" "${assets[@]}" --clobber
elif [ "$found" = 1 ]; then
    gh release create "$RELEASE_TAG" "${assets[@]}" --verify-tag --draft \
        --title "Snappie $RELEASE_TAG" --generate-notes --notes "$notes"
else
    exit "$found"
fi

# Only the highest stable version is Latest, so a patch for an older series doesn't replace it.
latest=false
if [ "$RELEASE_PRERELEASE" != true ]; then
    found=0
    current=$(release_field tagName) || found=$?
    if [ "$found" = 1 ]; then
        latest=true
    elif [ "$found" != 0 ]; then
        exit "$found"
    else
        newest=$(printf '%s\n' "${current#v}" "${RELEASE_TAG#v}" | sort -V | tail -n 1)
        if [ "$newest" = "${RELEASE_TAG#v}" ]; then
            latest=true
        fi
    fi
fi
gh release edit "$RELEASE_TAG" --draft=false --prerelease="$RELEASE_PRERELEASE" --latest="$latest"
