#!/usr/bin/env bash
# Re-create vendor/ac-ffmpeg from upstream plus our resampler fix.
#
# The vendored tree is committed, so running this is only necessary when re-vendoring (for
# example to move to a newer upstream revision). See vendor/README.md.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
upstream="${AC_FFMPEG_REPO:-https://github.com/angelcam/rust-ac-ffmpeg}"
revision="${AC_FFMPEG_REVISION:-main}"
patch_dir="$repo_root/patches"
target="$repo_root/vendor/ac-ffmpeg"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

git clone --quiet "$upstream" "$work/upstream"
git -C "$work/upstream" checkout --quiet "$revision"

for patch in "$patch_dir"/*.patch; do
    git -C "$work/upstream" -c user.name=vendor -c user.email=vendor@localhost am "$patch"
done

rm -rf "$target"
mkdir -p "$(dirname "$target")"
cp -r "$work/upstream" "$target"
rm -rf "$target/.git"

echo "vendored $revision + $(ls "$patch_dir"/*.patch | wc -l) patch(es) into vendor/ac-ffmpeg"
