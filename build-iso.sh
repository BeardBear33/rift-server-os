#!/usr/bin/env bash
# Repack verified Debian 13.7.0 netinst ISO, preserve BIOS/UEFI boot information.
set -Eeuo pipefail
[[ $# -eq 2 ]] || { echo 'Použití: bash build-iso.sh debian-13.7.0-amd64-netinst.iso Rift-Server-OS.iso' >&2; exit 2; }
BASE=$(realpath "$1")
OUTPUT=$(realpath -m "$2")
[[ -f "$BASE" && ! -e "$OUTPUT" ]] || { echo 'Vstup chybí nebo výstup již existuje.' >&2; exit 1; }
printf '%s  %s\n' a7ef94ac2fb9a7fec454552abd629b7cc9d5155c886165a45649f5ce6167e355 "$BASE" | sha256sum -c -
command -v xorriso >/dev/null || { echo 'Nainstaluj xorriso.' >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT
mkdir -p "$TEMP/rift"
cp -a "$HERE/app" "$HERE/install.sh" "$TEMP/rift/"
cp "$HERE/rift-media/rift/preseed.cfg" "$TEMP/rift/preseed.cfg"
xorriso -indev "$BASE" -outdev "$OUTPUT" -boot_image any replay \
 -map "$TEMP/rift" /rift \
 -map "$HERE/rift-media/isolinux/rift.cfg" /isolinux/rift.cfg \
 -map "$HERE/rift-media/isolinux/menu.cfg" /isolinux/menu.cfg \
 -map "$HERE/rift-media/isolinux/gtk.cfg" /isolinux/gtk.cfg \
 -map "$HERE/rift-media/boot/grub/grub.cfg" /boot/grub/grub.cfg \
 -commit -end
sha256sum "$OUTPUT"
