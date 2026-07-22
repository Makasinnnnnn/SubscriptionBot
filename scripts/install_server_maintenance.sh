#!/usr/bin/env sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }

ROOT=${PROPGATE_ROOT:-/opt/propgate}
[ -f "$ROOT/.env.production" ] || { echo "$ROOT/.env.production not found." >&2; exit 1; }

install -d -m 700 /etc/propgate /var/lib/propgate-monitor
if [ ! -f /etc/propgate/maintenance.env ]; then
  cat >/etc/propgate/maintenance.env <<EOF
PROPGATE_ROOT=$ROOT
MONITOR_BASE_URL=https://propgate.ru
BACKUP_OFFSITE_TARGET=
EOF
  chmod 600 /etc/propgate/maintenance.env
fi

cat >/etc/systemd/system/propgate-backup.service <<EOF
[Unit]
Description=PropGate verified PostgreSQL backup
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=$ROOT
EnvironmentFile=-/etc/propgate/maintenance.env
ExecStart=$ROOT/scripts/backup_db.sh
ExecStartPost=$ROOT/scripts/upload_latest_backup.sh
EOF

cat >/etc/systemd/system/propgate-backup.timer <<'EOF'
[Unit]
Description=Run PropGate backup every day

[Timer]
OnCalendar=*-*-* 03:15:00 UTC
RandomizedDelaySec=15m
Persistent=true
Unit=propgate-backup.service

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/propgate-healthcheck.service <<EOF
[Unit]
Description=PropGate production health check
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-/etc/propgate/maintenance.env
ExecStart=/usr/bin/python3 $ROOT/scripts/healthcheck.py
EOF

cat >/etc/systemd/system/propgate-healthcheck.timer <<'EOF'
[Unit]
Description=Check PropGate production every five minutes

[Timer]
OnBootSec=2m
OnUnitActiveSec=5m
AccuracySec=30s
Persistent=true
Unit=propgate-healthcheck.service

[Install]
WantedBy=timers.target
EOF

chmod 755 "$ROOT/scripts/backup_db.sh" "$ROOT/scripts/upload_latest_backup.sh"
chmod 755 "$ROOT/scripts/healthcheck.py"
systemctl daemon-reload
systemctl enable --now propgate-backup.timer propgate-healthcheck.timer
systemctl start propgate-backup.service
systemctl start propgate-healthcheck.service
systemctl list-timers 'propgate-*' --no-pager
