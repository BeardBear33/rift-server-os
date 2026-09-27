#!/usr/bin/env bash
# Fresh Debian 13 amd64 only. Does not partition or erase disks.
set -Eeuo pipefail
trap 'echo "Instalace Rift Server OS selhala na řádku $LINENO" >&2' ERR
[[ $EUID -eq 0 ]] || { echo 'Spusť jako root.' >&2; exit 1; }
. /etc/os-release
[[ ${ID:-} == debian && ${VERSION_ID:-} == 13 && $(uname -m) == x86_64 ]] || { echo 'Vyžadován Debian 13 amd64.' >&2; exit 1; }
SOURCE_DIR=$(cd "$(dirname "$0")" && pwd)
[[ -e "$SOURCE_DIR/app/panel.py" ]] || { echo 'Instalační soubory nejsou kompletní.' >&2; exit 1; }
[[ ! -e /srv/rift/panel.db ]] || { echo 'Rift již existuje. Data nebudou přepsána.' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 openjdk-21-jdk-headless openjdk-25-jdk-headless \
  mariadb-server openssh-server vsftpd git ca-certificates curl openssl smartmontools \
  chromium openbox lightdm xserver-xorg-core xserver-xorg-input-all xserver-xorg-video-all x11-xserver-utils
id rift >/dev/null 2>&1 || useradd --system --home-dir /srv/rift --shell /usr/sbin/nologin --user-group rift
install -d -o rift -g rift -m 0770 /srv/rift /srv/rift/servers /srv/rift/backups
install -d -m 0755 /mnt/rift-backups
if mountpoint -q /mnt/rift-backups; then chown rift:rift /mnt/rift-backups; chmod 0770 /mnt/rift-backups; fi
install -d -o root -g root -m 0755 /opt/rift/app /opt/rift/app/static
cp -a "$SOURCE_DIR/app/." /opt/rift/app/
chown -R root:root /opt/rift
chmod -R a+rX /opt/rift
install -d -o root -g rift -m 0750 /etc/rift /etc/rift/tls
openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
  -keyout /etc/rift/tls/server.key -out /etc/rift/tls/server.crt \
  -subj '/CN=rift-server.local' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' >/dev/null 2>&1
chown root:rift /etc/rift/tls/server.key
chmod 0640 /etc/rift/tls/server.key
chmod 0644 /etc/rift/tls/server.crt
cat > /etc/tmpfiles.d/rift.conf <<'EOF'
d /run/rift 2770 root rift - -
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/rift.conf
cat > /etc/systemd/system/rift-root.service <<'EOF'
[Unit]
Description=Rift privileged helper
After=systemd-tmpfiles-setup.service
[Service]
Type=simple
User=root
Group=rift
WorkingDirectory=/opt/rift/app
ExecStart=/usr/bin/python3 /opt/rift/app/helper.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/run/rift /srv/rift /etc/fstab /mnt/rift-backups
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/rift-panel.service <<'EOF'
[Unit]
Description=Rift Server OS panel
After=network-online.target rift-root.service
Wants=network-online.target
Requires=rift-root.service
[Service]
Type=simple
User=rift
Group=rift
WorkingDirectory=/opt/rift/app
ExecStart=/usr/bin/python3 /opt/rift/app/panel.py
Restart=on-failure
RestartSec=5
UMask=0007
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/srv/rift /run/rift
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/rift-mc@.service <<'EOF'
[Unit]
Description=Rift Minecraft %i
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=rift
Group=rift
WorkingDirectory=/srv/rift/servers/%i
ExecStart=/usr/bin/python3 /opt/rift/app/runner.py %i
Restart=on-failure
RestartSec=10
KillMode=mixed
TimeoutStopSec=130
UMask=0007
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/srv/rift /run/rift
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
ADMIN_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(20))')
printf '{"name":"admin","password":"%s"}\n' "$ADMIN_PASSWORD" | su -s /bin/sh rift -c 'cd /opt/rift/app && python3 panel.py init-admin'
cat > /root/rift-first-login.txt <<EOF
RIFT SERVER OS – PRVNÍ PŘIHLÁŠENÍ
Panel: https://IP_SERVERU:8443
Účet: admin
Heslo: $ADMIN_PASSWORD
Po přihlášení změň heslo v sekci Účet. Heslo k Debianu je to, které jsi zadal při instalaci.
EOF
chmod 0600 /root/rift-first-login.txt
id rift-kiosk >/dev/null 2>&1 || useradd --create-home --shell /bin/bash rift-kiosk
cat > /usr/local/bin/rift-kiosk <<'EOF'
#!/bin/sh
xset s off -dpms >/dev/null 2>&1 || true
openbox --sm-disable &
while :; do
  chromium --kiosk --incognito --no-first-run --disable-session-crashed-bubble \
    --allow-insecure-localhost https://localhost:8443
  sleep 3
done
EOF
chmod 0755 /usr/local/bin/rift-kiosk
install -d -m 0755 /usr/share/xsessions /etc/lightdm/lightdm.conf.d
cat > /usr/share/xsessions/rift-kiosk.desktop <<'EOF'
[Desktop Entry]
Name=Rift Server OS
Exec=/usr/local/bin/rift-kiosk
Type=Application
EOF
cat > /etc/lightdm/lightdm.conf.d/50-rift.conf <<'EOF'
[Seat:*]
autologin-user=rift-kiosk
autologin-user-timeout=0
user-session=rift-kiosk
EOF
cat > /etc/systemd/system/rift-kiosk.service <<'EOF'
[Unit]
Description=Rift local graphical kiosk
After=rift-panel.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/systemctl start lightdm.service
ExecStop=/usr/bin/systemctl stop lightdm.service
[Install]
WantedBy=multi-user.target
EOF
HOST_ADMIN=$(getent passwd | awk -F: '$3>=1000 && $3<60000 && $1!="rift-kiosk" {print $1;exit}')
if [[ -n "$HOST_ADMIN" ]]; then usermod -aG rift "$HOST_ADMIN"; fi
cat > /etc/vsftpd.conf <<'EOF'
listen=YES
listen_ipv6=NO
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=077
chroot_local_user=YES
allow_writeable_chroot=YES
local_root=/srv/rift/servers
ssl_enable=YES
force_local_logins_ssl=YES
force_local_data_ssl=YES
rsa_cert_file=/etc/rift/tls/server.crt
rsa_private_key_file=/etc/rift/tls/server.key
pasv_min_port=40000
pasv_max_port=40100
EOF
# Debian installer target is chrooted; systemctl enable works there, start only after first boot.
if [[ -d /run/systemd/system ]]; then systemctl daemon-reload; fi
systemctl enable mariadb.service ssh.service rift-root.service rift-panel.service rift-kiosk.service
systemctl disable vsftpd.service >/dev/null 2>&1 || true
if [[ -d /run/systemd/system ]]; then
  systemctl start mariadb.service ssh.service rift-root.service rift-panel.service
  systemctl start rift-kiosk.service || true
fi
printf 'Panel: https://%s:8443 | První heslo: /root/rift-first-login.txt\n' "$(hostname -I | awk '{print $1}')"
