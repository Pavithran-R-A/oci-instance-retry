#!/usr/bin/env bash
# Install the OCI A1 capacity watcher onto the EXISTING Ubuntu E2.1.Micro.
# This script never modifies/terminates the VM, and never runs in a tight loop.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer with sudo on vennila-oracle-micro." >&2
  exit 2
fi

REPO="https://github.com/Pavithran-R-A/oci-instance-retry.git"
DEST="/opt/oci-a1-hunter"
STATE="/var/lib/oci-hunter"
SERVICE="/etc/systemd/system/oci-a1-hunter.service"
TIMER="/etc/systemd/system/oci-a1-hunter.timer"

if [[ ! -f /etc/os-release ]] || ! grep -qi ubuntu /etc/os-release; then
  echo "Ubuntu is required. No changes made." >&2
  exit 2
fi

for package in git python3 python3-venv; do
  if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed"; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$package"
  fi
done

if ! id oci-hunter >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin oci-hunter
fi
install -d -m 0755 -o root -g root "$DEST"
install -d -m 0700 -o oci-hunter -g oci-hunter "$STATE"

if [[ -e "$DEST/.git" ]]; then
  git -C "$DEST" fetch --quiet origin main
  git -C "$DEST" checkout --force -q origin/main
else
  git clone --quiet --depth 1 --branch main "$REPO" "$DEST"
fi
python3 -m venv "$DEST/.venv"
PIP_NO_CACHE_DIR=1 "$DEST/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$DEST/requirements.txt"
"$DEST/.venv/bin/python" -m py_compile "$DEST/micro_hunter.py"

cat >"$SERVICE" <<'EOF'
[Unit]
Description=OCI Always Free A1 capacity hunter on Micro
Wants=network-online.target
After=network-online.target
ConditionPathExists=!/var/lib/oci-hunter/claimed

[Service]
Type=oneshot
User=oci-hunter
Group=oci-hunter
WorkingDirectory=/opt/oci-a1-hunter
ExecStart=/opt/oci-a1-hunter/.venv/bin/python -B /opt/oci-a1-hunter/micro_hunter.py
TimeoutStartSec=240
Nice=10
CPUWeight=10
MemoryMax=450M
OOMPolicy=stop
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ReadWritePaths=/var/lib/oci-hunter
Environment=PYTHONDONTWRITEBYTECODE=1

[Install]
WantedBy=multi-user.target
EOF

cat >"$TIMER" <<'EOF'
[Unit]
Description=Schedule OCI A1 capacity check every 5 minutes

[Timer]
OnCalendar=*-*-* *:01/5:00
RandomizedDelaySec=10
AccuracySec=1s
Persistent=true
Unit=oci-a1-hunter.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now oci-a1-hunter.timer

echo "INSTALL_OK: OCI A1 hunter timer is installed and enabled."
echo "TIMER: systemctl status oci-a1-hunter.timer --no-pager"
echo "LOGS: journalctl -u oci-a1-hunter.service -n 30 --no-pager"
echo "NOTE: OCI instance-principal IAM policy is required for API access."
echo "NOTE: The first timer run might report permission denied until IAM propagates."
