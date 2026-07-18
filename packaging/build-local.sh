#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
work=${TMPDIR:-/tmp}/uwu-launcher-package
out=${PKGDEST:-$HOME/uwu-packages}
version=2.0.0

rm -rf "$work"
mkdir -p "$work" "$out"
cp "$root/packaging/PKGBUILD" "$root/packaging/uwu-launcher.install" "$work/"

list=$(mktemp)
trap 'rm -f "$list"' EXIT HUP INT TERM
(
    cd "$root"
    git ls-files --cached --others --exclude-standard \
        | grep -v '^packaging/PKGBUILD$' \
        | grep -v '^packaging/uwu-launcher.install$' \
        > "$list"
    tar -cf "$work/uwu-launcher-$version.tar" \
        --transform="s,^,uwu-launcher-$version/," \
        -T "$list"
)

(
    cd "$work"
    PKGDEST="$out" makepkg --cleanbuild --force --noconfirm
)

printf 'Built %s\n' "$out/uwu-launcher-$version-1-any.pkg.tar.zst"
