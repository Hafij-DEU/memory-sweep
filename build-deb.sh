#!/bin/bash
# Build dist/memory-sweep_<version>_all.deb
set -euo pipefail
cd "$(dirname "$0")"
PKG=memory-sweep
ID=io.github.hafij_bge.MemorySweep
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' src/memory-sweep)
STAGE=$(mktemp -d)
chmod 755 "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

install -Dm755 src/memory-sweep "$STAGE/usr/bin/memory-sweep"
install -Dm644 data/$ID.desktop "$STAGE/usr/share/applications/$ID.desktop"
install -Dm644 data/icon.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/$ID.svg"
install -Dm644 LICENSE "$STAGE/usr/share/doc/$PKG/copyright"

# Settings > Memory Optimization runs this helper as root through pkexec; the
# policy names its path, so polkit shows "Change swap, zram and zswap".
install -Dm755 src/memory-sweep-swap "$STAGE/usr/libexec/$PKG/memory-sweep-swap"
install -Dm644 data/$ID.policy "$STAGE/usr/share/polkit-1/actions/$ID.policy"
for f in data/icons/*.svg; do
    install -Dm644 "$f" "$STAGE/usr/share/$PKG/icons/$(basename "$f")"
done

mkdir -p "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<CTL
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.12), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.7), pkexec | policykit-1
Recommends: zram-tools
Maintainer: Md. Hafijur Rahman <mdhafijur.rahman@ogr.deu.edu.tr>
Installed-Size: $(du -sk "$STAGE/usr" | cut -f1)
Description: See what fills your RAM, then end it or remove it
 GTK4/libadwaita app that groups processes by app, measures them with
 PSS so multi-process browsers are counted fairly, and ends them
 politely or by force. It also lists the files in /tmp and /dev/shm,
 which live in RAM, and removes the ones you own.
 .
 The overview shows RAM, swap, zram and tmpfs use with a live chart and
 suggestions. Settings > Memory Optimization sets the zram size and
 compression, the swap file size, zswap, swappiness and cache pressure,
 and installs or removes zram-tools, all after one password prompt.
CTL
cat > "$STAGE/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
SH
cp "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

mkdir -p dist
dpkg-deb --root-owner-group --build "$STAGE" "dist/${PKG}_${VERSION}_all.deb"
